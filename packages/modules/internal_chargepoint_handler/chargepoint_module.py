import logging

import time
from helpermodules.broker import BrokerClient

from helpermodules.utils import run_command
from helpermodules.utils.error_handling import CP_ERROR, ErrorTimerContext
from helpermodules.utils.topic_parser import decode_payload
from modules.common.abstract_chargepoint import AbstractChargepoint
from modules.common.component_context import SingleComponentUpdateContext
from modules.common.component_state import ChargepointState
from modules.common.fault_state import ComponentInfo, FaultState
from modules.common.store import get_internal_chargepoint_value_store, get_chargepoint_value_store
from modules.internal_chargepoint_handler.clients import ClientHandler
from modules.internal_chargepoint_handler.relay_safety import safe_relay_output, _read_evse_current_from_hardware
from helpermodules.subdata import SubData
from modules.internal_chargepoint_handler.internal_chargepoint_handler_config import InternalChargepoint

log = logging.getLogger(__name__)
evse_relay_log = logging.getLogger("evse_relay")

try:
    import RPi.GPIO as GPIO
except ImportError:
    log.info("failed to import RPi.GPIO! maybe we are not running on a pi")


class ChargepointModule(AbstractChargepoint):
    PLUG_STANDBY_POWER_THRESHOLD = 10

    def __init__(self, local_charge_point_num: int,
                 client_handler: ClientHandler,
                 internal_cp: InternalChargepoint,
                 hierarchy_id: int) -> None:
        self.local_charge_point_num = local_charge_point_num
        self.hierarchy_id = hierarchy_id
        self.fault_state = FaultState(ComponentInfo(
            local_charge_point_num,
            "Ladepunkt "+str(local_charge_point_num),
            "internal_chargepoint",
            hierarchy_id=hierarchy_id))
        self.store_internal = get_internal_chargepoint_value_store(local_charge_point_num)
        self.store = get_chargepoint_value_store(hierarchy_id)
        self.client_error_context = ErrorTimerContext(
            f"openWB/set/internal_chargepoint/{local_charge_point_num}/get/error_timestamp",
            CP_ERROR,
            hide_exception=True)
        self.client_error_context.error_timestamp = internal_cp.get.error_timestamp
        self.old_plug_state = False
        self._last_current_logged = None
        self.old_chargepoint_state = ChargepointState(plug_state=False,
                                                      charge_state=False,
                                                      imported=None,
                                                      exported=None,
                                                      currents=None,
                                                      phases_in_use=0,
                                                      power=0)
        self._client = client_handler
        self._cp_safety_asserted = False

        self.version = SubData.system_data["system"].data["version"]
        self.current_branch = SubData.system_data["system"].data["current_branch"]
        self.current_commit = SubData.system_data["system"].data["current_commit"]

        if float(run_command.run_command(["cat", "/proc/uptime"]).split(" ")[0]) < 180:
            self.perform_phase_switch(1)
            self.old_phases_in_use = 1
        else:
            def on_connect(client, userdata, flags, rc):
                client.subscribe(f"openWB/internal_chargepoint/{self.local_charge_point_num}/get/phases_in_use")

            def on_message(client, userdata, message):
                self.old_phases_in_use = decode_payload(message.payload)

            self.old_phases_in_use = 1
            BrokerClient(f"subscribeInternalCp{self.local_charge_point_num}",
                         on_connect, on_message).start_finite_loop()

    def _assert_cp_safety_stop(self) -> None:
        """Pull the CP pilot pin HIGH (disconnect) as a hardware-level safety stop
        when EVSE Modbus communication has been failing for longer than the error timeout.
        The vehicle will lose the pilot signal and stop charging even without EVSE co-operation.
        """
        gpio_cp = self._client.get_pins_cp_interruption()
        try:
            safe_relay_output(gpio_cp, GPIO.HIGH, self._client.evse_client,
                              cp_num=self.local_charge_point_num, check_evse=True)
            self._cp_safety_asserted = True
            log.error(
                "CP%d: EVSE communication failed for >%ds — CP pin GPIO%d forced HIGH (safety stop).",
                self.local_charge_point_num, self.client_error_context.timeout, gpio_cp)
        except Exception:
            log.exception("CP%d: Failed to assert CP safety stop via GPIO%d",
                          self.local_charge_point_num, gpio_cp)

    def _release_cp_safety_stop(self) -> None:
        """Pull the CP pilot pin LOW again once EVSE Modbus communication recovers."""
        gpio_cp = self._client.get_pins_cp_interruption()
        try:
            safe_relay_output(gpio_cp, GPIO.LOW, self._client.evse_client,
                              cp_num=self.local_charge_point_num, check_evse=True)
            self._cp_safety_asserted = False
            log.info(
                "CP%d: EVSE communication restored — CP pin GPIO%d released (LOW).",
                self.local_charge_point_num, gpio_cp)
        except Exception:
            log.exception("CP%d: Failed to release CP safety stop via GPIO%d",
                          self.local_charge_point_num, gpio_cp)

    def set_current(self, current: float, force: bool = False) -> None:
        applied = False
        with SingleComponentUpdateContext(self.fault_state, update_always=False):
            applied = self._client.evse_client.set_current(
                current, phases_in_use=self.old_phases_in_use, force=force)
        # Only log if the write was actually applied — if the EVSE-level debounce suppressed
        # it, the EVSE current did not change, so logging `current` here would be misleading
        # (it would look like a change happened when the write was in fact skipped).
        if applied and current != self._last_current_logged:
            evse_relay_log.info("CP%d: evse_current=%.1fA phases=%d%s",
                                self.local_charge_point_num, current, self.old_phases_in_use,
                                " [forced]" if force else "")
            self._last_current_logged = current

    def get_values(self, phase_switch_cp_active: bool, last_tag: str) -> ChargepointState:
        def store_state(chargepoint_state: ChargepointState) -> None:
            self.store.set(chargepoint_state)
            self.store.update()
            self.store_internal.set(chargepoint_state)
            self.store_internal.update()
        with self.client_error_context:
            chargepoint_state = self.old_chargepoint_state

            evse_state, counter_state = self._client.request_and_check_hardware(self.fault_state)
            power = counter_state.power
            if counter_state.power < self.PLUG_STANDBY_POWER_THRESHOLD:
                power = 0
            phases_in_use = sum(1 for current in counter_state.currents if current > 3)
            if phases_in_use == 0:
                phases_in_use = self.old_phases_in_use
            else:
                self.old_phases_in_use = phases_in_use

            time.sleep(0.1)
            self.client_error_context.reset_error_counter()

            if phase_switch_cp_active:
                # Während des Threads wird die CP-Leitung unterbrochen, das EV soll aber als angesteckt betrachtet
                # werden. In 1.9 war das kein Problem, da währenddessen keine Werte von der EVSE abgefragt wurden.
                log.debug(
                    "Plug_state %s beibehalten, da CP-Unterbrechung oder Phasenumschaltung aktiv.", self.old_plug_state
                )
                plug_state = self.old_plug_state
            else:
                self.old_plug_state = evse_state.plug_state
                plug_state = evse_state.plug_state

            chargepoint_state = ChargepointState(
                power=power,
                currents=counter_state.currents,
                imported=counter_state.imported,
                exported=0,
                powers=counter_state.powers,
                voltages=counter_state.voltages,
                frequency=counter_state.frequency,
                plug_state=plug_state,
                charge_state=evse_state.charge_state,
                phases_in_use=phases_in_use,
                power_factors=counter_state.power_factors,
                rfid=last_tag,
                evse_current=evse_state.set_current,
                serial_number=counter_state.serial_number,
                max_evse_current=evse_state.max_current,
                version=self.version,
                current_branch=self.current_branch,
                current_commit=self.current_commit
            )
            if phases_in_use == 1:
                measured_current = counter_state.currents[0]
            elif phases_in_use == 2:
                measured_current = (counter_state.currents[0] + counter_state.currents[1]) / 2
            elif phases_in_use == 3:
                measured_current = sum(counter_state.currents) / 3
            else:
                measured_current = 0
            try:
                soc = SubData.cp_data[f"cp{self.hierarchy_id}"].chargepoint.data.get.connected_vehicle.soc
            except (KeyError, AttributeError):
                soc = None
            evse_relay_log.info("CP%d: measured_current=%.1fA set_current=%.1fA phases=%d soc=%s",
                                self.local_charge_point_num, measured_current, evse_state.set_current,
                                phases_in_use, soc if soc is not None else "NA")
        if self.client_error_context.error_counter_exceeded():
            if not self._cp_safety_asserted:
                self._assert_cp_safety_stop()
            chargepoint_state = ChargepointState(plug_state=self.old_plug_state,
                                                 charge_state=False,
                                                 imported=self.old_chargepoint_state.imported,
                                                 exported=self.old_chargepoint_state.exported,
                                                 currents=[0]*3,
                                                 phases_in_use=self.old_chargepoint_state.phases_in_use,
                                                 power=0)
        elif self._cp_safety_asserted:
            # Communication has recovered — release the CP safety stop
            self._release_cp_safety_stop()

        store_state(chargepoint_state)
        self.old_chargepoint_state = chargepoint_state
        return chargepoint_state

    def perform_phase_switch(self, phases_to_use: int) -> None:
        gpio_cp, gpio_relay = self._client.get_pins_phase_switch(phases_to_use)
        evse = self._client.evse_client
        cp = self.local_charge_point_num
        with SingleComponentUpdateContext(self.fault_state, update_always=False, reraise=True):
            evse.set_current(0, force=True)  # stop charging before switching phases (bypass debounce)
            for _ in range(20):  # poll up to 10s (20 × 0.5s) for EVSE to confirm 0 A
                if _read_evse_current_from_hardware(evse) == 0:
                    break
                time.sleep(0.5)
                evse.set_current(0, force=True)  # send stop command again
            else:
                raise Exception("Ladung konnte nicht gestoppt werden - Phasenumschaltung abgebrochen.")
        safe_relay_output(gpio_cp, GPIO.HIGH, evse, cp_num=cp)  # CP off
        safe_relay_output(gpio_relay, GPIO.HIGH, evse, cp_num=cp)  # 3 on/off  turn on set or reset input of toggle relay
        time.sleep(0.5)
        safe_relay_output(gpio_relay, GPIO.LOW, evse, cp_num=cp)  # 3 turn off set/reset input of toggle relay
        #time.sleep(0.5)
        safe_relay_output(gpio_cp, GPIO.LOW, evse, cp_num=cp)  # CP on
        #time.sleep(1)
        self.old_phases_in_use = phases_to_use

    def perform_cp_interruption(self, duration: int) -> None:
        gpio_cp = self._client.get_pins_cp_interruption()
        evse = self._client.evse_client
        cp = self.local_charge_point_num
        with SingleComponentUpdateContext(self.fault_state, update_always=False):
            evse.set_current(0, force=True)  # CP interruption: bypass debounce
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(gpio_cp, GPIO.OUT)

        safe_relay_output(gpio_cp, GPIO.HIGH, evse, cp_num=cp, check_evse=False)
        time.sleep(duration)
        safe_relay_output(gpio_cp, GPIO.LOW, evse, cp_num=cp, check_evse=False)
