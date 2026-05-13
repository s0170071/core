"""Hausspeicher-Logik
Der Hausspeicher ist immer bestrebt, den EVU-Überschuss auf 0 zu regeln.
Wenn EVU_Überschuss vorhanden ist, lädt der Speicher. Wenn EVU-Bezug vorhanden wäre,
entlädt der Speicher, sodass kein Netzbezug stattfindet. Wenn das EV Vorrang hat, wird
eine Ladung gestartet und der Speicher hört automatisch auf zu laden, da sonst durch
das Laden des EV Bezug statt finden würde.

Sonderfall Hybrid-Systeme:
Wenn wir ein Hybrid Wechselrichter Speicher system haben das besteht aus:
20 kW PV
15kW Wechselrichter
Batterie DC
Kann es derzeit passieren das die PV 20kW erzeugt, die Batterie mit 5kW geladen wird und 15kW ins Netz gehen.
Zieht die openWB nun Überschuss (15kW Überschuss + 5kW Batterieladung = 20kW) kommt es zu 5kW Bezug weil der
Wechselrichter nur 15kW abgeben kann.

__Wie schnell regelt ein Speicher?
Je nach Speicher 1-4 Sekunden.
"""
from dataclasses import dataclass, field
from enum import Enum
import logging
from typing import List, Optional

from control.algorithm.chargemodes import CONSIDERED_CHARGE_MODES_CHARGING
from control.algorithm.filter_chargepoints import get_chargepoints_with_required_current_by_chargemode
from control.pv import Pv
from helpermodules.constants import NO_ERROR
from modules.common.abstract_device import AbstractDevice

log = logging.getLogger(__name__)


class BatConsiderationMode(Enum):
    BAT_MODE = "bat_mode"
    EV_MODE = "ev_mode"
    MIN_SOC_BAT = "min_soc_bat_mode"


class BatPowerLimitMode(Enum):
    NO_LIMIT = "no_limit"
    LIMIT_STOP = "limit_stop"
    LIMIT_TO_HOME_CONSUMPTION = "limit_to_home_consumption"


@dataclass
class Config:
    configured: bool = field(default=False, metadata={"topic": "config/configured"})
    power_limit_mode: str = field(default=BatPowerLimitMode.NO_LIMIT.value,
                                  metadata={"topic": "config/power_limit_mode"})
    bat_control_permitted: bool = field(default=False, metadata={"topic": "config/bat_control_permitted"})


def config_factory() -> Config:
    return Config()


@dataclass
class Get:
    power_limit_controllable: bool = field(default=False, metadata={"topic": "get/power_limit_controllable"})
    soc: float = field(default=0, metadata={"topic": "get/soc"})
    daily_exported: float = field(default=0, metadata={"topic": "get/daily_exported"})
    daily_imported: float = field(default=0, metadata={"topic": "get/daily_imported"})
    fault_str: str = field(default=NO_ERROR, metadata={"topic": "get/fault_str"})
    fault_state: int = field(default=0, metadata={"topic": "get/fault_state"})
    imported: float = field(default=0, metadata={"topic": "get/imported"})
    exported: float = field(default=0, metadata={"topic": "get/exported"})
    power: float = field(default=0, metadata={"topic": "get/power"})


def get_factory() -> Get:
    return Get()


@dataclass
class Set:
    charging_power_left: float = field(default=0, metadata={"topic": "set/charging_power_left"})
    power_limit: Optional[float] = field(default=None, metadata={"topic": "set/power_limit"})
    regulate_up: bool = field(default=False, metadata={"topic": "set/regulate_up"})
    protect_active: bool = field(default=False, metadata={"topic": "set/protect_active"})
    assist_active: bool = field(default=False, metadata={"topic": "set/assist_active"})


def set_factory() -> Set:
    return Set()


@dataclass
class BatAllData:
    config: Config = field(default_factory=config_factory)
    get: Get = field(default_factory=get_factory)
    set: Set = field(default_factory=set_factory)


