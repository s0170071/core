import argparse
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import tarfile


OFFSET_CONFIG = Path("data/config/pv_export_offsets.json")
FEBRUARY_START = "20260218"
OUTAGE_FIRST_DAY = "20260919"
OUTAGE_LAST_DAY = "20260929"
OUTAGE_DAYS = 11
OUTAGE_BACKFILL_WH = 173557.0
SPIKE_TIMESTAMP = 1790766601
SEPTEMBER_MONTH = "202609"
SEPTEMBER_BASELINE_DAY = "20260918"
TODAY = "20260930"


def read_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json_atomic(path, content):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    temporary = path.with_name(path.name + ".repair-tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(content, file, ensure_ascii=False, indent=4)
        file.write("\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def normalized_pv_value(entry, key, offset):
    value = entry["pv"][key]["exported"]
    adjusted = value - offset
    if adjusted < 0:
        raise ValueError(f"PV {key} counter {value} is below configured offset {offset}")
    return round(adjusted, 3)


def offset_entries(content, offset, after_date):
    changed = 0
    for entry in content["entries"]:
        if str(entry.get("date", "")) < after_date:
            continue
        pv = entry.get("pv", {})
        for key in ("all", "pv13"):
            if key in pv and "exported" in pv[key]:
                pv[key]["exported"] = normalized_pv_value(entry, key, offset)
        changed += 1
    return changed


def set_entry_time(entry, day, clock):
    timestamp = datetime.datetime.strptime(f"{day} {clock}", "%Y%m%d %H:%M").timestamp()
    entry["timestamp"] = int(timestamp)
    entry["date"] = clock
    return entry


def create_outage_logs(root, base_entry, names):
    daily_dir = root / "data" / "daily_log"
    monthly_path = root / "data" / "monthly_log" / f"{SEPTEMBER_MONTH}.json"
    monthly = read_json(monthly_path)
    existing_monthly_dates = {str(entry.get("date", "")) for entry in monthly["entries"]}
    days = [
        (datetime.datetime.strptime(OUTAGE_FIRST_DAY, "%Y%m%d") + datetime.timedelta(days=index)).strftime("%Y%m%d")
        for index in range(OUTAGE_DAYS)
    ]
    if days[-1] != OUTAGE_LAST_DAY:
        raise ValueError("Outage date range is inconsistent")
    for day in days:
        if (daily_dir / f"{day}.json").exists() or day in existing_monthly_dates:
            raise FileExistsError(f"History for {day} already exists; refusing to overwrite it")

    base_pv13 = base_entry["pv"]["pv13"]["exported"]
    base_all = base_entry["pv"]["all"]["exported"]
    per_day = round(OUTAGE_BACKFILL_WH / OUTAGE_DAYS, 3)
    increments = [per_day] * (OUTAGE_DAYS - 1)
    increments.append(round(OUTAGE_BACKFILL_WH - sum(increments), 3))
    current_pv13 = base_pv13
    current_all = base_all
    monthly_entries = []

    for day, increment in zip(days, increments):
        start = set_entry_time(deepcopy(base_entry), day, "00:00")
        start["pv"]["pv13"]["exported"] = round(current_pv13, 3)
        start["pv"]["all"]["exported"] = round(current_all, 3)

        end = set_entry_time(deepcopy(start), day, "23:55")
        current_pv13 = round(current_pv13 + increment, 3)
        current_all = round(current_all + increment, 3)
        end["pv"]["pv13"]["exported"] = current_pv13
        end["pv"]["all"]["exported"] = current_all

        write_json_atomic(daily_dir / f"{day}.json", {"entries": [start, end], "names": names})
        monthly_snapshot = deepcopy(start)
        monthly_snapshot["date"] = day
        monthly_entries.append(monthly_snapshot)
        base_entry = end

    monthly["entries"].extend(monthly_entries)
    write_json_atomic(monthly_path, monthly)
    return {
        "days": days,
        "per_day_wh": increments,
        "allocated_wh": round(sum(increments), 3),
        "last_pv13": current_pv13,
        "last_all": current_all,
    }


def find_spike(entries):
    spikes = []
    for previous, current in zip(entries, entries[1:]):
        previous_value = previous["pv"]["pv13"]["exported"]
        current_value = current["pv"]["pv13"]["exported"]
        delta = round(current_value - previous_value, 3)
        if delta > 100000:
            spikes.append((current["timestamp"], delta))
    if spikes != [(SPIKE_TIMESTAMP, OUTAGE_BACKFILL_WH)]:
        raise ValueError(f"Unexpected PV13 spike in {TODAY}: {spikes}")
    return spikes[0]


def prepare(root, offset_config):
    offsets = read_json(offset_config)
    offset = float(offsets["pv13"])
    daily_dir = root / "data" / "daily_log"
    monthly_dir = root / "data" / "monthly_log"
    february = read_json(daily_dir / f"{FEBRUARY_START}.json")
    february_value = february["entries"][0]["pv"]["pv13"]["exported"]
    if february_value <= offset:
        raise ValueError("February history appears already normalized; refusing to apply the offset twice")

    today = read_json(daily_dir / f"{TODAY}.json")
    spike_timestamp, spike_wh = find_spike(today["entries"])
    missing_days = [
        (datetime.datetime.strptime(OUTAGE_FIRST_DAY, "%Y%m%d") + datetime.timedelta(days=index)).strftime("%Y%m%d")
        for index in range(OUTAGE_DAYS)
    ]
    for day in missing_days:
        if (daily_dir / f"{day}.json").exists():
            raise FileExistsError(f"Daily history for {day} already exists; refusing to overwrite it")

    daily_files = sorted(path for path in daily_dir.glob("*.json")
                         if path.stem.isdigit() and path.stem >= FEBRUARY_START)
    monthly_files = sorted(path for path in monthly_dir.glob("*.json") if path.stem.isdigit())
    daily_entries = sum(len(read_json(path).get("entries", [])) for path in daily_files)
    monthly_entries = sum(len(read_json(path).get("entries", [])) for path in monthly_files)
    september = read_json(monthly_dir / f"{SEPTEMBER_MONTH}.json")
    if not september["entries"] or str(september["entries"][-1].get("date")) != SEPTEMBER_BASELINE_DAY:
        raise ValueError("September monthly history no longer ends at the expected Sep 18 snapshot")

    return {
        "offset_wh": offset,
        "february_first_pv13_wh": february_value,
        "spike_timestamp": spike_timestamp,
        "outage_backfill_wh": spike_wh,
        "outage_days": missing_days,
        "daily_files_to_normalize": len(daily_files),
        "daily_entries_to_normalize": daily_entries,
        "monthly_files_to_normalize": len(monthly_files),
        "monthly_entries_to_normalize": monthly_entries,
    }


def apply(root, summary, backup_path):
    daily_dir = root / "data" / "daily_log"
    monthly_dir = root / "data" / "monthly_log"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        raise FileExistsError(f"Backup already exists: {backup_path}")
    with tarfile.open(backup_path, "x:gz") as backup:
        backup.add(daily_dir, arcname="daily_log")
        backup.add(monthly_dir, arcname="monthly_log")

    offset = summary["offset_wh"]
    for directory in (daily_dir, monthly_dir):
        for path in sorted(directory.glob("*.json")):
            if not path.stem.isdigit() or path.stem < FEBRUARY_START:
                continue
            content = read_json(path)
            if directory == daily_dir:
                for entry in content["entries"]:
                    pv = entry.get("pv", {})
                    for key in ("all", "pv13"):
                        if key in pv and "exported" in pv[key]:
                            pv[key]["exported"] = normalized_pv_value(entry, key, offset)
            else:
                offset_entries(content, offset, FEBRUARY_START)
            write_json_atomic(path, content)

    baseline_file = daily_dir / f"{SEPTEMBER_BASELINE_DAY}.json"
    today_file = daily_dir / f"{TODAY}.json"
    baseline = read_json(baseline_file)
    today = read_json(today_file)
    baseline_entry = baseline["entries"][-1]
    names = today.get("names", {})

    # Move the post-outage counter level to the start of Sep 30 so the day's series stays continuous.
    for entry in today["entries"]:
        if entry["timestamp"] >= summary["spike_timestamp"]:
            break
        entry["pv"]["pv13"]["exported"] = round(entry["pv"]["pv13"]["exported"] + OUTAGE_BACKFILL_WH, 3)
        entry["pv"]["all"]["exported"] = round(entry["pv"]["all"]["exported"] + OUTAGE_BACKFILL_WH, 3)
    write_json_atomic(today_file, today)

    outage_summary = create_outage_logs(root, baseline_entry, names)
    summary["synthetic_outage"] = outage_summary
    summary["backup"] = str(backup_path)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--offset-config", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    offset_config = args.offset_config or root / OFFSET_CONFIG
    summary = prepare(root, offset_config)
    if args.apply:
        summary = apply(root, summary, args.backup)
        print("APPLIED")
    else:
        print("DRY RUN ONLY; no files changed")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()