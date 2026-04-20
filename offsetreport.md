# openWB2 — Vehicle Current Offset Feature: Analysis Report

## Purpose

This document analyses the openWB2 codebase to identify every data structure, code path, and constraint point involved in setting the EV charging current, so that a **`current_offset`** parameter can be introduced to adjust the actual current delivered to a vehicle closer to the intended value (e.g. to compensate for a vehicle that consistently draws less than commanded).

---

## 1. Key Data Structures

### 1.1 `EvTemplateData` — Vehicle Profile
**File:** `packages/control/ev/ev_template.py`

| Field | Type | Default | Role |
|---|---|---|---|
| `min_current` | `int` | `6` | Minimum AC charge current (A); lower bound in `check_min_max_current()` |
| `max_current_single_phase` | `int` | `16` | Maximum AC current on 1 phase |
| `max_current_multi_phases` | `int` | `16` | Maximum AC current on 2–3 phases |
| `dc_min_current` | `int` | `20` | Minimum DC charge current (A) |
| `dc_max_current` | `int` | `150` | Maximum DC charge current (A) |
| `nominal_difference` | `float` | `1` | Allowed deviation (A) between commanded and measured current; used in phase-switch decisions and the "less charging" load-management correction |
| `keep_charge_active_duration` | `int` | `40` | How long (s) to keep charging active during phase switch |
| `prevent_charge_stop` | `bool` | `False` | Suppresses automatic stop when surplus disappears |
| `prevent_phase_switch` | `bool` | `False` | Locks phase count after first kWh |
| `control_pilot_interruption` | `bool` | `False` | Whether CP interruption is needed at charge start |
| `bidi` | `bool` | `False` | Vehicle supports bidirectional charging |

**→ `current_offset` should be added here as `current_offset: float = 0`.**

---

### 1.2 `CpTemplateData` — Chargepoint Profile
**File:** `packages/control/chargepoint/chargepoint_template.py`

| Field | Type | Default | Role |
|---|---|---|---|
| `max_current_single_phase` | `int` | `32` | Hard ceiling for 1-phase AC current at the chargepoint hardware level |
| `max_current_multi_phases` | `int` | `32` | Hard ceiling for 3-phase AC current |
| `dc_max_current` | `float` | `435` | Hard ceiling for DC current |

These are **separate** from EV-template limits and represent hardware constraints of the charging station, not the vehicle.

---

### 1.3 `ControlParameter` — Per-cycle algorithm state
**File:** `packages/control/chargepoint/control_parameter.py`

| Field | Type | Default | Role |
|---|---|---|---|
| `min_current` | `int` | `6` | Copied from `ev_template.data.min_current` when charging starts |
| `required_current` | `float` | `0` | Target single-phase current for this algorithm cycle |
| `required_currents` | `List[float]` | `[0,0,0]` | Per-EVU-phase target currents |
| `phases` | `int` | `0` | Number of phases selected for this cycle |

---

### 1.4 `ChargepointData.Set` — What gets sent to hardware
**File:** `packages/control/chargepoint/chargepoint_data.py`

| Field | MQTT topic | Role |
|---|---|---|
| `current` | `openWB/set/chargepoint/{id}/set/current` | **Final commanded current (A)**; this is what `chargepoint_module.set_current()` receives |
| `target_current` | *(not published)* | Intermediate result stored between algorithm stages |
| `current_prev` | `openWB/set/chargepoint/{id}/set/current_prev` | Previous cycle's `current`; used to detect charge start/stop |
| `required_power` | `openWB/set/chargepoint/{id}/set/required_power` | Calculated as `sum(required_currents × voltages)`; used in load management |

---

### 1.5 Charge Template current fields
**File:** `packages/control/ev/charge_template.py`

| Mode | Field | Default | Meaning |
|---|---|---|---|
| Instant charging | `InstantCharging.current` | `16` A | User-configured target current |
| Eco charging | `EcoCharging.current` | `6` A | Target current for eco mode |
| PV charging | `PvCharging.min_current` | `0` A | Minimum current when PV mode active |
| PV charging | `PvCharging.min_soc_current` | `10` A | Minimum current when SoC threshold active |
| Time charging | `TimeChargingPlan.current` | per plan | Current during the plan's time window |
| Scheduled charging | Derived | — | Calculated to hit SoC target by deadline |

---

## 2. Full Current Chain: Algorithm Cycle → Hardware

### Step 0 — Timer trigger (`main.py`)
`schedule` fires every 10 seconds → `handler10Sec()` → `handler_with_control_interval()` → `control.calc_current()` + `proc.process_algorithm_results()`

---

