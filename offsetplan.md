# Implementation Plan: `current_offset` for Vehicle (EV) Templates

## Overview

A `current_offset` (float, Amperes, default `0`) is added to the vehicle profile (`EvTemplateData`).  
It is applied to the EVSE command current immediately before the hardware write, **only when the intended current is non-zero**.  
The algorithm, load management, energy logging, and phase-switch logic are completely unaffected — they always reason about the unmodified intended current.

**Primary use case:** a vehicle consistently draws less than commanded (e.g. commands 16 A, vehicle draws 14 A). Set `current_offset = +2` so the EVSE is commanded 16 A and the vehicle draws the intended 14 A (or conversely: set offset to bridge the gap in the other direction).

---

## To-Do List

- [x] **1.** Add `current_offset` field to `EvTemplateData`
- [x] **2.** Apply offset in `Process._update_state()` before hardware write
- [x] **3.** Write unit tests for `_update_state()` offset logic
- [x] **4.** Add `current_offset` input to the EV template settings UI *(done in `openwb-ui-settings/src/views/VehicleConfiguration.vue`)*
- [ ] **5.** Manual smoke-test on a live system

---

## Change 1 — `packages/control/ev/ev_template.py`

### What
Add `current_offset: float = 0` to `EvTemplateData`. Because it is a plain dataclass field with a default value, existing stored configs that do not contain this key will automatically get `0` when deserialised — no migration needed.

### Before

```python
@dataclass
class EvTemplateData:
    dc_min_current: int = 20
    dc_max_current: int = 150
    id: int = 0
    name: str = "Fahrzeug-Profil"
    max_current_multi_phases: int = 16
    max_phases: int = 3
    prevent_phase_switch: bool = False
    prevent_charge_stop: bool = False
    control_pilot_interruption: bool = False
    control_pilot_interruption_duration: int = 4
    average_consump: float = 17000
    min_current: int = 6
    max_current_single_phase: int = 16
    battery_capacity: float = 82000
    efficiency: float = 90
    nominal_difference: float = 1
    keep_charge_active_duration: int = 40
    bidi: bool = False
```

### After

```python
@dataclass
class EvTemplateData:
    dc_min_current: int = 20
    dc_max_current: int = 150
    id: int = 0
    name: str = "Fahrzeug-Profil"
    max_current_multi_phases: int = 16
    max_phases: int = 3
    prevent_phase_switch: bool = False
    prevent_charge_stop: bool = False
    control_pilot_interruption: bool = False
    control_pilot_interruption_duration: int = 4
    average_consump: float = 17000
    min_current: int = 6
    max_current_single_phase: int = 16
    battery_capacity: float = 82000
    efficiency: float = 90
    nominal_difference: float = 1
    keep_charge_active_duration: int = 40
    bidi: bool = False
    current_offset: float = 0
```

### Exact diff

```
    bidi: bool = False
+   current_offset: float = 0
```

### Persistence
`EvTemplate.data` has `metadata={"topic": "config"}` which means the full `EvTemplateData` dict is published to and subscribed from MQTT topic `openWB/vehicle/{id}/ev_template/config`. No additional code is needed — the new field is included automatically in `dataclasses.asdict()` serialisation and populated with the default `0` on deserialisation when the key is absent.

---

## Change 2 — `packages/control/process.py`

### What
In `Process._update_state()`, after all guards that can force `current = 0` have run, apply the offset and re-clamp only to the chargepoint hardware ceiling.

**Why here (not earlier):**
- Load management, algorithm and energy logging see the *intended* current, not the inflated/deflated EVSE command.
- All zero-forcing guards (phase switch, `PERFORMING_PHASE_SWITCH`) run first, so the `if current != 0` condition naturally respects "0 A stays 0 A".
- Only `check_cp_max_current()` is re-run (not the full EV min/max clamp): the EV limits were enforced during the prepare phase; only the hardware ceiling can be newly violated by a positive offset.
- `max(current, min_current)` prevents a large negative offset from accidentally producing a sub-minimum value (which would be semantically wrong — the intent was to charge, just at a lower current).