class BatAll:
    ERROR_CONFIG_MAX_AC_OUT = ("Maximale Entladeleistung des Wechselrichters  muss bei einem Hybrid-System " +
                               "konfiguriert werden. Bitte im Lastmanagement die maximale Ausgangsleistung des"
                               + " Wechselrichters angeben.")

    def __init__(self):
        self.data = BatAllData()

    def calc_power_for_all_components(self):
        try:
            if len(data.data.bat_data) >= 1:
                self.data.config.configured = True
                # Summe für alle konfigurierten Speicher bilden
                exported = 0
                imported = 0
                power = 0
                soc_sum = 0
                soc_count = 0
                fault_state = 0
                for battery in data.data.bat_data.values():
                    try:
                        if battery.data.get.fault_state < 2:
                            try:
                                power += battery.data.get.power
                            except Exception:
                                log.exception(f"Fehler im Bat-Modul {battery.num}")
                            imported += battery.data.get.imported
                            exported += battery.data.get.exported
                            soc_sum += battery.data.get.soc
                            soc_count += 1
                        else:
                            if fault_state < battery.data.get.fault_state:
                                fault_state = battery.data.get.fault_state
                    except Exception:
                        log.exception(f"Fehler im Bat-Modul {battery.num}")
                if fault_state == 0:
                    self.data.get.imported = imported
                    self.data.get.exported = exported
                    self.data.get.fault_state = 0
                    self.data.get.fault_str = NO_ERROR
                else:
                    self.data.get.fault_state = fault_state
                    self.data.get.fault_str = ("Bitte die Statusmeldungen der Speicher prüfen. Es konnte kein "
                                               "aktueller Zählerstand ermittelt werden, da nicht alle Module Werte "
                                               "liefern.")
                self.data.get.power = power
                try:
                    self.data.get.soc = int(soc_sum / soc_count)
                except ZeroDivisionError:
                    self.data.get.soc = 0
            else:
                self.data.config.configured = False
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def _inverter_limited_power(self, inverter: Pv) -> float:
        """gibt die maximale Entladeleistung des Speichers zurück, bis die maximale Ausgangsleistung des WR erreicht
        ist."""
        # tested
        # Wenn vom PV-Ertrag der Speicher geladen wird, kann diese Leistung bis zur max Ausgangsleistung des WR
        # genutzt werden.
        if inverter.data.config.max_ac_out > 0:
            return max(inverter.data.get.power * -1 - inverter.data.config.max_ac_out, 0)
        else:
            return 0

    def _limit_bat_power_discharge(self, required_power):
        """begrenzt die für den Algorithmus benötigte Entladeleistung des Speichers, wenn die maximale Ausgangsleistung
        des WR erreicht ist."""
        inverter_limited_power = 0
        if required_power > 0:
            # Nur wenn der Speicher entladen werden soll, fließt Leistung durch den WR.
            for inverter in data.data.pv_data.values():
                try:
                    inverter_limited_power += self._inverter_limited_power(inverter)
                except Exception:
                    log.exception(f"Fehler im Bat-Modul {inverter.num}")
            if inverter_limited_power > 0:
                required_power = max(required_power-inverter_limited_power, 0)
                log.debug(f"Verbleibende Speicher-Leistung durch maximale Ausgangsleistung auf {required_power}W"
                          " begrenzt.")
        return required_power

    def setup_bat(self):
        """ prüft, ob mind ein Speicher vorhanden ist und berechnet die Summen-Topics.
        """
        try:
            if self.data.config.configured is True:
                if self.data.get.fault_state == 0:
                    self.set_power_limit_controllable()
                    self.get_power_limit()
                    self._get_charging_power_left()
                    log.info(f"{self.data.set.charging_power_left}W verbleibende Speicher-Leistung")
                else:
                    # Bei Warnung oder Fehlerfall, zB durch Kalibrierung, Speicher-Leistung nicht in der
                    # Regelung berücksichtigen.
                    self.data.set.charging_power_left = 0
            else:
                self.data.set.charging_power_left = 0
                self.data.get.power = 0
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def _get_charging_power_left(self):
        """ ermittelt die Lade-Leistung des Speichers, die zum Laden der EV verwendet werden darf.
        """
        try:
            config = data.data.general_data.data.chargemode_config.pv_charging

            self.data.set.regulate_up = False
            if config.bat_mode == BatConsiderationMode.BAT_MODE.value:
                if self.data.get.power < 0:
                    charging_power_left = self.data.get.power
                else:
                    charging_power_left = 0
                # Treat the battery as "full" once it reaches the configured max SoC,
                # not at a hardcoded 100%. Otherwise regulate_up stays True forever
                # (chargers see -100W reserve + forced switch-off when raw_surplus<=0)
                # and PV charging will not start even though the battery is full per
                # the user's configuration.
                self.data.set.regulate_up = (
                    True if self.data.get.soc < config.max_bat_soc else False)
            elif config.bat_mode == BatConsiderationMode.EV_MODE.value:
                charging_power_left = self.data.get.power
            else:
                # MIN_SOC_BAT mode — PROTECT / PRIORITY / ASSIST state machine
                min_soc = config.min_bat_soc
                max_soc = config.max_bat_soc
                discharge_rate = config.bat_power_discharge if config.bat_power_discharge_active else 0
                power = self.data.get.power
                soc = self.data.get.soc

                # Hysteresis latch: PROTECT until max reached
                if soc <= min_soc:
                    self.data.set.protect_active = True
                    self.data.set.assist_active = False
                if self.data.set.protect_active and soc >= max_soc:
                    self.data.set.protect_active = False

                if self.data.set.protect_active:
                    # STATE: PROTECT — battery priority, but EV may use genuine grid export.
                    # regulate_up is NOT set: the battery is already absorbing all it can (export proves
                    # its charge rate is saturated), so we don't need to artificially starve the EV.
                    # The normal control_range_offset is preserved so switch_on_threshold is reachable.
                    charging_power_left = 0
                    log.debug(f"MIN_SOC_BAT PROTECT: soc={soc}%, cpl={charging_power_left}W (export still usable)")

                elif self.data.set.assist_active:
                    # STATE: ASSIST (latched) — battery helps car
                    if not self._any_ev_charging():
                        # Car stopped → exit ASSIST → PRIORITY
                        self.data.set.assist_active = False
                        if power < 0:
                            charging_power_left = power
                            self.data.set.regulate_up = True
                        else:
                            charging_power_left = 0
                        log.debug(f"MIN_SOC_BAT ASSIST→PRIORITY: car stopped, cpl={charging_power_left}W")
                    else:
                        charging_power_left = discharge_rate + min(0, power)
                        log.debug(f"MIN_SOC_BAT ASSIST: discharge_rate={discharge_rate}W, "
                                  f"power={power}W, cpl={charging_power_left}W")

                elif self._ev_at_min_current():
                    # Entering ASSIST — car at min, soc > min
                    self.data.set.assist_active = True
                    charging_power_left = discharge_rate + min(0, power)
                    log.debug(f"MIN_SOC_BAT ASSIST (enter): discharge_rate={discharge_rate}W, "
                              f"power={power}W, cpl={charging_power_left}W")

                else:
                    # STATE: PRIORITY — battery recovers, car gets grid export only
                    if power < 0:
                        charging_power_left = power
                        self.data.set.regulate_up = True
                    elif soc >= max_soc and power > 0:
                        # OVERFLOW: battery is "full" per user config but still absorbing
                        # PV. Expose that PV as available surplus so the car can take it
                        # instead of letting the battery hoard energy above max_bat_soc.
                        charging_power_left = power
                        log.debug(f"MIN_SOC_BAT OVERFLOW: soc={soc}% >= max_soc={max_soc}%, "
                                  f"battery charging at {power}W exposed as surplus, cpl={charging_power_left}W")
                    else:
                        charging_power_left = 0
                    log.debug(f"MIN_SOC_BAT PRIORITY: power={power}W, cpl={charging_power_left}W, "
                              f"regulate_up={self.data.set.regulate_up}")

            if self.data.set.regulate_up:
                log.debug("Damit der Speicher hochregeln kann, muss unabhängig vom eingestellten Regelmodus "
                          "Einspeisung erzeugt werden.")
                charging_power_left -= 100
            self.data.set.charging_power_left = self._limit_bat_power_discharge(charging_power_left)
        except Exception:
            log.exception("Fehler im Bat-Modul")

    def _any_ev_charging(self) -> bool:
        """True if any chargepoint is actively charging."""
        for cp in data.data.cp_data.values():
            if cp.data.get.charge_state:
                return True
        return False

    def _ev_at_min_current(self) -> bool:
        """True when at least one EV is charging AND all charging EVs are at their min_current."""
        any_charging = False
        for cp in data.data.cp_data.values():
            if cp.data.get.charge_state:
                any_charging = True
                ev_template = cp.data.set.charging_ev_data.ev_template.data
                if cp.data.set.current > ev_template.min_current:
                    return False
        return any_charging

    def power_for_bat_charging(self):
        """ gibt die Leistung zurück, die zum Laden verwendet werden kann.

        Return
        ------
        int: Leistung, die zum Laden verwendet werden darf.
        """
        try:
            if self.data.config.configured:
                return self.data.set.charging_power_left
            else:
                return 0
        except Exception:
            log.exception("Fehler im Bat-Modul")
            return 0

    def set_power_limit_controllable(self):
        controllable_bat_components = get_controllable_bat_components()
        if len(controllable_bat_components) > 0:
            self.data.get.power_limit_controllable = True
            for bat in controllable_bat_components:
                data.data.bat_data[f"bat{bat.component_config.id}"].data.get.power_limit_controllable = True
        else:
            self.data.get.power_limit_controllable = False

    def get_power_limit(self):
        if self.data.config.bat_control_permitted is False:
            self.data.set.power_limit = None
        else:
            chargepoint_by_chargemodes = get_chargepoints_with_required_current_by_chargemode(
                CONSIDERED_CHARGE_MODES_CHARGING)
            # Falls aktive Steuerung an und Fahrzeuge laden und kein Überschuss im System ist,
            # dann Speicherleistung begrenzen.
            if (self.data.config.power_limit_mode != BatPowerLimitMode.NO_LIMIT.value and
                len(chargepoint_by_chargemodes) > 0 and
                    data.data.cp_all_data.data.get.power > 100 and
                    self.data.get.power_limit_controllable and
                    self.data.get.power <= 0 and
                    data.data.counter_all_data.get_evu_counter().data.get.power >= -100):
                if self.data.config.power_limit_mode == BatPowerLimitMode.LIMIT_STOP.value:
                    self.data.set.power_limit = 0
                elif self.data.config.power_limit_mode == BatPowerLimitMode.LIMIT_TO_HOME_CONSUMPTION.value:
                    self.data.set.power_limit = data.data.counter_all_data.data.set.home_consumption * -1
                log.debug(f"Speicher-Leistung begrenzen auf {self.data.set.power_limit/1000}kW")
            else:
                self.data.set.power_limit = None
                control_range_low = data.data.general_data.data.chargemode_config.pv_charging.control_range[0]
                control_range_high = data.data.general_data.data.chargemode_config.pv_charging.control_range[1]
                control_range_center = control_range_high - (control_range_high - control_range_low) / 2
                if len(chargepoint_by_chargemodes) == 0:
                    log.debug("Speicher-Leistung nicht begrenzen, "
                              "da keine Ladepunkte in einem Lademodus mit Netzbezug sind.")
                elif data.data.cp_all_data.data.get.power <= 100:
                    log.debug("Speicher-Leistung nicht begrenzen, da kein Ladepunkt mit Netzbezug lädt.")
                elif self.data.get.power_limit_controllable is False:
                    log.debug("Speicher-Leistung nicht begrenzen, da keine regelbaren Speicher vorhanden sind.")
                elif self.data.get.power > 0:
                    log.debug("Speicher-Leistung nicht begrenzen, da kein Speicher entladen wird.")
                elif data.data.counter_all_data.get_evu_counter().data.get.power < control_range_center + 80:
                    # Wenn der Regelbereich zB auf Bezug steht, darf auch die Leistung des Regelbereichs entladen
                    # werden.
                    log.debug("Speicher-Leistung nicht begrenzen, da EVU-Überschuss vorhanden ist.")
                else:
                    log.debug("Speicher-Leistung nicht begrenzen.")
        remaining_power_limit = self.data.set.power_limit
        for bat_component in get_controllable_bat_components():
            if self.data.set.power_limit is None:
                power_limit = None
            else:
                power_limit = self._limit_bat_power_discharge(remaining_power_limit)
                remaining_power_limit -= power_limit

            data.data.bat_data[f"bat{bat_component.component_config.id}"].data.set.power_limit = power_limit


def get_controllable_bat_components() -> List:
    bat_components = []
    for value in data.data.system_data.values():
        if isinstance(value, AbstractDevice):
            for comp_value in value.components.values():
                if "bat" in comp_value.component_config.type:
                    if comp_value.power_limit_controllable():
                        bat_components.append(comp_value)
    return bat_components


# Deferred import: bat_all and data have a circular dependency (data.py imports BatAll).
# By placing this import after BatAll is fully defined, the circular import resolves cleanly.
from control import data  # noqa: E402