### Step 1 — Prepare phase: compute `required_current` per chargepoint
**File:** `packages/control/prepare.py` → `setup_algorithm()` → `cp.update(ev_list)`
**File:** `packages/control/chargepoint/chargepoint.py` → `Chargepoint.update()`

```
charging_ev.get_required_current(...)
  → charge_template.instant_charging / pv_charging / time_charging / eco_charging / scheduled_charging
  → returns: state, message, submode, required_current, template_phases
```

The `get_required_current` method (`ev.py`) routes to one of the charge template methods which return the raw target current in Amperes (e.g. `InstantCharging.current = 16`).

Then immediately:
```python
# Line ~656 chargepoint.py update()
required_current = self.chargepoint_module.add_conversion_loss_to_current(required_current)
# (Only DC adapter overrides this; default returns current unchanged)

required_current = self.check_min_max_current(required_current, self.data.control_parameter.phases)
# → calls ev.check_min_max_current() → clamps to [ev_template.min_current … ev_template.max_current_*]
# → then calls check_cp_max_current() → clamps to [_, cp_template.max_current_*]

required_current = self.chargepoint_module.add_conversion_loss_to_current(required_current)
# Second call: applied again post-clamping (relevant for DC adapter efficiency compensation)

self.set_required_currents(required_current)
# Stores required_current in control_parameter.required_current and required_currents[phase_indices]
# Also calculates required_power = sum(required_currents × voltages)
```

---

### Step 2 — Algorithm stage 1: Minimum current
**File:** `packages/control/algorithm/min_current.py` → `MinCurrent.set_min_current()`

For each chargepoint with a required current, load management checks whether `min_current` fits within available headroom on all counters. If yes:
```python
common.set_current_counterdiff(target, control_parameter.min_current, cp)
# Sets cp.data.set.current = min_current
```
If load management cannot accommodate even min current: `cp.data.set.current = 0`

---

### Step 3 — Algorithm stage 2: Additional current (toward required)
**File:** `packages/control/algorithm/additional_current.py` → `AdditionalCurrent.set_additional_current()`

Raises current from `min_current` toward `required_current`, subject to load management headroom:
```python
common.set_current_counterdiff(cp.data.control_parameter.min_current, current, cp)
# Sets cp.data.set.current = current  (somewhere between min_current and required_current)
```

---

### Step 4 — Algorithm stage 3: PV surplus current
**File:** `packages/control/algorithm/surplus_controlled.py` → `SurplusControlled.set_surplus_current()`

For PV/eco modes, adjusts current upward based on available surplus power. Also contains an existing `evse_current`-based correction:
```python
# surplus_controlled.py ~line 190
if evse_current and chargepoint.data.set.current != chargepoint.data.set.current_prev:
    offset = evse_current - get_medium_charging_current(chargepoint.data.get.currents)
    current_with_offset = chargepoint.data.set.current + offset
    current = min(current_with_offset, chargepoint.data.control_parameter.required_current)
    chargepoint.data.set.current = current
```
This is an **existing internal offset mechanism** that compensates for the gap between EVSE-reported current and actual measured current, but only when the current changed in the previous cycle.

For PV-mode max expansion, `set_required_current_to_max()` uses `ev_template.data.max_current_single_phase / max_current_multi_phases` as the ceiling.

---

### Step 5 — Process phase: final safety check and hardware call
**File:** `packages/control/process.py` → `Process.process_algorithm_results()` → `_update_state(cp)` → `_start_charging(cp)`

```python
# _update_state()
current = round(chargepoint.data.set.current, 2)
current = chargepoint.check_min_max_current(current, chargepoint.data.control_parameter.phases)
# Safety re-check of min/max before sending to hardware

chargepoint.data.set.current = current

# _start_charging()
return Thread(target=chargepoint.chargepoint_module.set_current,
              args=(chargepoint.data.set.current,), ...)
```

---

### Step 6 — Hardware module: `set_current(current)`
**Abstract interface:** `packages/modules/common/abstract_chargepoint.py`

Implementations:
| Module | Mechanism |
|---|---|
| `openwb_pro` | HTTP POST `{'ampere': current}` to `connect.php` |
| `mqtt` | `Pub().pub(f"openWB/mqtt/chargepoint/{id}/set/current", current)` |
| `openwb_dc_adapter` | Converts via `subtract_conversion_loss_from_current(current)` then POSTs power |
| `external_openwb` | MQTT publish |
| `openwb_series2_satellit` | Via internal chargepoint handler |
| `smartwb` | HTTP POST |

---

## 3. All Current Constraint / Clamping Points

