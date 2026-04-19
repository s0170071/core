# Relay Safety Check — Proof of Concept Plan

**Goal:** Verify that the bugs identified in `report.md` manifest in practice by detecting
when phase-switch relays are toggled while the EVSE still outputs a non-zero current,
and alerting via email.

---

## 1. Current State of the Code

### 1.1 Relay Access Is NOT Centralized

GPIO relay pins are toggled via **direct `GPIO.output()` calls** in two methods of
`packages/modules/internal_chargepoint_handler/chargepoint_module.py`:

| Method                     | Lines     | Relay pins touched                |
|----------------------------|-----------|-----------------------------------|
| `perform_phase_switch()`   | 149–154   | `gpio_relay` (29/37 or 11/13)    |
| `perform_cp_interruption()`| 165–167   | `gpio_cp` only (22 or 15) — no relay |

Only `perform_phase_switch()` touches the **phase-switch relay pins**.
There is no wrapper function — `GPIO.output(gpio_relay, …)` is called inline.

### 1.2 EVSE Current Read

`packages/modules/common/evse.py → Evse.get_plug_charge_state()` reads register 1000:

```python
raw_set_current, _, state_number = self.client.read_holding_registers(
    1000, [ModbusDataType.UINT_16]*3, unit=self.id)
self.evse_current = int(raw_set_current)
```

The cached field `self.evse_current` is **only** refreshed by this read.
`set_current()` does **not** update the cache after writing — see report Bug 4.4.

### 1.3 Exception Swallowing

`SingleComponentUpdateContext(fault_state, update_always=False)` wraps the `set_current(0)`
call inside `perform_phase_switch()`. Its `__exit__` returns `True` unconditionally
(when `reraise=False`), silently swallowing any Modbus exception. This is report Bug 4.2.

### 1.4 No Existing Email Facility

The Python codebase has no `smtplib` usage. We will add a minimal standalone email sender.

---

## 2. Design

### 2.1 Introduce a Centralized `safe_relay_output()` Function

**Why:** All relay GPIO toggles should pass through a single gate that reads the EVSE
register and detects the dangerous condition. This prevents future relay call-sites from
bypassing the check.

**Where:** New file `packages/modules/internal_chargepoint_handler/relay_safety.py`.

The function:
1. Reads the EVSE hardware register 1000 (retry up to 3 times on Modbus failure).
2. If the read value is > 0, logs a CRITICAL message and sends the alert email.
3. Executes the `GPIO.output()` call regardless (we do not block charging — this is a
   **detection** PoC, not a fix).

### 2.2 Patch `perform_phase_switch()` to Use the Central Function

Replace the four bare `GPIO.output(gpio_relay, …)` calls with `safe_relay_output()`.
The CP-signal pins (22/15) are **not** phase-switch relays and carry only the pilot
signal, so they do not need the EVSE check — but we route them through the same function
with a flag to skip the check, for future extensibility.

### 2.3 Email Alerting

Use Python's built-in `smtplib` + `email.mime` to send a plain-text email via
Posteo's SMTP server (`posteo.de:587`, STARTTLS). The email contains:
- Timestamp
- Chargepoint number
- EVSE register value (the non-zero current)
- GPIO pin being toggled
- Which direction (HIGH/LOW)

> **IMPORTANT:** The PoC needs SMTP credentials for `drk@posteo.de` configured at
> runtime. The plan stores them in a JSON config file that is `.gitignore`d.

---

## 3. File-by-File Changes

### 3.1 NEW: `packages/modules/internal_chargepoint_handler/relay_safety.py`

