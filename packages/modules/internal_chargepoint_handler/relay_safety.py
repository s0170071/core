"""Relay safety check — proof of concept.

Before every relay GPIO toggle during a phase switch, read the EVSE register
1000 to verify the set-current is 0.  If it is not, send an alert email and
log the event.  See report.md (Bugs 4.1, 4.2, 4.7) for background.

This module is additive and does not modify any existing openWB logic.
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
# Expected JSON structure — see relay_safety_email.json.example

_RELAY_PINS = {29, 37, 11, 13}  # phase-switch relay GPIO pins (BOARD numbering)
_last_relay_state: dict = {}  # gpio_pin → last GPIO value written

_RELAY_NAMES = {
    29: "1-phase relay CP0",
    37: "3-phase relay CP0",
    11: "1-phase relay CP1",
    13: "3-phase relay CP1",
    22: "CP signal CP0",
    15: "CP signal CP1",
}


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
    evse_client :
        The EVSE Modbus client for this chargepoint (evse.Evse instance).
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

    
    relay_name = _RELAY_NAMES[gpio_pin]
    state = "ON" if value == GPIO.HIGH else "OFF"
    logging.getLogger("evse_relay").info(
        "GPIO%d: %s → %s", gpio_pin, relay_name, state)