| Location | File | What it enforces |
|---|---|---|
| `ev.check_min_max_current()` | `ev/ev.py` | Clamps to `[ev_template.min_current … ev_template.max_current_*]` per phase count and AC/DC type |
| `chargepoint.check_cp_max_current()` | `chargepoint/chargepoint.py` | Clamps to chargepoint template max |
| `chargepoint.check_min_max_current()` | `chargepoint/chargepoint.py` | Calls both above; also handles bidi discharge limits |
| `algorithm._check_auto_phase_switch_delay()` | `algorithm/algorithm.py` line 84 | Re-clamps `required_current` after phase decision |
| `min_current.set_min_current()` | `algorithm/min_current.py` | Cannot go below `min_current`, falls to 0 if LM headroom insufficient |
| `additional_current.set_additional_current()` | `algorithm/additional_current.py` | Cannot exceed `required_current` |
| `surplus_controlled.set_required_current_to_max()` | `algorithm/surplus_controlled.py` | PV mode ceiling from `ev_template.max_current_*` |
| `process._update_state()` | `process.py` line 118 | Final safety clamp using `check_min_max_current()` before hardware call |
| `process._update_state()` phase switch guard | `process.py` | Forces `current = 0` during phase switch |

---

## 4. Existing Offset / Correction Mechanisms

1. **`nominal_difference`** (`ev_template.data.nominal_difference`, default `1` A): Not a current correction — it is a tolerance window used in two places:
   - Phase-switch condition: `_check_phase_switch_conditions()` in `ev.py` to judge whether the vehicle is charging within range
   - `consider_less_charging_chargepoint_in_loadmanagement()` in `common.py`: if actual current is more than `nominal_difference` below set current for >60 s, the LM uses the actual (lower) current instead, freeing up headroom for other chargepoints

2. **`add_conversion_loss_to_current()` / `subtract_conversion_loss_from_current()`**: Used by the DC adapter only. Applied at two points in `chargepoint.update()` to convert from AC-equivalent to actual DC current, accounting for inverter efficiency.

3. **EVSE-based dynamic offset** (`surplus_controlled.py` ~line 190): Applies `offset = evse_current - actual_measured_current` to the set current when transitioning, capped at `required_current`. This only runs during PV surplus mode when the current changes between cycles.

---

## 5. Where to Insert the Offset Parameter

### Data structure: `EvTemplateData`

**Add to `EvTemplateData`** (`packages/control/ev/ev_template.py`):
```python
current_offset: float = 0   # Amperes to add to the commanded current (can be negative)
```

---

### Placement decision: before hardware write, not before `check_min_max_current()`

The offset must be applied **as late as possible** — in `Process._update_state()` (`packages/control/process.py`), after all zero-forcing guards have run, immediately before `chargepoint.data.set.current = current`.

**Why not early (before `check_min_max_current()` in `Chargepoint.update()`):**

The core use case is: a vehicle consistently draws less current than commanded. The offset is +N A so that commanding (intended + N) A makes the vehicle draw (intended) A. Consider intended = 16 A, offset = +2 A:

| Insertion point | LM reserves headroom for | Vehicle draws | LM accurate? |
|---|---|---|---|
| Early (before clamp) | 18 A (offset value) | 16 A | No — over-reserves, restricts other chargepoints unnecessarily |
| Late (before hardware) | 16 A (intended value) | 16 A | Yes |

The algorithm, load management, energy logging, and phase-switch logic should all reason about the **intended** current. The offset is purely a compensation at the EVSE boundary — invisible to the rest of the system.

**Why the zero-guard condition is naturally satisfied by this placement:**

`_update_state()` already contains all the guards that force `current = 0`:
- `prevent_phase_switch` during first charge phase-switch
- `PERFORMING_PHASE_SWITCH` state

These run before the offset is applied. If any guard has set `current = 0`, it means "do not charge" and the offset must not be applied. Since the guards execute first, the `if current != 0:` check is already enforced by code flow.

**Apply in `Process._update_state()`** (`packages/control/process.py`):

```python
        # Wenn ein EV zugeordnet ist und die Phasenumschaltung aktiv ist, darf kein Strom gesetzt werden.
        if chargepoint.data.control_parameter.state == ChargepointState.PERFORMING_PHASE_SWITCH:
            current = 0

        # Apply vehicle current offset (compensation for vehicles that draw less/more than commanded).
        # Only when charging is actually intended (current != 0). The offset is applied after all
        # zero-forcing guards so those semantics are preserved. Re-clamp to CP hardware ceiling only
        # (EV min/max clamping has already run; a negative offset should not suppress charging entirely).
        if current != 0:
            offset = charging_ev.ev_template.data.current_offset
            if offset != 0:
                current = current + offset
                current = max(current, chargepoint.data.control_parameter.min_current)
                current = chargepoint.check_cp_max_current(current, chargepoint.data.control_parameter.phases)

        chargepoint.data.set.current = current
```

