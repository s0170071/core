import logging

from control import data
from control.algorithm import common
from control.algorithm.chargemodes import CONSIDERED_CHARGE_MODES_MIN_CURRENT, CONSIDERED_CHARGE_MODES_PV_ONLY
from control.chargepoint.chargepoint_state import ChargepointState, CHARGING_STATES
from control.loadmanagement import Loadmanagement
from control.algorithm.filter_chargepoints import get_chargepoints_by_mode_and_counter

log = logging.getLogger(__name__)


class MinCurrent:

    def __init__(self) -> None:
        pass

    def _assist_floor_active(self, cp) -> bool:
        """True when ASSIST should pin the EV at min_current (hard floor).

        Conditions:
          - MIN_SOC_BAT ASSIST state is latched, AND
          - the EV was previously charging (state in CHARGING_STATES and current_prev > 0)
        In this case the battery's ASSIST budget covers any momentary grid import, so
        the loadmanagement stage must not drop the EV to 0 — that would trigger an
        immediate SWITCH_OFF_NOT_CHARGING lockout and a multi-minute outage.
        """
        try:
            if not data.data.bat_all_data.data.set.assist_active:
                return False
            if cp.data.control_parameter.state not in CHARGING_STATES:
                return False
            if (cp.data.set.current_prev or 0) <= 0:
                return False
            return True
        except Exception:
            return False

    def set_min_current(self) -> None:
        for mode_tuple, counter in common.mode_and_counter_generator(CONSIDERED_CHARGE_MODES_MIN_CURRENT):
            preferenced_chargepoints = get_chargepoints_by_mode_and_counter(mode_tuple, f"counter{counter.num}")
            if preferenced_chargepoints:
                log.info(f"Mode-Tuple {mode_tuple[0]} - {mode_tuple[1]} - {mode_tuple[2]}, Zähler {counter.num}")
                common.update_raw_data(preferenced_chargepoints, diff_to_zero=True)
                while len(preferenced_chargepoints):
                    cp = preferenced_chargepoints[0]
                    missing_currents, counts = common.get_min_current(cp)
                    if max(missing_currents) > 0:
                        available_currents, limit = Loadmanagement().get_available_currents(
                            missing_currents, counter, cp)
                        cp.data.control_parameter.limit = limit
                        available_for_cp = common.available_current_for_cp(
                            cp, counts, available_currents, missing_currents)
                        current = common.get_current_to_set(
                            cp.data.set.current, available_for_cp, cp.data.set.target_current)
                        if current < cp.data.control_parameter.min_current:
                            if self._assist_floor_active(cp):
                                # ASSIST hard floor: allocate min_current anyway, the battery covers any
                                # transient grid import. Without this floor a single tick where loadmanagement
                                # (or the negative cpl from ASSIST itself) shrinks the allocation below
                                # min_current would set 0A, the car would report charge_state=False, and
                                # PRIORITY's switch_off_check_threshold would immediately lock the LP into
                                # NO_CHARGING_ALLOWED for the full switch-on delay window.
                                cp.set_state_and_log(
                                    "ASSIST aktiv: Mindeststrom wird trotz Lastmanagement-Grenze gehalten, "
                                    "der Speicher deckt die Differenz.")
                                common.set_current_counterdiff(
                                    cp.data.set.target_current,
                                    cp.data.control_parameter.min_current,
                                    cp)
                            else:
                                common.set_current_counterdiff(-(cp.data.set.current or 0), 0, cp)
                                if limit:
                                    cp.set_state_and_log(
                                        f"Ladung kann nicht gestartet werden{limit.message}")
                        else:
                            common.set_current_counterdiff(
                                cp.data.set.target_current,
                                cp.data.control_parameter.min_current,
                                cp)
                    else:
                        if mode_tuple in CONSIDERED_CHARGE_MODES_PV_ONLY:
                            try:
                                if (cp.data.control_parameter.state == ChargepointState.NO_CHARGING_ALLOWED or
                                        cp.data.control_parameter.state == ChargepointState.SWITCH_ON_DELAY):
                                    data.data.counter_all_data.get_evu_counter().switch_on_threshold_reached(cp)
                            except Exception:
                                log.exception(f"Fehler in der PV-gesteuerten Ladung bei {cp.num}")
                        cp.data.set.current = 0
                    preferenced_chargepoints.pop(0)
