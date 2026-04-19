# openWB Bug Research Report: EVSE Current Output During Relay Phase Switch

**Codebase version:** 2.1.9-Patch.2 (commit `f34d6752c`, 2026-03-18)  
**Report date:** 2026-04-19  
**Scope:** Internal chargepoint (openWB Series 2 / Duo / SE), PV surplus charging mode  
**Symptom:** The EVSE register still holds a non-zero ampere value at the moment the phase-switch relays transition, potentially allowing the vehicle to draw current while live contactors are switching.

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Key Code Paths](#2-key-code-paths)
   - 2.1 Main Control Loop
   - 2.2 Internal Chargepoint Handler Loop
   - 2.3 Phase-Switch Execution Path
3. [EVSE Register Protocol](#3-evse-register-protocol)
4. [Identified Bugs and Structural Issues](#4-identified-bugs-and-structural-issues)
   - 4.1 Race Condition: `set_current` Written Before Phase Switch Fires (Same Cycle)
   - 4.2 `set_current(0)` Is Not Verified Before GPIO Relay Toggle
   - 4.3 `old_phases_in_use` Initialization Race at Startup
   - 4.4 `evse_current` Cache Out-of-Sync During Phase Switch
   - 4.5 Concurrent Modbus Bus Access During Phase Switch
   - 4.6 No Modbus Write Confirmation / Retry Logic
   - 4.7 Composite Risk: No End-to-End Safety Verification
   - 4.8 Two Independent Loops Share EVSE State Without a Mutex
5. [Chain of Events Leading to the Bug (PV Mode)](#5-chain-of-events-leading-to-the-bug-pv-mode)
6. [Reproduction Conditions](#6-reproduction-conditions)
7. [Impact Assessment](#7-impact-assessment)
8. [Recommendations](#8-recommendations)

---

## 1. System Architecture Overview

The openWB system runs two concurrent execution loops that both interact with the EVSE:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  main.py  ─ handler10Sec()  (every ~10s per control interval, main thread)  │
│                                                                             │
│  loadvars_.get_values()          ← reads hardware sensors                   │
│  prep.setup_algorithm()          ← determines chargemode, PV surplus        │
│  control.calc_current()          ← algorithm assigns set.current            │
│  proc.process_algorithm_results()                                            │
│      ├─ _update_state(cp)        ← zeroes current if PERFORMING_PHASE_SWITCH │
│      ├─ initiate_phase_switch(cp)← starts phase-switch THREAD A             │
│      └─ _start_charging(cp)      ← starts THREAD B → chargepoint_module     │
│                                     .set_current(cp.data.set.current)       │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  internal_chargepoint_handler.py ─ loop()  (every 1.1s, separate thread)   │
│                                                                             │
│  HandlerChargepoint.update()                                                │
│      ├─ cp_module.get_values()   ← reads EVSE + meter over Modbus           │
│      └─ UpdateState.update_state()                                          │
│              ├─ cp_module.set_current(set_current)  ← writes EVSE register  │
│              └─ if trigger_phase_switch:                                     │
│                     __thread_phase_switch() → perform_phase_switch()        │
│                         ├─ evse_client.set_current(0)                       │
│                         ├─ GPIO CP off                                       │
│                         ├─ GPIO relay toggle                                 │
│                         ├─ GPIO relay restore                                │
│                         └─ GPIO CP on                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

These two loops share the EVSE Modbus client and GPIO pins **without any shared mutex**.

---

## 2. Key Code Paths

### 2.1 Main Control Loop

**File:** `packages/main.py` → `HandlerAlgorithm.handler10Sec()`

The main loop runs every 10 seconds (configurable via `control_interval`). Within one cycle:

1. All hardware values are read.
2. The PV surplus algorithm decides whether to charge, stop, or phase-switch.
3. `process.py → process_algorithm_results()` iterates over all chargepoints and:
   - Calls `cp.initiate_phase_switch()` if a phase switch is needed.
   - Calls `_update_state(cp)` which **forces `set.current = 0`** when the state is `PERFORMING_PHASE_SWITCH`.
   - Spawns `Thread B` via `_start_charging(cp)` → `chargepoint_module.set_current(current)`.

For the internal chargepoint (primary openWB): `chargepoint_module` is `modules/chargepoints/internal_openwb/ChargepoitModule` which extends `external_openwb/ChargepointModule`. Its `set_current()` publishes to MQTT:

```python
# packages/modules/chargepoints/external_openwb/chargepoint_module.py
def set_current(self, current: float) -> None:
    pub.pub_single(
        f"openWB/set/internal_chargepoint/{self.config.configuration.duo_num}/data/set_current",
        current,
        hostname=self.config.configuration.ip_address)
```

Similarly, `switch_phases()` publishes two MQTT topics in sequence:

```python
def switch_phases(self, phases_to_use: int) -> None:
    pub.pub_single("...data/phases_to_use", phases_to_use, ...)
    pub.pub_single("...data/trigger_phase_switch", True, ...)
```

These arrive at the satellite/internal handler as **separate, independent MQTT messages** processed on different broker cycles.

### 2.2 Internal Chargepoint Handler Loop

**File:** `packages/modules/internal_chargepoint_handler/internal_chargepoint_handler.py`

The handler loop runs every 1.1 seconds. Each cycle:

```python
def update(self, global_data, data, rfid_data):
    phase_switch_cp_active = (thread_active(cp_interruption_thread) or
                               thread_active(phase_switch_thread))
    state = self.module.get_values(phase_switch_cp_active, ...)  # reads EVSE
    self.update_state.update_state(data, heartbeat_expired)
```

`update_state.update_state()`:

```python
def update_state(self, data, heartbeat_expired):
    set_current = 0 if heartbeat_expired else data.set_current

    if self.phase_switch_thread and self.phase_switch_thread.is_alive():
        return  # guard: skip if phase switch in progress

    if self.cp_interruption_thread and self.cp_interruption_thread.is_alive():
        return  # guard: skip if CP interruption in progress

    self.cp_module.set_current(set_current)          # ← EVSE write
    Pub().pub(...)

    if data.trigger_phase_switch:
        self.__thread_phase_switch(data.phases_to_use)  # ← starts relay thread
        Pub().pub("...trigger_phase_switch", False)
```

### 2.3 Phase-Switch Execution Path

**File:** `packages/modules/internal_chargepoint_handler/chargepoint_module.py`

```python
def perform_phase_switch(self, phases_to_use: int) -> None:
    gpio_cp, gpio_relay = self._client.get_pins_phase_switch(phases_to_use)
    with SingleComponentUpdateContext(self.fault_state, update_always=False):
        self._client.evse_client.set_current(0)   # Step 1: write 0 to EVSE
    time.sleep(5)                                  # Step 2: wait 5s
    GPIO.output(gpio_cp, GPIO.HIGH)  # CP off      # Step 3: disconnect CP signal
    GPIO.output(gpio_relay, GPIO.HIGH)  # relay     # Step 4: switch relay
    time.sleep(5)                                  # Step 5: wait 5s
    GPIO.output(gpio_relay, GPIO.LOW)              # Step 6: relay settled
    time.sleep(5)                                  # Step 7: wait 5s
    GPIO.output(gpio_cp, GPIO.LOW)   # CP on       # Step 8: restore CP signal
    time.sleep(1)
```

**GPIO pin mapping** (`clients.py → get_pins_phase_switch()`):

| CP num | gpio_cp | gpio_relay (1-phase) | gpio_relay (3-phase) |
|--------|---------|----------------------|----------------------|
| 0      | 22      | 29                   | 37                   |
| 1      | 15      | 11                   | 13                   |

---

## 3. EVSE Register Protocol

**File:** `packages/modules/common/evse.py`

> **Note:** The register mappings below are inferred from the codebase (Modbus read/write calls in `evse.py`). They may vary across EVSE hardware revisions or firmware versions. The values should be verified against the official EVSE firmware documentation for the specific hardware in use.

| Register | Purpose |
|----------|---------|
| 1000     | Set current (integer Ampere, or ×100 in precise mode) |
| 1001     | (padding) |
| 1002     | State (1=Ready, 2=EV present, 3=Charging, 4=Charging+vent, 5=Failure) |
| 1005     | Firmware version |
| 2005     | Feature flags (bit 7 = precise current mode) |
| 2007     | Max current |

The EVSE's `get_plug_charge_state()` reads register 1000 and caches the result in `self.evse_current`:

```python
raw_set_current, _, state_number = self.client.read_holding_registers(
    1000, [ModbusDataType.UINT_16]*3, unit=self.id)
self.evse_current = int(raw_set_current)
```

`set_current()` uses this cache to avoid redundant writes:

```python
def set_current(self, current: int, phases_in_use=None) -> None:
    formatted_current = round(current * 100) if self._precise_current else round(current)
    if self.evse_current != formatted_current:
        self.client.write_register(1000, formatted_current, unit=self.id)
```

**Critical:** The cache `self.evse_current` is only updated by `get_plug_charge_state()` (called by `get_values()`), not by `set_current()`. If the hardware is slow to respond, the cache will diverge.

---

## 4. Identified Bugs and Structural Issues

### 4.1 Race Condition: `set_current` Written Before Phase Switch Fires (Same Cycle)

**Severity: HIGH**

**Location:** `internal_chargepoint_handler.py → UpdateState.update_state()`

```python
self.cp_module.set_current(set_current)   # ← EVSE write happens FIRST
Pub().pub(...)

if data.trigger_phase_switch:
    self.__thread_phase_switch(data.phases_to_use)  # ← phase switch starts AFTER
```

**The problem:** In the first cycle where `trigger_phase_switch` becomes `True`, the EVSE current is written **before** the phase switch is initiated. If the parent WB sends `set_current > 0` in the same MQTT batch as `trigger_phase_switch = True` (because the MQTT messages for those two fields arrive in the same 1.1-second window), the sequence is:

1. EVSE register 1000 is written with the **non-zero** current value.
2. Phase switch thread starts.
3. Phase switch thread calls `set_current(0)` — but only **after thread startup overhead and its own 0.1s sleep + Modbus transaction time**.
4. During this gap (on the order of milliseconds to ~200ms), the EVSE holds the non-zero setpoint.

The phase switch thread then sleeps for 5 seconds before touching any GPIO, so the relay does not toggle during this initial gap. The 5-second delay (which aligns with IEC 61851's requirement that vehicles stop drawing current within 3 seconds of a pilot signal change) provides a safety margin. However, this margin depends entirely on the `set_current(0)` write succeeding — which is not verified (see Bug 4.2).

The guard `if self.phase_switch_thread.is_alive(): return` only prevents **subsequent** handler cycles from writing to EVSE while the relay is switching. It does not protect the **first** cycle where both `set_current` and `trigger_phase_switch` arrive together.

**Correct ordering should be:**

```python
if data.trigger_phase_switch:
    self.cp_module.set_current(0)   # force zero before switch
    self.__thread_phase_switch(data.phases_to_use)
    Pub().pub("...trigger_phase_switch", False)
    return  # do not write set_current in this cycle
self.cp_module.set_current(set_current)
```

This ensures the zero-current command is written atomically with the phase switch initiation, eliminating the ordering race.

---

### 4.2 `set_current(0)` Is Not Verified Before GPIO Relay Toggle

**Severity: HIGH**

**Location:** `chargepoint_module.py → perform_phase_switch()`

```python
def perform_phase_switch(self, phases_to_use: int) -> None:
    gpio_cp, gpio_relay = self._client.get_pins_phase_switch(phases_to_use)
    with SingleComponentUpdateContext(self.fault_state, update_always=False):
        self._client.evse_client.set_current(0)   # write attempted...
    time.sleep(5)                                  # ...wait 5 seconds...
    GPIO.output(gpio_cp, GPIO.HIGH)                # CP off
    GPIO.output(gpio_relay, GPIO.HIGH)             # relay toggles here
```

The `SingleComponentUpdateContext` context manager **silently swallows exceptions** (`update_always=False`). If the Modbus write of `set_current(0)` fails (bus timeout, collision), the code **continues to sleep and then toggles the relay** without any verification that the EVSE received the zero-current command.

There is no read-back check of register 1000 before the relay is toggled. The EVSE register could still hold the previous non-zero value when the relay switches.

Additionally, the `evse_current` cache in `Evse` is **not updated** by `set_current()` — only by `get_plug_charge_state()`. So if the write silently fails, the cache still shows the old value, and the next `get_values()` call will not trigger a retry because it reads from hardware (but if the hardware retained the old value, the cache is now consistent with a wrong state).

---

### 4.3 `old_phases_in_use` Initialization Race at Startup

**Severity: MEDIUM**

**Location:** `chargepoint_module.py → ChargepointModule.__init__()`

```python
if float(run_command.run_command(["cat", "/proc/uptime"]).split(" ")[0]) < 180:
    self.perform_phase_switch(1)
    self.old_phases_in_use = 1
else:
    def on_connect(client, userdata, flags, rc):
        client.subscribe(f"openWB/internal_chargepoint/{self.local_charge_point_num}/get/phases_in_use")

    def on_message(client, userdata, message):
        self.old_phases_in_use = decode_payload(message.payload)

    self.old_phases_in_use = 1
    BrokerClient(...).start_finite_loop()
```

The `start_finite_loop()` is called synchronously, but the MQTT subscription may not deliver a message before the finite loop times out (e.g., if the broker has not yet received the retained message, or the message is in flight). In that case, `old_phases_in_use` defaults to **1** even if the hardware was left in a 3-phase state from the previous session.

This means `set_current()` may call `evse_client.set_current(current, phases_in_use=1)` when the relay is actually in the 3-phase position, causing the 16A cap for multi-phase operation to be skipped:

```python
def set_current(self, current: int, phases_in_use=None) -> None:
    if self.max_current == 20 and phases_in_use is not None and phases_in_use != 0:
        if current > 16 and phases_in_use > 1:
            current = 16  # ← only applied when phases_in_use > 1
```

If `phases_in_use=1` but relay is at 3-phase, the 16A cap is not applied. In practice, the EVSE hardware has a `max_current` register (register 2007, read at initialization) that imposes an absolute ceiling—a 20A EVSE will not accept values above 20A regardless of software state. The realistic impact is therefore a ~25% overcurrent (20A instead of 16A on each of 3 phases), not an unlimited bypass. This is still a concern for relay and wiring thermal limits, but the risk is bounded by hardware.

---

### 4.4 `evse_current` Cache Out-of-Sync During Phase Switch

**Severity: LOW–MEDIUM**

**Location:** `evse.py → Evse.set_current()` and `get_plug_charge_state()`

During a phase switch, `get_values()` is still called by the handler loop (because `phase_switch_cp_active=True` only freezes `plug_state`; the EVSE is still read):

```python
# chargepoint_module.py → get_values()
evse_state, counter_state = self._client.request_and_check_hardware(self.fault_state)
# ← this reads the EVSE including register 1000, updating evse_current cache
```

However, `request_and_check_hardware` is wrapped in `self.client_error_context`, and if the EVSE is mid-transaction (busy processing the `set_current(0)` write from `perform_phase_switch`), a Modbus collision can occur. In the RS-485 serial environment shared by the EVSE and the energy meter, a collision causes the EVSE read to fail, leaving `evse_current` at its previous cached value.

When the phase switch completes and the handler next calls `set_current(new_value)`, the comparison `self.evse_current != formatted_current` may be based on stale data.

**Mitigating factors:** The handler loop calls `get_plug_charge_state()` every 1.1 seconds via `get_values()`, which reads the hardware register and refreshes the cache. Additionally, the `is_alive()` guard in `update_state()` prevents `set_current()` from being called during the phase switch, so the stale cache is only a concern in the first handler cycle after the phase switch thread completes. For the cache to cause harm, a Modbus write must fail (leaving the EVSE at a non-zero value) while a subsequent Modbus read succeeds but returns the wrong value—an unlikely combination. The scenario is theoretically possible but low-probability in practice.

---

### 4.5 Concurrent Modbus Bus Access During Phase Switch

**Severity: MEDIUM**

**Location:** `internal_chargepoint_handler.py → HandlerChargepoint.update()`

> **Note:** Despite prior labeling, this issue is **not specific to satellite mode**. The satellite uses MQTT to communicate with the primary; the RS-485 bus contention described here occurs locally on a single openWB unit between the handler loop thread and the phase switch thread.

```python
phase_switch_cp_active = (
    __thread_active(self.update_state.cp_interruption_thread) or
    __thread_active(self.update_state.phase_switch_thread)
)
state = self.module.get_values(phase_switch_cp_active, rfid_data.last_tag)
heartbeat_expired = self._check_heartbeat_expired(global_data.heartbeat)
self.update_state.update_state(data, heartbeat_expired)
```

`get_values()` is called **regardless** of `phase_switch_cp_active`. Only `plug_state` is frozen inside `get_values()` when `phase_switch_cp_active` is True. The EVSE is still queried via Modbus during the relay switch, meaning the RS-485 bus is being used concurrently from two paths:

- `perform_phase_switch` → `set_current(0)` (from phase switch thread)
- `get_values` → `request_and_check_hardware` (from handler loop thread)

These are **not serialized** by any lock. The `ModbusSerialClient_` context manager (the `with client:` pattern) provides mutual exclusion for the duration of a single Modbus transaction, but it does not prevent interleaving of **separate** transactions from different threads. A read from the handler loop could occur between the phase switch thread's write and a hypothetical readback verification, for example.

> **Note on pymodbus:** Depending on the pymodbus version and the `ModbusSerialClient` configuration, the underlying socket may or may not serialize concurrent calls. The codebase does not appear to rely on pymodbus-level thread safety, so this concern is valid regardless of the library version.

---

### 4.6 No Modbus Write Confirmation / Retry Logic

**Severity: MEDIUM**

**Location:** `evse.py → Evse.set_current()`

```python
def set_current(self, current: int, phases_in_use=None) -> None:
    time.sleep(0.1)
    ...
    if self.evse_current != formatted_current:
        self.client.write_register(1000, formatted_current, unit=self.id)
        # ← no return value checked, no readback, no retry
```

The `write_register` call returns no confirmation of success. pymodbus raises an exception on failure, but only transport-level failures (timeout, framing error). Application-level NACK from the EVSE (e.g., value out of range, EVSE in error state) may not raise an exception depending on the pymodbus version and exception mode configuration.

This means the function returns normally even if the EVSE did not act on the write.

---

### 4.7 Composite Risk: `perform_phase_switch` Has No End-to-End Safety Verification

**Severity: HIGH** (composite of 4.1, 4.2, 4.6)

**Location:** `chargepoint_module.py → perform_phase_switch()`

> **Note:** This is not a separate bug but rather describes the **combined effect** of bugs 4.1, 4.2, and 4.6. It is listed here to make the end-to-end risk explicit.

The full phase switch sequence has no verified handshake between:

1. EVSE Modbus write (`set_current(0)`)
2. CP GPIO toggle (physically disconnects pilot signal from vehicle)
3. Relay GPIO toggle (live switching of AC phases)

If Step 1 fails silently (Modbus write exception caught and swallowed by `SingleComponentUpdateContext`, per Bug 4.2), Steps 2 and 3 still execute.

The **5-second sleep** between `set_current(0)` and the CP GPIO toggle is a reasonable engineering margin—IEC 61851-1 requires vehicles to stop drawing current within 3 seconds of pilot signal cessation, so 5 seconds provides ~2 seconds of headroom. However, this margin assumes the EVSE actually received and obeyed the zero-current command, which is not verified (Bug 4.6). A readback of register 1000 or a check of actual AC current via the energy meter would provide the missing verification.

---

### 4.8 Two Independent Loops Share EVSE State Without a Mutex

**Severity: HIGH**

**Location:** Architectural — `internal_chargepoint_handler.py` loop and `perform_phase_switch` thread

The `evse.Evse` object is accessed from:

1. **Handler loop thread**: `get_values()` → `request_and_check_hardware()` → `evse_client.get_evse_state()` → `get_plug_charge_state()` (reads register 1000, updates `self.evse_current`)
2. **Phase switch thread**: `perform_phase_switch()` → `evse_client.set_current(0)` (reads `self.evse_current`, writes register 1000)
3. **`update_state()` in handler loop**: `cp_module.set_current(current)` → `evse_client.set_current(current)`

The `Evse` object is a shared mutable object. `self.evse_current` is read and written from multiple threads. There is no `threading.Lock` on this object.

While the check in `update_state()` (`if self.phase_switch_thread.is_alive(): return`) attempts to prevent concurrent access, it is a **TOCTOU (time-of-check/time-of-use)** pattern. Between the check and the `set_current()` call, the phase switch thread could transition from not-alive to started, or vice versa.

---

## 5. Chain of Events Leading to the Bug (PV Mode)

The following is a concrete timeline of how the bug manifests in PV surplus charging mode with automatic phase switching.

### Prerequisites

- Chargepoint configured with `auto_phase_switch_hw = True`
- Vehicle is charging on 1 phase (PV surplus barely sufficient)
- PV surplus increases, triggering a 1→3 phase switch after the switch-on delay expires

### Timeline

```
Time    Event
─────────────────────────────────────────────────────────────────────────────
T=0     Main loop cycle N:
        - PV surplus algorithm determines 3-phase charging is now appropriate
        - counter.switch_on_timer_expired() sets state = WAIT_FOR_USING_PHASES
        - chargepoint.initiate_phase_switch() calls phase_switch.thread_phase_switch(cp)
          → _perform_phase_switch() is dispatched in THREAD A
          → THREAD A calls cp.chargepoint_module.switch_phases(3)
          → external_openwb.switch_phases():
              pub_single("...data/phases_to_use", 3)
              pub_single("...data/trigger_phase_switch", True)
          [Two MQTT messages published to the satellite/local broker]
        - process._update_state(cp): state=PERFORMING_PHASE_SWITCH → current=0
        - THREAD B started: chargepoint_module.set_current(0) → publishes
          "openWB/set/internal_chargepoint/0/data/set_current" = 0

─────────────────────────────────────────────────────────────────────────────
T=0 to T~1.1s: Between main loop cycles, handler loop runs at T≈1.1s

        Satellite handler receives MQTT topics. Due to broker ordering and
        QoS, the messages may arrive in this order:
          1. set_current = 0  (correct)
          2. phases_to_use = 3
          3. trigger_phase_switch = True

        OR, if a previous cycle's set_current was not processed yet, the
        InternalChargepointData snapshot may still show set_current > 0
        while trigger_phase_switch = True.

─────────────────────────────────────────────────────────────────────────────
T~1.1s  Handler loop cycle K:
        update_state() is called with data.set_current = 0 (correct)
        data.trigger_phase_switch = True

        Execution:
          1. phase_switch_thread is None → guard passes
          2. cp_module.set_current(0) → Evse.set_current(0) → Modbus write OK
          3. trigger_phase_switch=True → __thread_phase_switch(3) starts THREAD C

─────────────────────────────────────────────────────────────────────────────
T~1.1s+ THREAD C (perform_phase_switch) begins:
          Step 1: evse_client.set_current(0)
            ← This is the SECOND zero-write. At this point set_current(0)
               was already written in step 2 above. The EVSE cache
               evse_current == 0, so this write is SKIPPED:
               if self.evse_current != formatted_current → 0 == 0 → skip
            ← The EVSE has 0A. Good so far.
          Step 2: time.sleep(5)

─────────────────────────────────────────────────────────────────────────────
T~1.2s  Handler loop cycle K+1 (runs while THREAD C sleeps):
        update_state() is called
          1. phase_switch_thread.is_alive() = True → RETURN immediately ✓
        [No EVSE write happens. Correct.]

─────────────────────────────────────────────────────────────────────────────
T~2.3s  Handler loop cycle K+2: same → returns early ✓

─────────────────────────────────────────────────────────────────────────────
T~3.4s  Handler loop cycle K+3: same → returns early ✓

─────────────────────────────────────────────────────────────────────────────
T~4.5s  Handler loop cycle K+4: same → returns early ✓

─────────────────────────────────────────────────────────────────────────────
T~5.5s  Handler loop cycle K+5: same → returns early ✓

─────────────────────────────────────────────────────────────────────────────
T~6.1s  THREAD C continues:
          Step 3: GPIO.output(gpio_cp, GPIO.HIGH)   ← CP off
          Step 4: GPIO.output(gpio_relay, GPIO.HIGH) ← RELAY SWITCHES

        MEANWHILE: Handler loop cycle K+6 starts at approximately T~6.6s
        At this moment, phase_switch_thread is STILL alive (THREAD C is in
        the middle of its second 5-second sleep). Guard works. ✓

─────────────────────────────────────────────────────────────────────────────
T~10s   Main loop cycle N+1:
        Suppose PV surplus is recalculated. Due to meter lag or measurement
        noise, the algorithm determines that 3-phase charging is still valid
        AND the state has transitioned back to CHARGING_ALLOWED (or
        WAIT_FOR_USING_PHASES). The algorithm calls set_required_currents()
        and assigns cp.data.set.current = 10A (for example).

        process._update_state(cp):
          state = WAIT_FOR_USING_PHASES (NOT PERFORMING_PHASE_SWITCH any more,
          since the main loop does not know THREAD C is still alive).
          → current is NOT zeroed.

        THREAD B starts: chargepoint_module.set_current(10)
          → pub_single("...data/set_current", 10)
          [MQTT message published]

─────────────────────────────────────────────────────────────────────────────
T~10.5s CRITICAL WINDOW:

        THREAD C is still running:
          - Currently between Step 4 (GPIO relay HIGH) and Step 6 (GPIO relay LOW)
          - The relay IS IN TRANSITION

        Handler loop cycle K+N receives MQTT set_current=10.
        At this point, THREAD C is alive → guard fires → returns early.

        BUT: The MQTT message is queued in the broker. The next handler loop
        cycle that runs AFTER THREAD C dies will read set_current=10 from the
        data snapshot and write it to EVSE.

─────────────────────────────────────────────────────────────────────────────
T~11.1s THREAD C continues:
          Step 6: GPIO.output(gpio_relay, GPIO.LOW)   ← relay settles
          Step 7: time.sleep(5)

─────────────────────────────────────────────────────────────────────────────
T~16.1s THREAD C continues:
          Step 8: GPIO.output(gpio_cp, GPIO.LOW)      ← CP on
          Step 9: time.sleep(1)

        THREAD C exits at T~17.1s

─────────────────────────────────────────────────────────────────────────────
T~17.2s Handler loop cycle K+M:
        phase_switch_thread.is_alive() = False → guard does NOT fire
        update_state() proceeds:
          cp_module.set_current(10)
          → Evse.set_current(10): evse_current == 0, 10 ≠ 0 → WRITES to EVSE
          EVSE register 1000 = 10A
          Vehicle begins drawing current on 3 phases ✓

        [This last step is actually correct, but see the next scenario below]
```

### The Core Bug: `set_current` and `trigger_phase_switch` Processed in Wrong Order (Same Handler Cycle)

The above timeline is the **happy path**. The problematic path is a direct consequence of **Bug 4.1**: within a single handler loop cycle, `set_current(N)` is written to the EVSE **before** `trigger_phase_switch` is checked. This is not a cross-thread race or an MQTT delivery ordering issue — it is a deterministic ordering bug within a single thread of execution.

When both `set_current > 0` and `trigger_phase_switch = True` are present in the same handler cycle's data snapshot (which occurs naturally when the main control loop publishes them in close succession and the handler loop's 1.1s window captures both), the sequence is:

```
Handler cycle K (single thread, sequential execution):
  1. phase_switch_thread is None or not alive → guard passes
  2. cp_module.set_current(N) → EVSE register = N Ampere    ← PROBLEMATIC
  3. Pub().pub(set_current)
  4. trigger_phase_switch = True → __thread_phase_switch(3) starts THREAD C
  5. THREAD C begins: evse_client.set_current(0) → EVSE = 0  ← corrects it
  6. time.sleep(5) → GPIO toggle occurs 5s later
```

Between steps 2 and 5, the EVSE holds N amperes. This window is short (milliseconds), and the 5-second delay before GPIO toggle provides margin for the vehicle to stop. **The real danger is if step 5 fails silently** (Bug 4.2) — in that case, the EVSE retains N amperes through the entire 5-second wait and the subsequent relay toggle.

The fix is straightforward: check `trigger_phase_switch` **before** calling `set_current()`, force a zero-write, and return early (see Recommendation Fix 1).

---

## 6. Reproduction Conditions

The bug is most likely to manifest under these conditions:

1. **PV mode with automatic 1→3 or 3→1 phase switching enabled**
2. **The PV surplus oscillates near the switch threshold** — causing the switch-on delay timer to expire at approximately the same time as a new `set_current` assignment from the algorithm
3. **High MQTT broker latency or queue depth** — causing `trigger_phase_switch` and `set_current` MQTT messages to be delivered in the same handler loop cycle
4. **Satellite (secondary openWB) configuration** — the primary and satellite have separate MQTT brokers, increasing message delivery jitter
5. **Short control interval** (e.g., 10 seconds) — reduces the time between algorithm cycles, narrowing the synchronization window
6. **Modbus bus under load** — a busy RS-485 bus increases the probability that `set_current(0)` writes fail silently

---

## 7. Impact Assessment

> **Methodology note:** This assessment is based entirely on static code analysis. No field testing, log analysis, or hardware reproduction was performed. The likelihood ratings below are theoretical estimates. Real-world occurrence depends on factors such as MQTT broker performance, Modbus bus load, PV surplus volatility, and vehicle-specific charging behavior.

| Consequence | Likelihood | Severity |
|-------------|-----------|---------|
| EVSE holds non-zero current when relay switches (Bug 4.1 + 4.2 combined) | Low–Medium | High — live switching under load, potential arc damage to relay contacts |
| Silent Modbus write failure → relay fires with EVSE still armed (Bug 4.2) | Low | High — same as above; mitigated by 5s delay if write eventually succeeds |
| Concurrent Modbus bus access without mutex → framing errors (Bug 4.5) | Medium | Low — causes spurious errors but is self-healing on next cycle |
| EVSE cache desynchronized, wrong current applied (Bug 4.4) | Low | Low–Medium — incorrect charging behavior until next `get_values()` cycle (1.1s) |
| `old_phases_in_use` wrong at startup → incorrect max-current cap (Bug 4.3) | Low | Low–Medium — bounded ~25% overcurrent (20A vs 16A) on 20A EVSEs; hardware max_current register prevents unlimited bypass |
| Vehicle ignores CP interruption signal due to timing | Low | Medium — 5s delay provides margin per IEC 61851; risk only if EVSE write fails |

---

## 8. Recommendations

### Fix 1 (Critical): Reorder `set_current` and `trigger_phase_switch` in `update_state()`

**File:** `packages/modules/internal_chargepoint_handler/internal_chargepoint_handler.py`

```python
def update_state(self, data, heartbeat_expired):
    set_current = 0 if heartbeat_expired else data.set_current

    if self.phase_switch_thread and self.phase_switch_thread.is_alive():
        return
    if self.cp_interruption_thread and self.cp_interruption_thread.is_alive():
        return

    # FIX: Check trigger_phase_switch BEFORE writing set_current
    if data.trigger_phase_switch:
        self.cp_module.set_current(0)   # force zero before switch
        self.__thread_phase_switch(data.phases_to_use)
        Pub().pub("...trigger_phase_switch", False)
        return  # do not write set_current in this cycle
    
    self.cp_module.set_current(set_current)
    Pub().pub(...)
```

This eliminates the ordering race by ensuring a zero-current write happens atomically with phase switch initiation, and prevents any non-zero current from reaching the EVSE in the same cycle.

### Fix 2 (Critical): Verify EVSE Zero-Current Before Toggling Relay

**File:** `packages/modules/internal_chargepoint_handler/chargepoint_module.py`

Add a readback loop in `perform_phase_switch()` before touching GPIO:

```python
def perform_phase_switch(self, phases_to_use: int) -> None:
    gpio_cp, gpio_relay = self._client.get_pins_phase_switch(phases_to_use)
    
    # Write zero and verify with readback
    for attempt in range(3):
        try:
            self._client.evse_client.set_current(0)
            time.sleep(0.2)
            plugged, charging, evse_current = self._client.evse_client.get_plug_charge_state()
            if evse_current == 0:
                break
        except Exception:
            log.warning(f"EVSE zero-write verification failed, attempt {attempt+1}")
    else:
        log.error("EVSE could not be set to 0A before phase switch. Aborting relay toggle.")
        return
    
    time.sleep(5)
    GPIO.output(gpio_cp, GPIO.HIGH)  # CP off
    GPIO.output(gpio_relay, GPIO.HIGH)
    time.sleep(5)
    GPIO.output(gpio_relay, GPIO.LOW)
    time.sleep(5)
    GPIO.output(gpio_cp, GPIO.LOW)  # CP on
    time.sleep(1)
```

### Fix 3 (High): Add a Mutex to `Evse` for Multi-Thread Access

**File:** `packages/modules/common/evse.py`

> **Caution:** A naive `threading.Lock` on `Evse` could introduce priority inversion or deadlocks. The handler loop calls `get_plug_charge_state()` every 1.1s (holding the lock for the duration of a Modbus read), while the phase switch thread calls `set_current(0)` (needing the lock for a Modbus write). If the lock is held during the full Modbus transaction timeout, the other thread could be blocked for seconds. Consider using `threading.RLock` if re-entrant calls are possible, and ensure lock hold times are bounded. Alternatively, Fix 5 (skipping EVSE reads during phase switch) may be a simpler and safer approach to eliminating the contention.

```python
import threading

class Evse:
    def __init__(self, modbus_id, client):
        self._lock = threading.Lock()
        ...

    def get_plug_charge_state(self):
        with self._lock:
            ...

    def set_current(self, current, phases_in_use=None):
        with self._lock:
            ...
```

### Fix 4 (Medium): Update `evse_current` Cache After Successful Write

**File:** `packages/modules/common/evse.py`

```python
def set_current(self, current, phases_in_use=None):
    ...
    formatted_current = round(current * 100) if self._precise_current else round(current)
    if self.evse_current != formatted_current:
        self.client.write_register(1000, formatted_current, unit=self.id)
        self.evse_current = formatted_current  # ← update cache after write
```

This ensures the cache reflects the intended state even before the next `get_plug_charge_state()` call.

### Fix 5 (Medium): Skip EVSE Modbus Read During Active Phase Switch

**File:** `packages/modules/internal_chargepoint_handler/chargepoint_module.py`

In `get_values()`, when `phase_switch_cp_active` is True, skip the EVSE Modbus read entirely (not just freeze `plug_state`) to avoid bus contention:

```python
def get_values(self, phase_switch_cp_active, last_tag):
    with self.client_error_context:
        if phase_switch_cp_active:
            # During phase switch, don't read EVSE to avoid bus contention
            # Return last known state
            store_state(self.old_chargepoint_state)
            return self.old_chargepoint_state
        
        evse_state, counter_state = self._client.request_and_check_hardware(self.fault_state)
        ...
```

### Fix 6 (Low): Validate `old_phases_in_use` at Startup from Hardware

**File:** `packages/modules/internal_chargepoint_handler/chargepoint_module.py`

After the MQTT finite loop, if `old_phases_in_use` remains at the default (1), read the relay GPIO state directly to determine the actual hardware phase configuration, rather than trusting a potentially missing MQTT retained message.

---

## Summary

> **Root cause consolidation:** While this report lists 8 numbered findings, several are facets of the same underlying defect. The distinct root causes are:
> 1. **Ordering bug in `update_state()`** (4.1) — the primary, directly exploitable issue
> 2. **No Modbus write verification in `perform_phase_switch()`** (4.2, 4.6, 4.7) — three findings describing the same missing safety check from different angles
> 3. **Unsynchronized concurrent access to `Evse`** (4.5, 4.8) — architectural threading concern
> 4. **Startup initialization race** (4.3) — edge case with bounded impact
> 5. **Stale cache** (4.4) — low-probability secondary effect

The core structural defect is that the phase switch initiation path in `update_state()` writes a potentially non-zero current to the EVSE **before** checking whether a phase switch should be initiated in the same cycle. This is a deterministic ordering bug, not a probabilistic race condition, and is straightforward to fix (see Recommendation Fix 1).

The secondary defect is the absence of Modbus write verification in `perform_phase_switch()`: the `set_current(0)` write is wrapped in a `SingleComponentUpdateContext` that swallows exceptions, and there is no readback confirmation before GPIO relay toggling proceeds. The 5-second delay before relay toggle provides engineering margin per IEC 61851 (vehicles must stop within 3s), but this margin is only effective if the zero-write succeeded.

The remaining findings (concurrent Modbus access without mutex, stale EVSE cache, startup initialization) are valid concerns but have lower practical impact due to existing mitigations (the `is_alive()` guard, the 1.1s cache refresh cycle, and EVSE hardware current limits).

**This analysis is based on static code review only.** Field testing, log analysis, or hardware reproduction would be needed to confirm whether the identified race conditions manifest in practice and under what conditions.
