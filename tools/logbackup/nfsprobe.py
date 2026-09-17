#!/usr/bin/env python3
"""Ask a host's mountd for its NFS export list, without installing nfs-common.

Speaks just enough ONC RPC to call PMAPPROC_GETPORT and then MOUNTPROC3_EXPORT.
"""
import socket
import struct
import sys

PMAP_PROG, PMAP_VERS, PMAP_GETPORT = 100000, 2, 3
MOUNT_PROG, MOUNT_EXPORT = 100005, 5


def rpc_call(sock, prog, vers, proc, params=b"", xid=1):
    body = struct.pack(">IIIIII", xid, 0, 2, prog, vers, proc)
    body += struct.pack(">II", 0, 0)  # cred  AUTH_NULL
    body += struct.pack(">II", 0, 0)  # verf  AUTH_NULL
    body += params
    sock.sendall(struct.pack(">I", 0x80000000 | len(body)) + body)

    chunks = []
    while True:
        hdr = recv_exact(sock, 4)
        marker = struct.unpack(">I", hdr)[0]
        chunks.append(recv_exact(sock, marker & 0x7FFFFFFF))
        if marker & 0x80000000:
            break
    reply = b"".join(chunks)

    off = 8  # xid, msg_type
    reply_stat = struct.unpack(">I", reply[off:off + 4])[0]
    off += 4
    if reply_stat != 0:
        raise RuntimeError("RPC denied (reply_stat=%d)" % reply_stat)
    off += 4  # verf flavor
    vlen = struct.unpack(">I", reply[off:off + 4])[0]
    off += 4 + ((vlen + 3) // 4) * 4
    accept_stat = struct.unpack(">I", reply[off:off + 4])[0]
    off += 4
    if accept_stat != 0:
        raise RuntimeError("RPC not accepted (accept_stat=%d)" % accept_stat)
    return reply[off:]


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise RuntimeError("connection closed mid-reply")
        buf += part
    return buf


def xdr_string(data, off):
    length = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    val = data[off:off + length].decode("utf-8", "replace")
    return val, off + ((length + 3) // 4) * 4


def main(host):
    with socket.create_connection((host, 111), timeout=10) as s:
        res = rpc_call(s, PMAP_PROG, PMAP_VERS, PMAP_GETPORT,
                       struct.pack(">IIII", MOUNT_PROG, 3, 6, 0))
        port = struct.unpack(">I", res[:4])[0]
    if port == 0:
        print("mountd not registered -- NFS exports unavailable")
        return 1
    print("mountd on port %d" % port)

    with socket.create_connection((host, port), timeout=10) as s:
        data = rpc_call(s, MOUNT_PROG, 3, MOUNT_EXPORT, xid=2)

    off = 0
    found = 0
    while off + 4 <= len(data) and struct.unpack(">I", data[off:off + 4])[0] == 1:
        off += 4
        path, off = xdr_string(data, off)
        groups = []
        while off + 4 <= len(data) and struct.unpack(">I", data[off:off + 4])[0] == 1:
            off += 4
            grp, off = xdr_string(data, off)
            groups.append(grp)
        off += 4  # terminating value_follows for the group list
        found += 1
        print("EXPORT %-50s allowed: %s" % (path, ", ".join(groups) or "(everyone)"))
    if not found:
        print("mountd is running but exports nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.52"))
