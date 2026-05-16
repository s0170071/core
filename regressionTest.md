# PV Charging Regression Test Plan

Goal: replay realistic PV-surplus and house-battery scenarios against the openWB
control loop at faster-than-real-time, capture every EVSE current write and every
GPIO relay toggle, and assert on aggregate behaviour (no rapid on/off cycles, no
oscillating phase switches, debounce of the 60 s zero-write filter, etc.).

## 0. Defects this plan must catch

The 17:47 incident shows that, with `BatConsiderationMode.MIN_SOC_BAT` and
SoC > `min_soc`, the car was resumed on 1 phase after the switch and then
stopped ~6 min later. The MIN_SOC_BAT design says the car must keep charging
while SoC > min_soc — so the stop is a real bug, not just a PV transient.
Identified failure chain (all in live code, not the new EVSE filter):

1. `BatAll._ev_at_min_current()`
   ([packages/control/bat_all.py](packages/control/bat_all.py#L304))
   only latches ASSIST when **every** charging CP is already at
   `ev_template.min_current`. Right after a phase switch the EV is at the
   pre‑switch current (here 6.7 A, not 6.0 A min), so ASSIST never latches.
2. In **PRIORITY** state
   ([bat_all.py](packages/control/bat_all.py#L275)),
   `charging_power_left = 0` whenever `power >= 0` and SoC < max. So as soon
   as the EV draws current, the meter goes import‑positive, battery throttles,
   and surplus visible to the algorithm collapses to ≤ 0.
3. `switch_off_check_threshold`
   ([counter.py](packages/control/counter.py#L505))
   arms the 60 s `SWITCH_OFF_DELAY` timer the moment
   `power_in_use > threshold AND actual_current ≤ min_current + nominal_diff`.
   The algorithm needs several ticks to ramp 6.7 A → 6.0 A; before ASSIST can
   finally latch, the timer expires and `NO_CHARGING_ALLOWED` fires.
4. Even when ASSIST *does* latch, `cpl = discharge_rate + min(0, power)`
   ([bat_all.py L261](packages/control/bat_all.py#L261))
   can be negative when `bat_power_discharge_active=False`, so the invariant
   "SoC > min_soc ⇒ EV keeps charging" is not enforced.

The new 60 s zero-write debounce in `Evse.set_current` is a hardware-level
safety net only; it does **not** repair this. The regression suite below
covers both the EVSE-level filter (cases 2, 4, 7, 8 in §4) and the four state-
machine defects above (cases 4–7 in §4 — see `test_no_post_switch_immediate_stop`,
`test_assist_latches_above_min_soc`,
`test_assist_with_zero_discharge_rate`,
`test_switch_off_timer_blocked_during_ramp`).

## 1. Scope

Subjects under test (live, unmodified):

- `packages/control/algorithm/*` (surplus, additional_current, min_current)
- `packages/control/counter.py`, `counter_all.py`
- `packages/control/bat_all.py` (state machine: BAT_MODE / EV_MODE / MIN_SOC_BAT)
- `packages/control/chargepoint/chargepoint.py`
  (`get_phases_by_selected_chargemode`, `set_phases`)
- `packages/control/ev/ev.py` (`_check_phase_switch_conditions`,
  `auto_phase_switch`)
- `packages/control/process.py` (`_update_state`, `_start_charging`,
  including `current_offset` logic)
- `packages/modules/common/evse.py` (the new 60 s zero-write debounce)

Faked at the I/O boundary:

- The Modbus client behind `Evse` (records every `write_registers` call).
- `RPi.GPIO` / `safe_relay_output` (records every relay edge).
- Inverter, EVU counter, house battery (`bat_all`) value sources — fed from a
  scenario table.
- `time.time()` and `time.sleep()` — replaced by a virtual clock so a 4 h
  scenario runs in well under a second.

## 2. Architecture

```
   scenario.csv  ─┐
                  │
   ┌──────────────▼──────────────┐
   │     FakeClock (virtual t)   │  patches time.time / time.sleep
   └──────────────┬──────────────┘
                  │
   ┌──────────────▼──────────────┐    ┌────────────────────────────┐
   │    ScenarioDriver (tick)    │───▶│  House-battery simulator   │
   │  - sets PV power            │    │  7.5 kWh, ±5 kW, η=0.95    │
   │  - sets house load          │    │  SoC integrator            │
   │  - computes meter reading   │◀───│                            │
   └──────────────┬──────────────┘    └────────────────────────────┘
                  │ publishes counter/inverter/bat values
                  ▼
   ┌─────────────────────────────┐
   │   openWB control loop       │  real code: algorithm, bat_all,
   │   (one tick per 10 virt-s)  │  chargepoint, process, ev, evse
   └──────────────┬──────────────┘
                  │ evse.set_current / GPIO writes
                  ▼
   ┌─────────────────────────────┐
   │   Recorders                 │  → events.jsonl (t, kind, value)
   └─────────────────────────────┘
```

### 2.1 Virtual clock

`packages/test_utils/fake_clock.py` (new):

```python
class FakeClock:
    def __init__(self, start=0.0): self.t = start
    def time(self): return self.t
    def sleep(self, dt): self.t += dt        # never actually sleeps
    def advance(self, dt): self.t += dt
```

Patched via `pytest` `monkeypatch` against:

- `time.time`, `time.sleep` (module-level in `evse.py`, `process.py`,
  `chargepoint_module.py`, `bat_all.py`, anywhere else that imports
  `time.time` directly).
- Any `datetime.now()` usage that feeds into control decisions.

A helper `patch_time(monkeypatch, clock)` does the wiring once.

### 2.2 Hardware fakes

`packages/test_utils/fake_evse.py`:

- `FakeModbusClient`: in-memory register dict; `write_registers` appends
  to `evse_writes` list with `(clock.t, register, value)`.
- Built so the real `Evse` class is instantiated unchanged — only the
  client below it is swapped. This keeps the new `force=` /
  `ZERO_WRITE_DEBOUNCE_S` logic on the live code path.

`packages/test_utils/fake_gpio.py`:

- Replaces `safe_relay_output` (or patches `RPi.GPIO.output`) to append
  `(clock.t, pin, level)` to `gpio_events`.

`packages/test_utils/fake_counter.py`:

- Implements the minimal `Counter`/`Inverter`/`Bat` getter surface used
  by `counter_all` and `bat_all`. Values are driven each tick from the
  battery simulator + scenario driver.

### 2.3 House-battery simulator

```python
class BatterySim:
    capacity_wh = 7500
    p_max_charge_w = 5000
    p_max_discharge_w = 5000
    eta = 0.95
    soc = 0.5           # 0..1

    def step(self, surplus_w, dt_s):
        """surplus_w > 0  → charge, < 0 → discharge.
        Returns (battery_power_w, grid_power_w)."""
        p = clamp(surplus_w, -self.p_max_discharge_w, self.p_max_charge_w)
        dE = p * dt_s / 3600 * (self.eta if p > 0 else 1/self.eta)
        new_soc = clamp(self.soc + dE / self.capacity_wh, 0.0, 1.0)
        actual_dE = (new_soc - self.soc) * self.capacity_wh
        actual_p = actual_dE * 3600 / dt_s
        self.soc = new_soc
        grid = surplus_w - actual_p   # what flows to/from the grid
        return actual_p, grid
```

Per tick the driver computes:

```
ev_power     = evse_current * phases_in_use * 230   # from current EVSE state
surplus      = pv_power - house_load - ev_power
bat_p, grid  = battery.step(surplus, dt)
counter.set(power = -grid, ...)                     # negative = export
inverter.set(power = -pv_power)
bat.set(power = bat_p, soc = battery.soc*100)
```

### 2.4 Scenario format

CSV, one row per scenario segment:

```
t_start_s, duration_s, pv_w, house_load_w, comment
0,        600,  9000, 800, "strong sun, light load"
600,      300,  4500, 800, "cloud transit, surplus drops"
900,      300, 11000, 800, "sun back"
1200,     180,  1500, 800, "deep cloud — should trigger 3→1 then stop"
1380,     300,  9000, 800, "sun back again"
```

Driver linearly interpolates between segments (configurable: step / ramp).

### 2.5 Tick loop

```python
TICK_S = 10
for _ in range(scenario_duration_s // TICK_S):
    driver.step(TICK_S)        # update PV/load/bat/counter values
    publish_to_data_layer()    # mimic helpermodules.subdata
    algorithm.run()            # one full control tick
    clock.advance(TICK_S)
```

Because `time.sleep` is a no-op and there is no real Modbus I/O, a 4 h
scenario (= 1440 ticks) finishes in roughly the time it takes Python to
execute the algorithm 1440 times — typically < 2 s on a dev box.

## 3. Recorded artefacts

Per test, written to `tmp_path/events.jsonl`:

```json
{"t": 612.0, "kind": "evse_write", "id": 1, "current_a": 6.7, "force": false}
{"t": 614.0, "kind": "evse_write_suppressed", "id": 1, "age_s": 1.0}
{"t": 1230.0, "kind": "gpio", "pin": "GPIO29", "level": "HIGH", "label": "1p_relay"}
{"t": 1234.5, "kind": "phase_switch", "from": 3, "to": 1}
```

Helpers:

- `events.evse_writes()` → list of accepted writes
- `events.relay_edges(label=...)`
- `events.phase_switches()`

## 4. Test cases

All under `packages/control/regression/`:

| Test                                  | Scenario                                       | Assertion |
|---------------------------------------|------------------------------------------------|-----------|
| `test_steady_sun_no_oscillation`      | 9 kW PV, 800 W load, 1 h                       | exactly 1 start, 0 stops, 0 phase switches |
| `test_cloud_dip_does_not_stop_quickly`| 9 kW → 1.5 kW for 3 min → 9 kW                 | no EVSE 0-write within 60 s of the start → 60 s debounce active; ≤ 1 phase switch round-trip |
| `test_3_to_1_phase_switch_clean`      | Slowly ramp 9 kW → 2.5 kW over 10 min          | exactly one 3→1 switch, EVSE goes to 0 (forced), then non-zero on 1 phase, no immediate stop |
| `test_no_post_switch_immediate_stop`  | Reproduces the 17:47 log: 3‑phase ▶ stop ▶ switch ▶ resume on 1‑phase, PV held just below 1‑phase × 6 A demand, battery SoC > min_soc, `bat_power_discharge_active=True` | car must stay charging for the full scenario; **0 EVSE zero‑writes** after the resume; `bat_all` reaches ASSIST within ≤ 3 ticks of resume; battery SoC decreases (proving discharge actually happened); no `SWITCH_OFF_DELAY` ever entered |
| `test_assist_latches_above_min_soc`   | Steady PV slightly below EV demand, SoC starts 60 %, `min_soc=20`, `discharge_rate=4000` | ASSIST latches even though EV is **above** `min_current` (regression guard for the `_ev_at_min_current` fix); car charges continuously; SoC drifts down toward 20 % but never stops while > min_soc |
| `test_assist_with_zero_discharge_rate`| Same as above but `bat_power_discharge_active=False` | documents intended behaviour: car *may* stop. Asserts that if it stops, the stop happens **only after** the algorithm has reached `min_current` AND `switch_off_delay` has elapsed — not on a transient |
| `test_switch_off_timer_blocked_during_ramp` | Car at 6.7 A on 1 phase, PV drops so target current ramps from 6.7 → 6.0 A over several ticks | `SWITCH_OFF_DELAY` must not arm while `actual_current > min_current`; timer may only start once min is reached |
| `test_full_battery_uses_all_surplus`  | PV 11 kW, load 0.8 kW, batt starts at 100 %    | EV gets the surplus, no battery charge attempt, no oscillation |
| `test_empty_battery_priority`         | PV 4 kW, load 0.8 kW, batt at 5 %, MIN_SOC_BAT | EV stays off until batt > min SoC, then starts |
| `test_phase_switch_bypasses_debounce` | Force a switch < 60 s after a non-zero write   | the forced 0-write goes through (events show `force=true`) while a non-forced 0-write at the same instant would be suppressed |
| `test_relay_loop_alarm`               | Synthetic: force ≥ 3 toggles in 120 s          | `evse_relay_log` warning emitted, but no hardware writes within debounce window |

Each test:

1. Loads scenario CSV.
2. Builds `FakeClock`, `BatterySim`, `FakeModbusClient`, `FakeGPIO`,
   real `Evse`, real `Chargepoint`, real algorithm.
3. Runs the tick loop.
4. Reads `events.jsonl` via helpers and asserts.

## 5. Wall-clock budget

A full pass of all eight scenarios (~16 h of simulated time) must finish
in < 10 s on the dev machine and < 30 s on the Pi. CI runs the suite on
every PR via the existing `pytest` invocation:

```
ssh openwb@192.168.1.20 \
  "cd /var/www/html/openWB && python3 -m pytest packages/control/regression -v"
```

## 6. Deliverables / file layout

```
packages/test_utils/
  fake_clock.py
  fake_evse.py        # FakeModbusClient + helpers
  fake_gpio.py
  fake_counter.py
  battery_sim.py
  scenario.py         # CSV loader + ScenarioDriver
  events.py           # JSONL recorder + query helpers

packages/control/regression/
  conftest.py         # builds the full simulated stack as a fixture
  scenarios/
    steady_sun.csv
    cloud_dip.csv
    ramp_down.csv
    post_switch.csv
    full_battery.csv
    empty_battery.csv
  test_steady_sun_no_oscillation.py
  test_cloud_dip_does_not_stop_quickly.py
  test_3_to_1_phase_switch_clean.py
  test_no_post_switch_immediate_stop.py
  test_full_battery_uses_all_surplus.py
  test_empty_battery_priority.py
  test_phase_switch_bypasses_debounce.py
  test_relay_loop_alarm.py
```
## 7. Fail/pass criteria
 the test has failed if there are consecutive evse writes that alternate between 0 and > 0 ampere where a fail pattern is 0 ... >0 ... 0
 The result will also provide a charged amount of energy into the car. A good / fail number depends on the input (PV data)

## 7. Open questions / risks

- `bat_all` reads several MQTT-backed retained values; the fake counter
  layer must cover those (`openWB/bat/get/power`, `.../soc`,
  `openWB/counter/0/get/power`, `openWB/pv/get/power`). Use the existing
  `dataclass_utils` to construct `Data` in-process rather than going
  through MQTT.
- The algorithm relies on `data.data.counter_all_data.get_evu_counter()`
  being singletons; the fixture must reset the global `data` module
  between tests (a `data._reset()` helper).
- Some modules import `time.time` as `from time import time` at module
  scope, capturing the unpatched reference. Audit and convert offenders
  to `import time` + `time.time()` so `monkeypatch` works uniformly.
- Threading in `process._start_charging`: tests run the produced
  `Thread.run()` synchronously (don't `start()`) to keep ordering
  deterministic.
