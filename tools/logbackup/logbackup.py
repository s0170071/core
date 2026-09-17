#!/usr/bin/env python3
"""
openWB log backup agent.

Archives every openWB log file to a remote store, deduplicated by content hash,
so that fast-rotating ramdisk logs (main.log rolls every ~17 min) are preserved
long after they would otherwise be discarded.

Design notes
------------
* Rotated files (``*.log.N``) are immutable once written, so hashing them gives
  exactly-once archival with no duplicates.
* A live ``*.log`` is snapshotted only every LIVE_SNAPSHOT_INTERVAL_S, which
  bounds the data lost to a reboot (tmpfs is volatile) without shipping the same
  growing file every run.
* Everything is gzipped into a staging tree first, then moved across in a single
  rsync, so one SSH connection covers the whole run.
* The remote store is pruned oldest-first to stay under MAX_BYTES.

Config lives in /etc/openwb-logbackup.conf (see DEFAULTS below for keys).
"""

import fcntl
import gzip
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

DEFAULTS = {
    # Where to look for logs (colon-separated globs).
    "sources": "/var/www/html/openWB/ramdisk/*.log*:/var/www/html/openWB/data/log/*",
    # "local:/path"  or  "ssh:user@host:/path"
    "dest": "",
    # Refuse to write unless this path is a real mount point. Without it a
    # dropped CIFS mount would silently fill the controller's SD card with
    # archives written into the bare mountpoint directory.
    "require_mount": "",
    # Private key for the ssh dest. Used when present; ssh_password is only a
    # fallback for servers that will not take a key.
    "ssh_key": "/root/.ssh/openwb_logbackup_ed25519",
    "ssh_password": "",
    "ssh_port": "22",
    # Total cap on the remote store, in bytes. 1 TB decimal.
    "max_bytes": str(1000 * 1000 * 1000 * 1000),
    # Minimum seconds between two snapshots of the same *live* log file.
    "live_snapshot_interval_s": "900",
    # Forget a content hash after this long, so the state file cannot grow
    # without bound. Re-archiving identical content later is harmless.
    "hash_ttl_days": "90",
    "staging_dir": "/var/tmp/openwb-logbackup-stage",
    # Ceiling on the local buffer that accumulates while the destination is
    # unreachable. The controller's root filesystem must never fill up, so past
    # this point the oldest staged archives are dropped.
    "max_staging_bytes": str(3 * 1024 * 1024 * 1024),
    "state_file": "/opt/openwb-logbackup/state.json",
    "label": "",  # defaults to hostname
}

CONF_PATH = os.environ.get("OPENWB_LOGBACKUP_CONF", "/etc/openwb-logbackup.conf")
LOCK_PATH = "/var/tmp/openwb-logbackup.lock"


def log(msg):
    sys.stderr.write("%s %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONF_PATH):
        with open(CONF_PATH) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                cfg[key.strip()] = val.strip().strip('"').strip("'")
    for key in ("dest", "sources", "label", "ssh_password", "staging_dir", "state_file"):
        env = os.environ.get("OPENWB_LOGBACKUP_" + key.upper())
        if env is not None:
            cfg[key] = env
    if not cfg["label"]:
        cfg["label"] = os.uname()[1]
    return cfg


def load_state(path):
    try:
        with open(path) as fh:
            state = json.load(fh)
    except (IOError, ValueError):
        state = {}
    state.setdefault("hashes", {})       # sha256 -> epoch archived
    state.setdefault("live_snapshots", {})  # abs path -> epoch
    return state


def save_state(path, state):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.rename(tmp, path)


def expand_sources(spec):
    import glob
    found = []
    for pattern in spec.split(":"):
        pattern = pattern.strip()
        if not pattern:
            continue
        for path in glob.glob(pattern):
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                found.append(os.path.realpath(path))
    return sorted(set(found))


def is_rotated(path):
    """True for main.log.1 style files, which never change again."""
    tail = os.path.basename(path).rsplit(".", 1)[-1]
    return tail.isdigit()


def has_rotated_sibling(path, all_paths):
    prefix = path + "."
    return any(p.startswith(prefix) and is_rotated(p) for p in all_paths)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def group_of(path):
    parts = path.split(os.sep)
    if "ramdisk" in parts:
        return "ramdisk"
    if "log" in parts and "data" in parts:
        return "data-log"
    return "other"


def select_files(cfg, state, now):
    """Decide which log files this run should archive, and why."""
    all_paths = expand_sources(cfg["sources"])
    interval = float(cfg["live_snapshot_interval_s"])
    selected = []
    for path in all_paths:
        if is_rotated(path):
            selected.append((path, "rotated"))
            continue
        # A live file that has already rotated once is guaranteed to roll into
        # .1 and be captured there, so snapshot it only occasionally.
        last = state["live_snapshots"].get(path, 0)
        if now - last < interval:
            continue
        selected.append((path, "live"))
    return all_paths, selected


def staged_digests(staging):
    """Digests already sitting in the staging tree from a previous failed push."""
    seen = set()
    for _, _, files in os.walk(staging):
        for name in files:
            if name.endswith(".gz") and "__" in name:
                seen.add(name[:-len(".gz")].rsplit("__", 1)[-1])
    return seen


def staging_has_files(staging):
    for _, _, files in os.walk(staging):
        if files:
            return True
    return False


def trim_staging(cfg):
    """Drop the oldest buffered archives if the local buffer got too big."""
    staging = cfg["staging_dir"]
    cap = int(cfg["max_staging_bytes"])
    entries = []
    total = 0
    for root, _, files in os.walk(staging):
        for name in files:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, path))
            total += st.st_size
    if total <= cap:
        return 0
    entries.sort()
    dropped = 0
    for _, size, path in entries:
        if total <= cap:
            break
        try:
            os.remove(path)
        except OSError:
            continue
        total -= size
        dropped += 1
    log("staging buffer over %d bytes; dropped %d oldest archive(s)" % (cap, dropped))
    return dropped


