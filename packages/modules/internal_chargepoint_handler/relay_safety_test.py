"""Unit tests for relay_safety — run with pytest.

These tests mock RPi.GPIO and the EVSE Modbus client so they can run on any
machine, not just a Raspberry Pi.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Patch GPIO before importing relay_safety so it loads on non-Pi machines.
# The RPi mock's .GPIO attribute must point to the same object as
# sys.modules["RPi.GPIO"], because `import RPi.GPIO as GPIO` resolves
# the name via the parent package's attribute, not sys.modules directly.
# ---------------------------------------------------------------------------
GPIO_mock = MagicMock()
GPIO_mock.HIGH = 1
GPIO_mock.LOW = 0
_rpi_mock = MagicMock()
_rpi_mock.GPIO = GPIO_mock
sys.modules["RPi"] = _rpi_mock
sys.modules["RPi.GPIO"] = GPIO_mock

from modules.internal_chargepoint_handler.relay_safety import (  # noqa: E402
    safe_relay_output,
    _read_evse_current_from_hardware,
    _RELAY_PINS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_evse_client(current_value: float = 0.0, fail: bool = False):
    """Return a mock EVSE client that returns the given current (or raises)."""
    client = MagicMock()
    if fail:
        client.get_plug_charge_state.side_effect = Exception("Modbus timeout")
    else:
        client.get_plug_charge_state.return_value = (True, True, current_value)
    return client


# ---------------------------------------------------------------------------
# _read_evse_current_from_hardware
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# safe_relay_output
# ---------------------------------------------------------------------------

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
    def test_no_check_on_cp_pin_with_flag(self, mock_email):
        """CP-signal pins should not trigger an EVSE check when check_evse=False."""
        client = _make_evse_client(16.0)
        safe_relay_output(22, GPIO_mock.HIGH, client, cp_num=0, check_evse=False)
        mock_email.assert_not_called()

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_no_check_on_non_relay_pin(self, mock_email):
        """Pins not in _RELAY_PINS should not trigger an EVSE check."""
        client = _make_evse_client(16.0)
        # pin 22 is NOT in _RELAY_PINS, so no check even with check_evse=True
        safe_relay_output(22, GPIO_mock.HIGH, client, cp_num=0, check_evse=True)
        mock_email.assert_not_called()

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_gpio_always_toggled_even_on_alert(self, mock_email):
        """The GPIO must always be toggled (detection PoC, not a block)."""
        client = _make_evse_client(10.0)
        GPIO_mock.output.reset_mock()
        safe_relay_output(29, GPIO_mock.HIGH, client, cp_num=0)
        GPIO_mock.output.assert_called_with(29, GPIO_mock.HIGH)

    @patch("modules.internal_chargepoint_handler.relay_safety._send_alert_email")
    def test_all_four_relay_pins_are_checked(self, mock_email):
        """Each of the 4 relay pins (29, 37, 11, 13) should trigger a check."""
        for pin in sorted(_RELAY_PINS):
            mock_email.reset_mock()
            client = _make_evse_client(8.0)
            safe_relay_output(pin, GPIO_mock.HIGH, client, cp_num=0)
            assert mock_email.called, f"No alert for relay pin {pin}"