```python
#!/usr/bin/env python3
"""Relay safety check — proof of concept.

Before every relay GPIO toggle, read the EVSE register 1000 to verify the
set-current is 0.  If it is not, send an alert email and log the event.
"""

import json
import logging
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except ImportError:
    log.info("failed to import RPi.GPIO! maybe we are not running on a pi")

# ---------- configuration -------------------------------------------------- #

_EMAIL_CONFIG_PATH = Path(__file__).resolve().parent / "relay_safety_email.json"
# Expected JSON structure:
# {
#     "smtp_host": "posteo.de",
#     "smtp_port": 587,
#     "smtp_user": "drk@posteo.de",
#     "smtp_password": "<app-password>",
#     "recipient": "drk@posteo.de"
# }

_RELAY_PINS = {29, 37, 11, 13}  # phase-switch relay GPIO pins (BOARD numbering)


def _load_email_config() -> Optional[dict]:
    """Load SMTP config from JSON.  Returns None if file missing or invalid."""
    try:
        with open(_EMAIL_CONFIG_PATH) as f:
            cfg = json.load(f)
        for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "recipient"):
            if key not in cfg:
                log.error("relay_safety_email.json missing key: %s", key)
                return None
        return cfg
    except FileNotFoundError:
        log.warning("No relay_safety_email.json found — email alerts disabled.")
        return None
    except Exception:
        log.exception("Failed to load relay_safety_email.json")
        return None


def _send_alert_email(evse_current: float, gpio_pin: int, direction: str,
                      cp_num: int, timestamp: str) -> None:
    """Send a plain-text alert email.  Best-effort; logs but does not raise."""
    cfg = _load_email_config()
    if cfg is None:
        return
    subject = (f"[openWB RELAY SAFETY] EVSE current {evse_current}A "
               f"at relay toggle — CP{cp_num}")
    body = (
        f"RELAY SAFETY ALERT\n"
        f"==================\n\n"
        f"Timestamp  : {timestamp}\n"
        f"Chargepoint: {cp_num}\n"
        f"GPIO pin   : {gpio_pin}\n"
        f"Direction  : {direction}\n"
        f"EVSE reg1000 (set current): {evse_current}\n\n"
        f"The EVSE register held a non-zero current value at the moment\n"
        f"the relay GPIO was about to be toggled.  This confirms the race\n"
        f"condition described in report.md (Bugs 4.1 / 4.2 / 4.7).\n"
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg["smtp_user"]
    msg["To"] = cfg["recipient"]
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10) as srv:
            srv.starttls()
            srv.login(cfg["smtp_user"], cfg["smtp_password"])
            srv.sendmail(cfg["smtp_user"], [cfg["recipient"]], msg.as_string())
        log.warning("Relay safety alert email sent successfully.")
    except Exception:
        log.exception("Failed to send relay safety alert email")


def _read_evse_current_from_hardware(evse_client, max_retries: int = 3) -> Optional[float]:
    """Read EVSE register 1000 directly from hardware.  Retries on failure.

    Returns the raw register value, or None if all retries fail.
    """
    for attempt in range(1, max_retries + 1):
        try:
            plugged, charging, evse_current = evse_client.get_plug_charge_state()
            log.debug("Relay safety: EVSE read attempt %d/%d — evse_current=%s",
                      attempt, max_retries, evse_current)
            return evse_current
        except Exception:
            log.warning("Relay safety: EVSE read attempt %d/%d failed", attempt, max_retries)
            if attempt < max_retries:
                time.sleep(0.2)
    return None


def safe_relay_output(gpio_pin: int, value, evse_client, cp_num: int = 0,
                      check_evse: bool = True) -> None:
    """Drop-in replacement for ``GPIO.output(gpio_pin, value)`` on relay pins.

    Parameters
    ----------
    gpio_pin : int
        Board-numbering GPIO pin.
    value : int
        GPIO.HIGH or GPIO.LOW.
    evse_client : evse.Evse
        The EVSE Modbus client for this chargepoint.
    cp_num : int
        Local chargepoint number (0 or 1).
    check_evse : bool
        If False, skip the EVSE read (use for CP-signal pins).
    """
    direction = "HIGH" if value == GPIO.HIGH else "LOW"
    is_relay_pin = gpio_pin in _RELAY_PINS

    if check_evse and is_relay_pin:
        evse_current = _read_evse_current_from_hardware(evse_client)
        timestamp = datetime.now().isoformat()
        if evse_current is None:
            log.critical(
                "RELAY SAFETY: Could not read EVSE before relay toggle! "
                "pin=%d dir=%s cp=%d", gpio_pin, direction, cp_num)
            _send_alert_email(-1, gpio_pin, direction, cp_num, timestamp)
        elif evse_current > 0:
            log.critical(
                "RELAY SAFETY: EVSE current is %.2fA (non-zero) at relay toggle! "
                "pin=%d dir=%s cp=%d", evse_current, gpio_pin, direction, cp_num)
            _send_alert_email(evse_current, gpio_pin, direction, cp_num, timestamp)
        else:
            log.debug(
                "Relay safety OK: EVSE current=0 before relay toggle "
                "pin=%d dir=%s cp=%d", gpio_pin, direction, cp_num)

    # Always proceed with the GPIO toggle (detection PoC, not a block).
    GPIO.output(gpio_pin, value)
```

### 3.2 NEW: `packages/modules/internal_chargepoint_handler/relay_safety_email.json`

This file must be created manually on the target system with real credentials.
It is **not** committed to the repository.

```json
{
    "smtp_host": "posteo.de",
    "smtp_port": 587,
    "smtp_user": "drk@posteo.de",
    "smtp_password": "REPLACE_WITH_APP_PASSWORD",
    "recipient": "drk@posteo.de"
}
```

### 3.3 MODIFY: `packages/modules/internal_chargepoint_handler/chargepoint_module.py`

**Add import** (top of file):

```python
from modules.internal_chargepoint_handler.relay_safety import safe_relay_output
```

**Replace `perform_phase_switch()`** (lines 144–155):

