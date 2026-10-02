import json
import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)
_OFFSET_FILE = Path(__file__).resolve().parents[2] / "data" / "config" / "pv_export_offsets.json"


@lru_cache(maxsize=1)
def _load_offsets() -> dict[str, float]:
    try:
        with _OFFSET_FILE.open(encoding="utf-8") as offset_file:
            offsets = json.load(offset_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        log.error("Could not read PV export offsets from %s: %s", _OFFSET_FILE, error)
        return {}

    if not isinstance(offsets, dict):
        log.error("PV export offsets in %s must be a JSON object", _OFFSET_FILE)
        return {}

    result = {}
    for module, offset in offsets.items():
        try:
            value = float(offset)
        except (TypeError, ValueError):
            log.error("Ignoring invalid PV export offset for %s: %r", module, offset)
            continue
        if value < 0:
            log.error("Ignoring negative PV export offset for %s: %s", module, value)
            continue
        result[str(module)] = value
    return result


def get_pv_export_offset(module_num: int) -> float:
    return _load_offsets().get(f"pv{module_num}", 0.0)