def stage(cfg, state, selected, now):
    """gzip everything new into the staging tree; return count and bytes."""
    staging = cfg["staging_dir"]
    label = cfg["label"]
    pending = staged_digests(staging)
    count = 0
    total = 0
    for path, reason in selected:
        try:
            digest = sha256_of(path)
        except (IOError, OSError) as exc:
            log("skip %s: %s" % (path, exc))
            continue
        if digest[:12] in pending:
            # Staged by an earlier run whose push failed; it will go out with
            # this run's rsync. Do not stage a second copy.
            continue
        if digest in state["hashes"]:
            if reason == "live":
                # Content unchanged since the last snapshot; reset the clock so
                # an idle log is not rehashed every single run.
                state["live_snapshots"][path] = now
            continue

        base = os.path.basename(path)
        stamp = datetime.fromtimestamp(now)
        rel = os.path.join(label, group_of(path), base, stamp.strftime("%Y-%m-%d"))
        name = "%s__%s__%s.gz" % (base, stamp.strftime("%Y%m%d-%H%M%S"), digest[:12])
        outdir = os.path.join(staging, rel)
        if not os.path.isdir(outdir):
            os.makedirs(outdir)
        outpath = os.path.join(outdir, name)
        try:
            with open(path, "rb") as src, gzip.open(outpath + ".part", "wb", 6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            os.rename(outpath + ".part", outpath)
        except (IOError, OSError) as exc:
            log("stage failed %s: %s" % (path, exc))
            if os.path.exists(outpath + ".part"):
                os.remove(outpath + ".part")
            continue

        state["hashes"][digest] = now
        if reason == "live":
            state["live_snapshots"][path] = now
        count += 1
        total += os.path.getsize(outpath)
    return count, total


def ssh_base(cfg):
    """Build the ssh command prefix, honouring password vs key auth."""
    use_key = cfg["ssh_key"] and os.path.exists(cfg["ssh_key"])
    opts = [
        "ssh", "-p", cfg["ssh_port"],
        "-o", "BatchMode=" + ("yes" if use_key or not cfg["ssh_password"] else "no"),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
    ]
    if use_key:
        opts += ["-i", cfg["ssh_key"], "-o", "IdentitiesOnly=yes"]
        return opts
    if cfg["ssh_password"]:
        return ["sshpass", "-p", cfg["ssh_password"]] + opts
    return opts


def parse_dest(dest):
    if dest.startswith("local:"):
        return "local", None, dest[len("local:"):]
    if dest.startswith("ssh:"):
        rest = dest[len("ssh:"):]
        target, _, path = rest.partition(":")
        return "ssh", target, path
    raise ValueError("dest must start with 'local:' or 'ssh:' (got %r)" % dest)


def run_remote(cfg, command):
    """Run a shell command at the destination, local or remote."""
    kind, target, _ = parse_dest(cfg["dest"])
    if kind == "local":
        argv = ["sh", "-c", command]
    else:
        argv = ssh_base(cfg) + [target, command]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def push(cfg):
    """rsync the staging tree to the destination, then clear staging."""
    kind, target, rpath = parse_dest(cfg["dest"])
    staging = cfg["staging_dir"]
    guard = cfg["require_mount"]
    if guard and not os.path.ismount(guard):
        log("%s is not mounted; refusing to write (staying buffered)" % guard)
        return False
    rc, _, err = run_remote(cfg, "mkdir -p %s" % shlex.quote(rpath))
    if rc != 0:
        log("cannot create remote root: %s" % err.strip())
        return False

    cmd = ["rsync", "-a", "--partial", "--remove-source-files"]
    if kind == "ssh":
        cmd += ["-e", " ".join(shlex.quote(a) for a in ssh_base(cfg))]
        remote = "%s:%s/" % (target, rpath)
    else:
        remote = rpath.rstrip("/") + "/"
    cmd += [staging.rstrip("/") + "/", remote]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if proc.returncode != 0:
        log("rsync failed (%d): %s" % (proc.returncode, err.decode("utf-8", "replace").strip()))
        return False
    # --remove-source-files leaves the directory skeleton behind.
    for root, dirs, files in os.walk(staging, topdown=False):
        if root == staging:
            continue
        if not dirs and not files:
            try:
                os.rmdir(root)
            except OSError:
                pass
    return True


def prune(cfg):
    """Delete oldest archives until the remote store is under max_bytes."""
    _, _, rpath = parse_dest(cfg["dest"])
    cap = int(cfg["max_bytes"])
    script = (
        "set -e; cd %(root)s || exit 0; "
        "total=$(find . -type f -printf '%%s\\n' 2>/dev/null | awk '{s+=$1} END {print s+0}'); "
        "cap=%(cap)d; "
        "echo TOTAL=$total; "
        "if [ \"$total\" -le \"$cap\" ]; then echo PRUNED=0; exit 0; fi; "
        "removed=0; "
        "find . -type f -printf '%%T@ %%s %%p\\n' 2>/dev/null | sort -n | "
        "while read ts sz p; do "
        "  rm -f \"$p\" || true; total=$((total-sz)); removed=$((removed+1)); "
        "  if [ \"$total\" -le \"$cap\" ]; then break; fi; "
        "done; "
        "find . -type d -empty -delete 2>/dev/null || true; "
        "echo PRUNED=done"
    ) % {"root": shlex.quote(rpath), "cap": cap}
    rc, out, err = run_remote(cfg, script)
    if rc != 0:
        log("prune failed: %s" % err.strip())
        return None
    return out.strip().replace("\n", " ")


def gc_state(cfg, state, now):
    ttl = float(cfg["hash_ttl_days"]) * 86400.0
    stale = [h for h, ts in state["hashes"].items() if now - ts > ttl]
    for h in stale:
        del state["hashes"][h]
    return len(stale)


def main():
    cfg = load_config()
    if not cfg["dest"]:
        log("no 'dest' configured in %s -- nothing to do" % CONF_PATH)
        return 2

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        log("another run is in progress; exiting")
        return 0

    now = time.time()
    state = load_state(cfg["state_file"])
    all_paths, selected = select_files(cfg, state, now)
    count, staged_bytes = stage(cfg, state, selected, now)

    if not staging_has_files(cfg["staging_dir"]):
        log("nothing new (%d log files scanned)" % len(all_paths))
        save_state(cfg["state_file"], state)
        return 0

    if not push(cfg):
        # Leave the staged files in place and do not persist the new hashes, so
        # the next run retries the push instead of re-gzipping the same content.
        # The snapshot clocks are only rate limiters, though, and must survive a
        # failed push -- otherwise every run would re-snapshot every growing log
        # and the buffer would balloon during an outage.
        trim_staging(cfg)
        persisted = load_state(cfg["state_file"])
        persisted["live_snapshots"] = state["live_snapshots"]
        save_state(cfg["state_file"], persisted)
        log("push failed; %d file(s) staged, buffer awaiting the next run" % count)
        return 1

    dropped = gc_state(cfg, state, now)
    save_state(cfg["state_file"], state)
    pruned = prune(cfg)
    log("archived %d file(s), %.1f MiB compressed, %d scanned, %d hash(es) expired; %s"
        % (count, staged_bytes / 1048576.0, len(all_paths), dropped, pruned or "prune skipped"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