### Before (`_update_state`, last section only)

```python
        # Wenn ein EV zugeordnet ist und die Phasenumschaltung aktiv ist, darf kein Strom gesetzt werden.
        if chargepoint.data.control_parameter.state == ChargepointState.PERFORMING_PHASE_SWITCH:
            current = 0

        chargepoint.data.set.current = current
        if chargepoint.data.get.plug_state:
            log.info(f"LP{chargepoint.num}: set current {current} A, "
                     f"state {ChargepointState(chargepoint.data.control_parameter.state).name}")
```

### After

```python
        # Wenn ein EV zugeordnet ist und die Phasenumschaltung aktiv ist, darf kein Strom gesetzt werden.
        if chargepoint.data.control_parameter.state == ChargepointState.PERFORMING_PHASE_SWITCH:
            current = 0

        # Vehicle current offset: compensates for vehicles that draw less/more than commanded.
        # Applied only when charging is intended (current != 0) so all zero-forcing guards above
        # are respected. Re-clamp to CP hardware ceiling; floor to min_current to avoid
        # a large negative offset accidentally producing a sub-minimum command.
        if current != 0:
            offset = charging_ev.ev_template.data.current_offset
            if offset != 0:
                current = current + offset
                current = max(current, chargepoint.data.control_parameter.min_current)
                current = chargepoint.check_cp_max_current(current, chargepoint.data.control_parameter.phases)
                current = round(current, 2)

        chargepoint.data.set.current = current
        if chargepoint.data.get.plug_state:
            log.info(f"LP{chargepoint.num}: set current {current} A "
                     f"(offset {charging_ev.ev_template.data.current_offset:+.2f} A), "
                     f"state {ChargepointState(chargepoint.data.control_parameter.state).name}")
```

> **Note:** The `log.info` line is updated to include the offset value so it is visible in the log without any extra searching.

### Exact diff

```diff
         if chargepoint.data.control_parameter.state == ChargepointState.PERFORMING_PHASE_SWITCH:
             current = 0
 
+        # Vehicle current offset: compensates for vehicles that draw less/more than commanded.
+        # Applied only when charging is intended (current != 0) so all zero-forcing guards above
+        # are respected. Re-clamp to CP hardware ceiling; floor to min_current to avoid
+        # a large negative offset accidentally producing a sub-minimum command.
+        if current != 0:
+            offset = charging_ev.ev_template.data.current_offset
+            if offset != 0:
+                current = current + offset
+                current = max(current, chargepoint.data.control_parameter.min_current)
+                current = chargepoint.check_cp_max_current(current, chargepoint.data.control_parameter.phases)
+                current = round(current, 2)
+
         chargepoint.data.set.current = current
         if chargepoint.data.get.plug_state:
-            log.info(f"LP{chargepoint.num}: set current {current} A, "
-                     f"state {ChargepointState(chargepoint.data.control_parameter.state).name}")
+            log.info(f"LP{chargepoint.num}: set current {current} A "
+                     f"(offset {charging_ev.ev_template.data.current_offset:+.2f} A), "
+                     f"state {ChargepointState(chargepoint.data.control_parameter.state).name}")
```

---

## Change 3 — New test file `packages/control/process_test.py`

Create this file. It follows the same conventions as `packages/control/chargepoint/chargepoint_test.py`.

### Test cases explained

| # | Scenario | set_current | offset | CP max | min | Expected EVSE command |
|---|---|---|---|---|---|---|
| 1 | No offset → pass-through | 10 A | 0 | 32 A | 6 A | 10 A |
| 2 | Positive offset → raised | 10 A | +2 | 32 A | 6 A | 12 A |
| 3 | Negative offset → lowered | 10 A | −2 | 32 A | 6 A | 8 A |
| 4 | Negative offset → floored at min_current | 7 A | −4 | 32 A | 6 A | 6 A (7−4=3 < 6) |
| 5 | Positive offset → clamped to CP max | 30 A | +5 | 32 A | 6 A | 32 A (30+5=35 > 32) |
| 6 | current=0 (no charge intended) → offset never applied | 0 A | +5 | 32 A | 6 A | 0 A |
| 7 | PERFORMING_PHASE_SWITCH → guard forces 0, offset not applied | 10 A | +3 | 32 A | 6 A | 0 A |

