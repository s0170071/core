#!/usr/bin/env python3
import logging
from enum import IntEnum
import time
from typing import Optional, Tuple
from helpermodules.logger import ModifyLoglevelContext

from modules.common import modbus
from modules.common.component_state import EvseState
from modules.common.modbus import ModbusDataType

log = logging.getLogger(__name__)
evse_relay_log = logging.getLogger("evse_relay")


class EvseStatusCode(IntEnum):
    READY = (1, False, False)
    EV_PRESENT = (2, True, False)
    CHARGING = (3, True, True)
    CHARGING_WITH_VENTILATION = (4, True, True)
    FAILURE = (5, None, None)

    def __new__(cls, num: int, plugged: Optional[bool], charge_enabled: Optional[bool]):
        member = int.__new__(cls, num)
        member._value_ = num
        member.plugged = plugged
        member.charge_enabled = charge_enabled
        return member


class Evse:
    PRECISE_CURRENT_BIT = 1 << 7

    def __init__(self, modbus_id: int, client: modbus.ModbusSerialClient_) -> None:
        self.client = client
        self.id = modbus_id
        with client:
            time.sleep(0.1)
            self.version = self.client.read_holding_registers(1005, ModbusDataType.UINT_16, unit=self.id)
            time.sleep(0.1)
            self.max_current = self.client.read_holding_registers(2007, ModbusDataType.UINT_16, unit=self.id)
            with ModifyLoglevelContext(log, logging.DEBUG):
                log.debug(f"Firmware-Version der EVSE: {self.version}")
            if self.version < 17:
                self._precise_current = False
            else:
                if self.is_precise_current_active() is False:
                    self.activate_precise_current()
                self._precise_current = self.is_precise_current_active()
        self._toggle_timestamps = []
        self._last_was_zero = None
        # Debounce: timestamp of the last non-zero current write. Used to suppress
        # zero-writes that happen within ZERO_WRITE_DEBOUNCE_S of starting charge,
        # which prevents very brief enable/disable cycles seen with borderline PV surplus.
        # Phase switches and CP interruptions bypass this via force=True.
        self._last_nonzero_write_ts = 0.0

    ZERO_WRITE_DEBOUNCE_S = 60.0

    def get_plug_charge_state(self) -> Tuple[bool, bool, float]:
        time.sleep(0.1)
        raw_set_current, _, state_number = self.client.read_holding_registers(
            1000, [ModbusDataType.UINT_16]*3, unit=self.id)
        # remove leading zeros
        self.evse_current = int(raw_set_current)
        log.debug("Gesetzte Stromstärke EVSE: "+str(self.evse_current) +
                  ", Status: "+str(state_number)+", Modbus-ID: "+str(self.id))
        state = EvseStatusCode(state_number)
        if state == EvseStatusCode.FAILURE:
            raise ValueError("Unbekannter Zustand der EVSE: State " +
                             str(state)+", Soll-Stromstärke: "+str(self.evse_current))
        plugged = state.plugged
        charging = self.evse_current > 0 if state.charge_enabled else False
        # Convert to amps for the return value only; keep self.evse_current in raw register units
        # so it matches the format stored by set_current() and the guard comparison works correctly.
        set_current_amps = self.evse_current / 100 if self._precise_current else float(self.evse_current)
        return plugged, charging, set_current_amps

    def get_firmware_version(self) -> int:
        return self.version

    def get_evse_state(self) -> EvseState:
        plugged, charging, set_current = self.get_plug_charge_state()
        state = EvseState(plug_state=plugged,
                          charge_state=charging,
                          set_current=set_current,
                          max_current=self.max_current)
        return state

    def is_precise_current_active(self) -> bool:
        time.sleep(0.1)
        value = self.client.read_holding_registers(2005, ModbusDataType.UINT_16, unit=self.id)
        with ModifyLoglevelContext(log, logging.DEBUG):
            if value & self.PRECISE_CURRENT_BIT:
                log.debug("Angabe der Ströme in 0,01A-Schritten ist aktiviert.")
                return True
            else:
                log.debug("Angabe der Ströme in 0,01A-Schritten ist nicht aktiviert.")
                return False

    def activate_precise_current(self) -> None:
        time.sleep(0.1)
        value = self.client.read_holding_registers(2005, ModbusDataType.UINT_16, unit=self.id)
        if value & self.PRECISE_CURRENT_BIT:
            return
        else:
            with ModifyLoglevelContext(log, logging.DEBUG):
                log.debug("Bit zur Angabe der Ströme in 0,01A-Schritten wird gesetzt.")
            self.client.write_registers(2005, value ^ self.PRECISE_CURRENT_BIT, unit=self.id)
            # Zeit zum Verarbeiten geben
            time.sleep(1)

    def deactivate_precise_current(self) -> None:
        time.sleep(0.1)
        value = self.client.read_holding_registers(2005, ModbusDataType.UINT_16, unit=self.id)
        if value & self.PRECISE_CURRENT_BIT:
            with ModifyLoglevelContext(log, logging.DEBUG):
                log.debug("Bit zur Angabe der Ströme in 0,01A-Schritten wird zurueckgesetzt.")
            self.client.write_registers(2005, value ^ self.PRECISE_CURRENT_BIT, unit=self.id)
        else:
            return

    def set_current(self, current: int, phases_in_use: Optional[int] = None, force: bool = False) -> None:
        time.sleep(0.1)
        if self.max_current == 20 and phases_in_use is not None and phases_in_use != 0:
            # Bei 20A EVSE und bekannter Phasenzahl auf 16A begrenzen, sonst erstmal Ladung mit Minimalstrom starten,
            # um Phasenzahl zu ermitteln
            if current > 16 and phases_in_use > 1:
                current = 16
        formatted_current = round(current*100) if self._precise_current else round(current)
        if self.evse_current != formatted_current:
            now = time.time()
            # Low-level debounce: refuse to write 0 if a non-zero value was written less than
            # ZERO_WRITE_DEBOUNCE_S ago, unless force=True (used by phase switch / CP interruption).
            if (formatted_current == 0
                    and not force
                    and self._last_nonzero_write_ts > 0
                    and (now - self._last_nonzero_write_ts) < self.ZERO_WRITE_DEBOUNCE_S):
                evse_relay_log.info(
                    "EVSE id=%d: zero-write suppressed (%.1fs since last non-zero, debounce=%.0fs)",
                    self.id, now - self._last_nonzero_write_ts, self.ZERO_WRITE_DEBOUNCE_S)
                return
            self.client.write_registers(1000, formatted_current, unit=self.id)
            evse_relay_log.info("EVSE id=%d: set_current %.2fA (reg=%d, prev_reg=%d)%s",
                               self.id, current, formatted_current, self.evse_current,
                               " [forced]" if force else "")
            self.evse_current = formatted_current
            if formatted_current != 0:
                self._last_nonzero_write_ts = now
            is_zero = formatted_current == 0
            if self._last_was_zero is not None and is_zero != self._last_was_zero:
                self._toggle_timestamps = [t for t in self._toggle_timestamps if now - t < 120]
                self._toggle_timestamps.append(now)
                if len(self._toggle_timestamps) >= 3:
                    evse_relay_log.warning(
                        "EVSE id=%d: %d zero/non-zero toggles in 120s — possible relay loop!",
                        self.id, len(self._toggle_timestamps))
            self._last_was_zero = is_zero