```python
    def perform_phase_switch(self, phases_to_use: int) -> None:
        gpio_cp, gpio_relay = self._client.get_pins_phase_switch(phases_to_use)
        with SingleComponentUpdateContext(self.fault_state, update_always=False):
            self._client.evse_client.set_current(0)
        time.sleep(5)
        # CP pin — no EVSE check needed (pilot signal, not power relay)
        safe_relay_output(gpio_cp, GPIO.HIGH, self._client.evse_client,
                          cp_num=self.local_charge_point_num, check_evse=False)
        # RELAY pin — this is where the safety check fires
        safe_relay_output(gpio_relay, GPIO.HIGH, self._client.evse_client,
                          cp_num=self.local_charge_point_num)
        time.sleep(5)
        safe_relay_output(gpio_relay, GPIO.LOW, self._client.evse_client,
                          cp_num=self.local_charge_point_num)
        time.sleep(5)
        safe_relay_output(gpio_cp, GPIO.LOW, self._client.evse_client,
                          cp_num=self.local_charge_point_num, check_evse=False)
        time.sleep(1)
```

### 3.4 ADD to `.gitignore`

```
relay_safety_email.json
```

---

## 4. To-Do Checklist

- [ ] **4.1** Create `packages/modules/internal_chargepoint_handler/relay_safety.py`
      with the code from §3.1.
- [ ] **4.2** Create a template
      `packages/modules/internal_chargepoint_handler/relay_safety_email.json.example`
      (committed) with placeholder credentials.
- [ ] **4.3** On the target Raspberry Pi, copy the template to
      `relay_safety_email.json` and fill in the real Posteo app-password.
- [ ] **4.4** Add `relay_safety_email.json` to `.gitignore`.
- [ ] **4.5** Add the import line to `chargepoint_module.py` (top of file).
- [ ] **4.6** Replace the body of `perform_phase_switch()` in
      `chargepoint_module.py` with the patched version from §3.3.
- [ ] **4.7** Run the unit test (§5) to verify the detection logic
      independently of hardware.
- [ ] **4.8** Deploy to the openWB and trigger a phase switch under PV surplus
      conditions. Monitor logs and inbox.

---

## 5. Verification & Testing

### 5.1 Offline Unit Test (no hardware required)

Create `packages/modules/internal_chargepoint_handler/relay_safety_test.py`:

```python
"""Unit tests for relay_safety — run with pytest."""

import types
from unittest.mock import MagicMock, patch, call
import pytest


def _make_evse_client(current_value: float, fail: bool = False):
    """Return a mock EVSE client that returns the given current (or raises)."""
    client = MagicMock()
    if fail:
        client.get_plug_charge_state.side_effect = Exception("Modbus timeout")
    else:
        client.get_plug_charge_state.return_value = (True, True, current_value)
    return client


# Patch GPIO before importing relay_safety so it loads on non-Pi machines.
GPIO_mock = MagicMock()
GPIO_mock.HIGH = 1
GPIO_mock.LOW = 0

with patch.dict("sys.modules", {"RPi": MagicMock(), "RPi.GPIO": GPIO_mock}):
    from modules.internal_chargepoint_handler.relay_safety import (
        safe_relay_output,
        _read_evse_current_from_hardware,
        _send_alert_email,
        _RELAY_PINS,
    )


class TestReadEvseCurrentFromHardware:
    def test_returns_current_on_success(self):
        client = _make_evse_client(6.0)
        assert _read_evse_current_from_hardware(client, max_retries=1) == 6.0

    def test_returns_none_after_all_retries_fail(self):
        client = _make_evse_client(0, fail=True)
        assert _read_evse_current_from_hardware(client, max_retries=2) is None
        assert client.get_plug_charge_state.call_count == 2

    def test_retries_on_failure_then_succeeds(self):
        client = MagicMock()
        client.get_plug_charge_state.side_effect = [
            Exception("timeout"),
            (True, True, 0.0),
        ]
        assert _read_evse_current_from_hardware(client, max_retries=2) == 0.0


class TestSafeRelayOutput:
    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_alert_sent_when_evse_current_nonzero(self, mock_email):
        client = _make_evse_client(10.0)
        safe_relay_output(29, GPIO_mock.HIGH, client, cp_num=0)
        mock_email.assert_called_once()
        args = mock_email.call_args[0]
        assert args[0] == 10.0   # evse_current
        assert args[1] == 29     # gpio_pin

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_no_alert_when_evse_current_zero(self, mock_email):
        client = _make_evse_client(0.0)
        safe_relay_output(29, GPIO_mock.HIGH, client, cp_num=0)
        mock_email.assert_not_called()

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_alert_sent_when_evse_read_fails(self, mock_email):
        client = _make_evse_client(0, fail=True)
        safe_relay_output(37, GPIO_mock.HIGH, client, cp_num=0)
        mock_email.assert_called_once()
        args = mock_email.call_args[0]
        assert args[0] == -1  # sentinel for read failure

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_no_check_on_cp_pin(self, mock_email):
        """CP-signal pins should not trigger an EVSE check."""
        client = _make_evse_client(16.0)
        safe_relay_output(22, GPIO_mock.HIGH, client, cp_num=0, check_evse=False)
        mock_email.assert_not_called()

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_no_check_on_non_relay_pin(self, mock_email):
        """Pins not in _RELAY_PINS should not trigger an EVSE check."""
        client = _make_evse_client(16.0)
        safe_relay_output(22, GPIO_mock.HIGH, client, cp_num=0, check_evse=True)
        # pin 22 is NOT in _RELAY_PINS, so no check should happen
        mock_email.assert_not_called()

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_gpio_always_toggled_even_on_alert(self, mock_email):
        """The GPIO must always be toggled (detection PoC, not a block)."""
        client = _make_evse_client(10.0)
        safe_relay_output(29, GPIO_mock.HIGH, client, cp_num=0)
        GPIO_mock.output.assert_called_with(29, GPIO_mock.HIGH)
```