### Full test file

```python
from dataclasses import dataclass
from unittest.mock import Mock
import pytest

from control import data
from control.chargepoint.chargepoint import Chargepoint
from control.chargepoint.chargepoint_state import ChargepointState
from control.chargepoint.chargepoint_template import CpTemplate
from control.ev.ev import Ev
from control.ev.ev_template import EvTemplate
from control.process import Process


@pytest.fixture()
def mock_data() -> None:
    data.data_init(Mock())


@dataclass
class UpdateStateParams:
    name: str
    set_current: float
    current_offset: float
    cp_max_current: int
    min_current: int
    phases: int
    chargepoint_state: ChargepointState
    expected_current: float


update_state_params = [
    UpdateStateParams(
        name="no offset - current unchanged",
        set_current=10, current_offset=0, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=10,
    ),
    UpdateStateParams(
        name="positive offset - current raised",
        set_current=10, current_offset=2, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=12,
    ),
    UpdateStateParams(
        name="negative offset - current lowered",
        set_current=10, current_offset=-2, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=8,
    ),
    UpdateStateParams(
        name="negative offset - floored at min_current",
        set_current=7, current_offset=-4, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=6,  # 7 - 4 = 3 < min_current=6
    ),
    UpdateStateParams(
        name="positive offset - clamped to CP max",
        set_current=30, current_offset=5, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=32,  # 30 + 5 = 35 > cp_max=32
    ),
    UpdateStateParams(
        name="current is 0 - offset never applied",
        set_current=0, current_offset=5, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=0,
    ),
    UpdateStateParams(
        name="PERFORMING_PHASE_SWITCH forces 0 - offset not applied",
        set_current=10, current_offset=3, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.PERFORMING_PHASE_SWITCH,
        expected_current=0,
    ),
]


@pytest.mark.parametrize("params", update_state_params, ids=[p.name for p in update_state_params])
def test_update_state_current_offset(params: UpdateStateParams, mock_data, monkeypatch):
    # --- setup chargepoint ---
    cp = Chargepoint(0, None)
    cp.template = CpTemplate()
    cp.template.data.max_current_multi_phases = params.cp_max_current
    cp.template.data.max_current_single_phase = params.cp_max_current
    cp.data.set.current = params.set_current
    cp.data.control_parameter.phases = params.phases
    cp.data.control_parameter.state = params.chargepoint_state
    cp.data.control_parameter.min_current = params.min_current
    cp.data.get.plug_state = True

    # --- setup EV with offset ---
    ev = Ev(0)
    ev.ev_template = EvTemplate()
    ev.ev_template.data.current_offset = params.current_offset
    ev.ev_template.data.prevent_phase_switch = False
    ev.ev_template.data.min_current = params.min_current
    ev.ev_template.data.max_current_multi_phases = params.cp_max_current
    ev.ev_template.data.max_current_single_phase = params.cp_max_current
    cp.data.set.charging_ev_data = ev

    # Mock the initial check_min_max_current safety re-clamp so it passes
    # the current through unchanged, isolating the offset logic under test.
    monkeypatch.setattr(
        Ev, "check_min_max_current",
        Mock(return_value=(params.set_current, None))
    )

    # --- run ---
    Process()._update_state(cp)

    # --- assert ---
    assert cp.data.set.current == params.expected_current
```

### Running the tests

```powershell
cd C:\Users\Toby\Documents\GitHub\openWB\packages
python -m pytest control/process_test.py -v
```