The `check_cp_max_current()` re-clamp ensures a large positive offset cannot exceed the chargepoint hardware ceiling. The `max(current, min_current)` guard ensures a negative offset cannot accidentally produce a value below the EV's minimum (which would be semantically "stop charging" when charging was intended).

---

## 6. Design Considerations for Safe Implementation

### 6.1 Zero-current guard — naturally satisfied by placement
`current == 0` means "do not charge" (stop mode, phase switch, etc.). By inserting the offset after all zero-forcing guards in `_update_state()`, these semantics are preserved without an explicit `if current != 0` check needing to guard against guard overrides — the guards simply run first.

### 6.2 Load management is undistorted — key advantage of late application
The LM, algorithm, and logging all operate on the intended current. The offset is invisible to everything upstream. See the comparison table in section 5.

### 6.3 Negative offset and the `min_current` floor
A negative offset (vehicle draws more than commanded) reduces the EVSE command. If the offset brings `current` below `ev_template.min_current`, the `max(current, min_current)` guard raises it back rather than silently dropping to 0, preserving charging intent.

### 6.4 CP hardware ceiling re-clamp
After applying the offset, `check_cp_max_current()` re-clamps to the chargepoint template's hardware ceiling. The full `check_min_max_current()` is intentionally not re-run: the EV min/max checks have already passed; only the hardware ceiling can now be violated by the offset.

### 6.5 `chargepoint.data.set.current` holds the offset value
The log line `log.info(f"LP{num}: set current {current} A ...")` and the MQTT publish `openWB/set/chargepoint/{id}/set/current` will reflect the offset value. This is correct and desirable: it shows what was actually sent to the EVSE.

### 6.6 `nominal_difference` interaction
The phase-switch logic compares actual measured current against `required_current ± nominal_difference`. `required_current` is set in the prepare phase and does not include the offset. If the offset closes the actual/commanded gap, the tolerance window may become less necessary. These are independent tuning parameters.

### 6.7 `consider_less_charging_chargepoint_in_loadmanagement()` interaction
This function in `common.py` detects when actual current is more than `nominal_difference` below **`cp.data.set.current`** (the offset value) for >60 s and substitutes actual current in LM calculations. If the offset is well-tuned, the vehicle draws near `set.current` and this correction is rarely triggered — which is the desired outcome.

### 6.8 EVSE-based dynamic offset in `surplus_controlled.py` stacking
The existing PV-mode correction (`surplus_controlled.py` ~line 190) adjusts `cp.data.set.current` upward when `evse_current > actual_current`. This happens earlier in the cycle (before `_update_state()`), so the static offset is applied on top of it. In practice: the static offset handles the systematic vehicle bias; the dynamic EVSE correction handles transient surplus tracking. They can coexist.

### 6.9 DC charging
For DC chargepoints, `add_conversion_loss_to_current()` (efficiency compensation) is applied during prepare. The offset is applied in AC-equivalent amps and sits downstream of that conversion. This is correct — the offset compensates for EVSE-to-vehicle behaviour, not the AC/DC conversion efficiency.

### 6.10 Bidi (discharge) mode
For negative currents (discharge), `required_current < 0`. The `current != 0` guard is satisfied. A positive offset reduces discharge magnitude; a negative offset increases it. If the vehicle discharges less than commanded, a negative offset commands more. The `check_cp_max_current()` call uses `sign = 1 if required_current >= 0 else -1` and handles the sign correctly. Consider whether a separate `discharge_current_offset` makes the UI less confusing.

### 6.11 UI / MQTT persistence
`EvTemplateData` fields are serialised to MQTT topic `openWB/vehicle/{id}/ev_template/config` and to the configuration JSON. Adding the field to the dataclass will automatically handle persistence. The web UI settings page for the EV template needs a corresponding input field.

---

## 7. Summary of All Files That Need Changes

| File | Change |
|---|---|
| `packages/control/ev/ev_template.py` | Add `current_offset: float = 0` to `EvTemplateData` |
| `packages/control/process.py` | Apply offset in `_update_state()` after all zero-forcing guards, before `chargepoint.data.set.current = current`; re-clamp to CP hardware ceiling only |
| `web/settings/` (EV template UI) | Add input for `current_offset` in the vehicle profile settings page |
| *(optional)* `packages/helpermodules/update_config.py` | Migration entry if needed for existing configs (default 0 means no migration required) |

No changes are needed in the algorithm modules, load management, or chargepoint hardware modules — the offset is applied at the very last step before the hardware call, completely transparent to the rest of the system.