**Run with:**

```bash
cd /opt/openWB
python -m pytest packages/modules/internal_chargepoint_handler/relay_safety_test.py -v
```

### 5.2 On-Device Log Verification

After deploying to the Raspberry Pi:

1. SSH into the openWB.
2. Tail the internal chargepoint log:
   ```bash
   tail -f /var/log/openWB/internal_chargepoint.log | grep -i "relay safety"
   ```
3. Wait for a PV-mode phase switch to trigger (or manually trigger one via the
   settings UI: set phase switching to "automatic" with a low threshold).
4. Look for either:
   - `Relay safety OK: EVSE current=0` — the safe case, no email sent.
   - `RELAY SAFETY: EVSE current is X.XXA (non-zero)` — **bug confirmed**,
     email will be sent.
   - `RELAY SAFETY: Could not read EVSE` — Modbus failure before relay toggle,
     email sent with sentinel value `-1`.

### 5.3 Email Verification

1. Check the inbox of `drk@posteo.de` for messages with subject
   `[openWB RELAY SAFETY]`.
2. If an email arrives, the body contains the exact EVSE register value and
   timestamp, confirming the relay was toggled while current was non-zero.
3. **No email = no bug manifestation during that specific phase switch.**
   Allow multiple phase switches (varying PV conditions) before concluding.

### 5.4 Forced Test (Optional — Validates the Email Path)

To verify the email pipeline works without waiting for the bug to manifest
naturally, temporarily modify `relay_safety.py` to always flag:

```python
# TEMPORARY — remove after verification
elif evse_current >= 0:   # was: > 0
```

This will send an email on **every** relay toggle, even when current is 0.
Trigger one phase switch, confirm the email arrives, then revert the change.

---

## 6. What This Proves

| Observation                               | Report finding confirmed     |
|-------------------------------------------|------------------------------|
| Email received with `evse_current > 0`    | Bugs 4.1, 4.2, 4.7 — EVSE holds current at relay toggle |
| Email received with `evse_current = -1`   | Bug 4.6 — Modbus read failure; write may also have failed |
| Multiple emails in one phase switch       | Bug 4.5 — concurrent Modbus access |
| No emails after many phase switches       | Bugs exist in code but did not manifest under test conditions |

---

## 7. Risk Assessment of This PoC

| Concern | Mitigation |
|---------|------------|
| Extra Modbus read adds ~100ms before each relay toggle | Acceptable; the 5s sleep already dominates the sequence |
| Modbus read failure could delay the relay toggle | Retry loop is capped at 3 attempts × 0.2s = 0.6s max |
| Email send blocks the phase-switch thread | `smtplib` timeout is 10s; worst case adds 10s to phase switch. Use a background thread for production. Acceptable for PoC |
| Credentials on disk | `.gitignore`d; file permissions should be `600`. Not production-grade — acceptable for PoC |

---

## 8. Relationship to Report Recommendations

This PoC is **detection only**. It does not implement the fixes from the report.
The mapping is:

| Report Recommendation | PoC relationship |
|----------------------|------------------|
| Fix 1: Reorder `set_current` / `trigger_phase_switch` | Not implemented — ordering bug remains; PoC detects its effect |
| Fix 2: Verify EVSE zero before relay toggle | PoC reads EVSE but does NOT abort the toggle; it only alerts |
| Fix 3: Mutex on `Evse` | Not implemented |
| Fix 4: Update `evse_current` cache after write | Not implemented |
| Fix 5: Skip EVSE read during phase switch | Not implemented (would conflict with PoC's own read) |

The PoC confirms whether the described conditions occur in the field.
Once confirmed, the report's Fix 1 + Fix 2 should be implemented as the actual remedy.