Expected output:
```
PASSED control/process_test.py::test_update_state_current_offset[no offset - current unchanged]
PASSED control/process_test.py::test_update_state_current_offset[positive offset - current raised]
PASSED control/process_test.py::test_update_state_current_offset[negative offset - current lowered]
PASSED control/process_test.py::test_update_state_current_offset[negative offset - floored at min_current]
PASSED control/process_test.py::test_update_state_current_offset[positive offset - clamped to CP max]
PASSED control/process_test.py::test_update_state_current_offset[current is 0 - offset never applied]
PASSED control/process_test.py::test_update_state_current_offset[PERFORMING_PHASE_SWITCH forces 0 - offset not applied]
```

Also run the existing test suite to confirm nothing is broken:
```powershell
python -m pytest control/ -v --tb=short
```

---

## Change 4 — UI: EV template settings page

The EV template config is edited at `web/settings/` (Vue component for the vehicle profile).  
Locate the component that renders EV template fields (search for `min_current` in the web/settings source) and add:

```html
<openwb-base-range-input
    title="Strom-Offset"
    :subtitle="'Korrektur des EVSE-Strombefehls in A (positiv: höher befehlen, negativ: niedriger). Standard: 0'"
    v-model="evTemplateData.current_offset"
    :min="-10"
    :max="10"
    :step="0.5"
    unit="A"
/>
```

Place it after the `nominal_difference` field for logical grouping.

The MQTT publish path used by the UI is already `openWB/vehicle/{id}/ev_template/config` (the whole config dict), so the new field is transmitted automatically once it exists in the UI model.

---

## Examples

### Example 1: Vehicle draws 2 A less than commanded (most common)

A vehicle commanded 16 A consistently charges at ~14 A.  
Set `current_offset = +2`:

| Cycle | LM plans for | EVSE commanded | Vehicle draws |
|---|---|---|---|
| Before offset | 16 A | 16 A | 14 A |
| After offset | 16 A | 18 A | ~16 A ✓ |

Load management still reserves headroom for 16 A (the intended value). Only the EVSE command changes.

### Example 2: Negative offset (overcurrent protection)

A vehicle charged at 32 A (CP max) generates heat on a long cable.  
Set `current_offset = −2` to reduce the EVSE command by 2 A without changing any charge template setting:

| LM plans for | EVSE commanded | Vehicle draws |
|---|---|---|
| 32 A | 30 A | ~30 A |

### Example 3: Offset = 0 (default, no change in behaviour)

Offset of `0` takes the `if offset != 0:` early-exit path — no arithmetic performed, identical behaviour to the codebase before this change.

### Example 4: Zero-current guard

Phase switch is in progress → `PERFORMING_PHASE_SWITCH` guard sets `current = 0`.  
The offset block checks `if current != 0:` — false — so `0` is written to the EVSE unchanged. Correct: the vehicle must not charge during a phase switch regardless of any offset.

---

## Constraints Checklist

| Constraint | How it is handled |
|---|---|
| `current == 0` → never charge | Guard `if current != 0:` — offset block is skipped entirely |
| Positive offset exceeding CP hardware max | `check_cp_max_current()` re-clamp after offset |
| Negative offset below EV `min_current` | `max(current, min_current)` floor after offset |
| Load management accuracy | Offset not applied until after LM has run — LM always sees intended current |
| Phase switch zero-forcing | Phase switch guard runs *before* offset block |
| `prevent_phase_switch` zero-forcing | That guard also runs *before* offset block |
| MQTT `set/current` reflects EVSE command | `chargepoint.data.set.current` is set to the offset value — correct for diagnostics |
| `current_prev` comparison (CP interruption, charge-start detection) | `current_prev` is set from `current` in `remember_previous_values()`, called after `_update_state()` — so `current_prev` also holds the offset value; comparisons remain consistent |

---

## File Summary

| File | Action | Lines changed |
|---|---|---|
| `packages/control/ev/ev_template.py` | Add field `current_offset: float = 0` to `EvTemplateData` | +1 |
| `packages/control/process.py` | Apply offset + re-clamp in `_update_state()`, update log line | +9, ~2 |
| `packages/control/process_test.py` | New file — 7 parametrised test cases | new |
| `web/settings/` EV template component | Add range input for `current_offset` | +~10 |
