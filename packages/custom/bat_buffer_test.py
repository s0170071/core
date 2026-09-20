from datetime import datetime, timezone
from typing import Optional
from unittest.mock import Mock

import pytest

from control import data
from control.algorithm import filter_chargepoints
from control.bat_all import BatAll, BatConsiderationMode
from control.chargemode import Chargemode
from control.chargepoint.chargepoint_state import ChargepointState
from control.general import General
from control.limiting_value import LimitingValue, LoadmanagementLimit
from custom import bat_buffer

# Die autouse-Fixture ersetzt sun_is_high_enough; die Tests der Funktion selbst
# brauchen das Original.
_REAL_SUN_IS_HIGH_ENOUGH = bat_buffer.sun_is_high_enough


@pytest.fixture(autouse=True)
def bat_buffer_fixture(monkeypatch) -> None:
    data.data_init(Mock())
    data.data.general_data = General()
    data.data.bat_all_data = BatAll()
    data.data.bat_all_data.data.config.configured = True
    data.data.bat_all_data.data.get.soc = 80
    data.data.bat_data = {"bat2": Mock()}
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = BatConsiderationMode.MIN_SOC_BAT.value
    # Der Latch lebt auf Modul-Ebene und muss zwischen den Tests zurückgesetzt werden.
    monkeypatch.setattr(bat_buffer, "_buffering", False)
    # Sonst haengen alle Tests an der Uhrzeit, zu der sie laufen.
    monkeypatch.setattr(bat_buffer, "sun_is_high_enough", lambda: True)


def make_chargepoint(state: ChargepointState = ChargepointState.CHARGING_ALLOWED,
                     current: Optional[float] = 0,
                     chargemode: Chargemode = Chargemode.PV_CHARGING,
                     submode: Chargemode = Chargemode.PV_CHARGING,
                     prevent_charge_stop: bool = False,
                     limiting_value: Optional[LimitingValue] = None,
                     current_prev: float = 6,
                     charge_state: bool = True) -> Mock:
    cp = Mock()
    cp.num = 1
    control_parameter = cp.data.control_parameter
    control_parameter.chargemode = chargemode
    control_parameter.submode = submode
    control_parameter.state = state
    control_parameter.min_current = 6
    control_parameter.phases = 1
    control_parameter.limit = LoadmanagementLimit(None, limiting_value)
    control_parameter.timestamp_switch_on_off = 1652683252.0
    cp.data.set.current = current
    cp.data.set.current_prev = current_prev
    cp.data.get.charge_state = charge_state
    cp.data.set.required_power = 1380
    cp.data.set.charging_ev_data.ev_template.data.prevent_charge_stop = prevent_charge_stop
    return cp


# active() / buffering()

@pytest.mark.parametrize("latch, soc, expected",
                         [pytest.param(False, 80, True, id="above max_bat_soc starts buffering"),
                          pytest.param(False, 70, False, id="exactly max_bat_soc does not start (strict >)"),
                          pytest.param(False, 60, False, id="inside band stays off"),
                          pytest.param(False, 40, False, id="below min_bat_soc stays off"),
                          pytest.param(True, 80, True, id="above max stays on"),
                          pytest.param(True, 60, True, id="inside band holds"),
                          pytest.param(True, 50, True, id="exactly min_bat_soc holds (strict <)"),
                          pytest.param(True, 40, False, id="below min_bat_soc releases")])
def test_buffering_hysteresis(latch: bool, soc: int, expected: bool, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", latch)
    data.data.bat_all_data.data.get.soc = soc

    # execution
    result = bat_buffer.buffering()

    # evaluation
    assert result == expected


@pytest.mark.parametrize("bat_mode, expected",
                         [pytest.param(BatConsiderationMode.MIN_SOC_BAT.value, True, id="min_soc_bat_mode"),
                          pytest.param(BatConsiderationMode.BAT_MODE.value, False, id="bat_mode"),
                          pytest.param(BatConsiderationMode.EV_MODE.value, False, id="ev_mode")])
def test_active_only_in_min_soc_bat_mode(bat_mode: str, expected: bool):
    # setup
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = bat_mode

    # execution / evaluation
    assert bat_buffer.active() == expected


@pytest.mark.parametrize("configured, bats, expected",
                         [pytest.param(True, {"bat2": Mock()}, True, id="battery present"),
                          pytest.param(False, {"bat2": Mock()}, False, id="not configured"),
                          pytest.param(True, {}, False, id="no battery")])
def test_active_requires_battery(configured: bool, bats: dict, expected: bool):
    # setup
    data.data.bat_all_data.data.config.configured = configured
    data.data.bat_data = bats

    # execution / evaluation
    assert bat_buffer.active() == expected


def test_buffering_resets_latch_when_inactive(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = BatConsiderationMode.EV_MODE.value

    # execution / evaluation
    assert bat_buffer.buffering() is False


# block_switch_on()

def test_block_switch_on_passes_while_buffering(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    counter = Mock()
    cp = make_chargepoint(state=ChargepointState.NO_CHARGING_ALLOWED)

    # execution / evaluation
    assert bat_buffer.block_switch_on(counter, cp) is False


@pytest.mark.parametrize("latch, soc, expected",
                         [pytest.param(False, 80, False, id="above max_bat_soc may start"),
                          pytest.param(True, 80, False, id="above max_bat_soc may start while buffering"),
                          pytest.param(True, 60, True, id="inside band blocked even though latch is on"),
                          pytest.param(False, 60, True, id="inside band blocked with latch off"),
                          pytest.param(True, 40, True, id="below min_bat_soc blocked")])
def test_block_switch_on_start_gate(latch: bool, soc: int, expected: bool, monkeypatch):
    """Eine laufende Ladung wird im Band gehalten, eine neue darf dort nicht starten."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", latch)
    data.data.bat_all_data.data.get.soc = soc
    counter = Mock()
    counter.data.set.reserved_surplus = 0
    cp = make_chargepoint(state=ChargepointState.NO_CHARGING_ALLOWED)

    # execution / evaluation
    assert bat_buffer.block_switch_on(counter, cp) == expected


def test_block_switch_on_blocks_below_max_soc():
    # setup
    data.data.bat_all_data.data.get.soc = 60
    counter = Mock()
    cp = make_chargepoint(state=ChargepointState.NO_CHARGING_ALLOWED)

    # execution
    result = bat_buffer.block_switch_on(counter, cp)

    # evaluation
    assert result is True
    assert cp.data.control_parameter.state == ChargepointState.NO_CHARGING_ALLOWED


def test_block_switch_on_refunds_reserved_surplus():
    """Eine laufende Einschaltverzögerung muss die reservierte Leistung zurückgeben."""
    # setup
    data.data.bat_all_data.data.get.soc = 60
    counter = Mock()
    counter.data.set.reserved_surplus = 1500
    cp = make_chargepoint(state=ChargepointState.SWITCH_ON_DELAY)

    # execution
    result = bat_buffer.block_switch_on(counter, cp)

    # evaluation
    assert result is True
    assert counter.data.set.reserved_surplus == 0
    assert cp.data.control_parameter.timestamp_switch_on_off is None


def test_block_switch_on_does_not_refund_without_delay():
    # setup
    data.data.bat_all_data.data.get.soc = 60
    counter = Mock()
    counter.data.set.reserved_surplus = 0
    cp = make_chargepoint(state=ChargepointState.NO_CHARGING_ALLOWED)

    # execution
    bat_buffer.block_switch_on(counter, cp)

    # evaluation
    assert counter.data.set.reserved_surplus == 0


def test_block_switch_on_ignores_other_chargemodes():
    # setup
    data.data.bat_all_data.data.get.soc = 60
    counter = Mock()
    cp = make_chargepoint(chargemode=Chargemode.SCHEDULED_CHARGING, state=ChargepointState.NO_CHARGING_ALLOWED)

    # execution / evaluation
    assert bat_buffer.block_switch_on(counter, cp) is False


# sun_elevation() / sun_is_high_enough()

@pytest.mark.parametrize("when, expected",
                         [pytest.param(datetime(2026, 6, 21, 11, 15, tzinfo=timezone.utc), 62.3,
                                       id="summer solstice, solar noon"),
                          pytest.param(datetime(2026, 12, 21, 11, 15, tzinfo=timezone.utc), 15.4,
                                       id="winter solstice, solar noon"),
                          pytest.param(datetime(2026, 9, 19, 17, 30, tzinfo=timezone.utc), -1.8,
                                       id="after sunset is negative")])
def test_sun_elevation(when: datetime, expected: float):
    # execution
    elevation = bat_buffer.sun_elevation(when)

    # evaluation: die Naeherung ist auf deutlich unter 1 Grad genau
    assert elevation == pytest.approx(expected, abs=1.0)


def test_sun_elevation_peaks_around_solar_noon():
    """Sanity-Check ohne Referenzwert: das Maximum liegt nahe dem wahren Mittag."""
    # setup
    day = [datetime(2026, 6, 21, h, 0, tzinfo=timezone.utc) for h in range(24)]

    # execution
    peak = max(day, key=bat_buffer.sun_elevation)

    # evaluation: Laengengrad 10.45 Grad Ost -> wahrer Mittag ca. 11:18 UTC
    assert peak.hour in (11, 12)


@pytest.mark.parametrize("elevation, expected",
                         [pytest.param(35.0, True, id="high sun buffers"),
                          pytest.param(20.0, True, id="exactly at the threshold still buffers"),
                          pytest.param(19.9, False, id="below the threshold does not"),
                          pytest.param(-5.0, False, id="night does not")])
def test_sun_is_high_enough(elevation: float, expected: bool, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "sun_is_high_enough", _REAL_SUN_IS_HIGH_ENOUGH)
    monkeypatch.setattr(bat_buffer, "sun_elevation", lambda when=None: elevation)

    # execution / evaluation
    assert bat_buffer.sun_is_high_enough() == expected


def test_sun_is_high_enough_fails_open(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "sun_is_high_enough", _REAL_SUN_IS_HIGH_ENOUGH)
    monkeypatch.setattr(bat_buffer, "sun_elevation", Mock(side_effect=ValueError))

    # execution / evaluation
    assert bat_buffer.sun_is_high_enough() is True


def test_buffering_releases_latch_when_sun_is_low(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    monkeypatch.setattr(bat_buffer, "sun_is_high_enough", lambda: False)
    data.data.bat_all_data.data.get.soc = 100

    # execution / evaluation
    assert bat_buffer.buffering() is False


def test_switch_off_decision_defers_to_upstream_when_sun_is_low(monkeypatch):
    """Bei tiefer Sonne zieht sich der Puffer zurueck, statt hart abzuschalten."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    monkeypatch.setattr(bat_buffer, "sun_is_high_enough", lambda: False)
    data.data.bat_all_data.data.get.soc = 100
    cp = make_chargepoint()

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


# discharge_allowance()
@pytest.mark.parametrize("discharge_active, hysteresis, power_left, expected",
                         [pytest.param(True, True, 1000, 1000, id="granted allowance is excluded"),
                          pytest.param(True, True, 400, 400, id="never more than actually granted"),
                          pytest.param(True, True, -200, 0, id="negative power left excludes nothing"),
                          pytest.param(True, False, 1000, 0, id="no hysteresis discharge, nothing excluded"),
                          pytest.param(False, True, 1000, 0, id="discharge inactive, nothing excluded")])
def test_discharge_allowance(discharge_active: bool, hysteresis: bool, power_left: float, expected: float):
    # setup
    pv_config = data.data.general_data.data.chargemode_config.pv_charging
    pv_config.bat_power_discharge_active = discharge_active
    pv_config.bat_power_discharge = 1000
    data.data.bat_all_data.data.set.hysteresis_discharge = hysteresis
    data.data.bat_all_data.data.set.charging_power_left = power_left

    # execution / evaluation
    assert bat_buffer.discharge_allowance() == expected


def test_discharge_allowance_inactive_feature(monkeypatch):
    # setup
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = BatConsiderationMode.BAT_MODE.value
    data.data.bat_all_data.data.set.hysteresis_discharge = True
    data.data.bat_all_data.data.set.charging_power_left = 1000

    # execution / evaluation
    assert bat_buffer.discharge_allowance() == 0.0


# clamp_charging_power_left()

@pytest.mark.parametrize("soc, bat_power, charging_power_left, expected",
                         [pytest.param(60, 3000, 4000, 1000,
                                       id="charging battery is not handed to the car"),
                          pytest.param(60, -2500, -1500, -1500,
                                       id="discharging battery passes through unchanged"),
                          pytest.param(60, 0, 1000, 1000, id="idle battery keeps the allowance"),
                          pytest.param(60, 3000, 200, 200, id="never raises above upstream"),
                          pytest.param(80, 3000, 4000, 4000,
                                       id="above max_bat_soc upstream decides"),
                          pytest.param(70, 3000, 4000, 1000, id="exactly max_bat_soc is still clamped")])
def test_clamp_charging_power_left(soc: int, bat_power: float, charging_power_left: float, expected: float,
                                   monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    pv_config = data.data.general_data.data.chargemode_config.pv_charging
    pv_config.bat_power_discharge_active = True
    pv_config.bat_power_discharge = 1000
    data.data.bat_all_data.data.get.soc = soc
    data.data.bat_all_data.data.get.power = bat_power

    # execution / evaluation
    assert bat_buffer.clamp_charging_power_left(charging_power_left) == expected


def test_clamp_charging_power_left_without_discharge_allowance(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    pv_config = data.data.general_data.data.chargemode_config.pv_charging
    pv_config.bat_power_discharge_active = False
    pv_config.bat_power_discharge = 1000
    data.data.bat_all_data.data.get.soc = 60
    data.data.bat_all_data.data.get.power = 3000

    # execution / evaluation
    assert bat_buffer.clamp_charging_power_left(3000) == 0


@pytest.mark.parametrize("latch, bat_mode",
                         [pytest.param(False, BatConsiderationMode.MIN_SOC_BAT.value, id="not buffering"),
                          pytest.param(True, BatConsiderationMode.BAT_MODE.value, id="feature inactive")])
def test_clamp_charging_power_left_defers_to_upstream(latch: bool, bat_mode: str, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", latch)
    pv_config = data.data.general_data.data.chargemode_config.pv_charging
    pv_config.bat_mode = bat_mode
    pv_config.bat_power_discharge_active = True
    pv_config.bat_power_discharge = 1000
    data.data.bat_all_data.data.get.soc = 60
    data.data.bat_all_data.data.get.power = 3000

    # execution / evaluation
    assert bat_buffer.clamp_charging_power_left(4000) == 4000


@pytest.mark.parametrize("cp_current", [pytest.param(6, id="at min_current"), pytest.param(16, id="well above")])
def test_clamp_charging_power_left_ignores_charging_current(cp_current: float, monkeypatch):
    """Die Entladefreigabe haengt nicht am Ladestrom -- den stellt die Regelung ein."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    pv_config = data.data.general_data.data.chargemode_config.pv_charging
    pv_config.bat_power_discharge_active = True
    pv_config.bat_power_discharge = 1000
    data.data.bat_all_data.data.get.soc = 60
    data.data.bat_all_data.data.get.power = -800
    data.data.cp_data = {"cp1": make_chargepoint(current=cp_current, current_prev=cp_current)}

    # execution / evaluation
    assert bat_buffer.clamp_charging_power_left(200) == 200


# switch_off_decision()

def test_switch_off_decision_vetoes_while_buffering(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint()

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is True


def test_switch_off_decision_defers_when_charge_not_flowing(monkeypatch):
    """Ein Veto haelt nur eine laufende Ladung, es startet keine."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current_prev=0, charge_state=False)

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


def test_switch_off_decision_defers_without_flow_even_above_max_bat_soc(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 80
    cp = make_chargepoint(current_prev=0, charge_state=False)

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


def test_switch_off_decision_stops_below_min_soc(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 40
    cp = make_chargepoint()

    # execution
    result = bat_buffer.switch_off_decision(cp)

    # evaluation
    assert result is False
    assert cp.data.control_parameter.state == ChargepointState.NO_CHARGING_ALLOWED


def test_switch_off_decision_releases_surplus_when_delay_running(monkeypatch):
    """Wird eine laufende Abschaltverzögerung übersprungen, muss released_surplus zurückgenommen werden."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 40
    evu_counter = Mock()
    evu_counter.data.set.released_surplus = 1380
    monkeypatch.setattr(data.data.counter_all_data, "get_evu_counter", Mock(return_value=evu_counter))
    cp = make_chargepoint(state=ChargepointState.SWITCH_OFF_DELAY)

    # execution
    result = bat_buffer.switch_off_decision(cp)

    # evaluation
    assert result is False
    assert evu_counter.data.set.released_surplus == 0
    assert cp.data.control_parameter.timestamp_switch_on_off is None


def test_switch_off_decision_honours_prevent_charge_stop(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 40
    cp = make_chargepoint(prevent_charge_stop=True)

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


@pytest.mark.parametrize("chargemode, submode",
                         [pytest.param(Chargemode.SCHEDULED_CHARGING, Chargemode.PV_CHARGING, id="scheduled via pv"),
                          pytest.param(Chargemode.ECO_CHARGING, Chargemode.PV_CHARGING, id="eco via pv"),
                          pytest.param(Chargemode.PV_CHARGING, Chargemode.INSTANT_CHARGING, id="pv via instant")])
def test_switch_off_decision_ignores_other_chargemodes(chargemode: Chargemode, submode: Chargemode, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    cp = make_chargepoint(chargemode=chargemode, submode=submode)

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


def test_switch_off_decision_inactive_defers_to_upstream(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.general_data.data.chargemode_config.pv_charging.bat_mode = BatConsiderationMode.EV_MODE.value
    cp = make_chargepoint()

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is None


# apply_min_current_floor()

@pytest.mark.parametrize("current, expected",
                         [pytest.param(0, 6, id="raises zero to min_current"),
                          pytest.param(None, 6, id="raises None to min_current"),
                          pytest.param(3, 6, id="raises below min_current"),
                          pytest.param(10, 10, id="never lowers an already higher current"),
                          pytest.param(6, 6, id="leaves min_current untouched")])
def test_apply_min_current_floor(current: Optional[float], expected: float, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=current)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == expected


def test_apply_min_current_floor_inactive_when_not_buffering(monkeypatch):
    # setup
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 0


def test_apply_min_current_floor_skips_non_charging_state(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0, state=ChargepointState.NO_CHARGING_ALLOWED)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 0


@pytest.mark.parametrize("state",
                         [pytest.param(ChargepointState.SWITCH_OFF_DELAY, id="switch off delay"),
                          pytest.param(ChargepointState.WAIT_FOR_USING_PHASES, id="after phase switch"),
                          pytest.param(ChargepointState.PHASE_SWITCH_AWAITED, id="phase switch awaited")])
def test_apply_min_current_floor_does_not_restart_below_max_bat_soc(state: ChargepointState, monkeypatch):
    """Der Boden haelt eine laufende Ladung, er startet keine: sonst wuerde may_start() umgangen."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0, state=state, current_prev=0, charge_state=False)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 0


def test_apply_min_current_floor_does_not_start_even_above_max_bat_soc(monkeypatch):
    """Oberhalb von max_bat_soc darf nur der Einschaltpfad starten, der den Ueberschuss prueft."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 80
    cp = make_chargepoint(current=0, state=ChargepointState.SWITCH_OFF_DELAY, current_prev=0, charge_state=False)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 0


def test_apply_min_current_floor_holds_charge_that_is_still_flowing(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0, state=ChargepointState.SWITCH_OFF_DELAY, current_prev=0, charge_state=True)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 6


@pytest.mark.parametrize("limiting_value, expected",
                         [pytest.param(None, 6, id="unlimited is floored"),
                          pytest.param(LimitingValue.POWER, 6, id="power limit is floored"),
                          pytest.param(LimitingValue.CURRENT, 0, id="hard current limit is respected"),
                          pytest.param(LimitingValue.UNBALANCED_LOAD, 0, id="unbalanced load is respected")])
def test_apply_min_current_floor_respects_physical_limits(limiting_value: Optional[LimitingValue],
                                                          expected: float,
                                                          monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0, limiting_value=limiting_value)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == expected


# suppress_3_to_1()

@pytest.mark.parametrize("latch, soc, chargemode, expected",
                         [pytest.param(True, 60, Chargemode.PV_CHARGING, True, id="suppressed while buffering"),
                          pytest.param(True, 40, Chargemode.PV_CHARGING, False, id="released below min_bat_soc"),
                          pytest.param(False, 60, Chargemode.PV_CHARGING, False, id="not buffering"),
                          pytest.param(True, 60, Chargemode.SCHEDULED_CHARGING, False, id="other chargemode")])
def test_suppress_3_to_1(latch: bool, soc: int, chargemode: Chargemode, expected: bool, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", latch)
    data.data.bat_all_data.data.get.soc = soc
    cp = make_chargepoint(chargemode=chargemode)

    # execution / evaluation
    assert bat_buffer.suppress_3_to_1(cp.data.control_parameter) == expected


# Wolkendurchgang: aus einem hoeheren Strom heraus darf nicht direkt abgeschaltet werden.

@pytest.mark.parametrize("current", [pytest.param(8, id="8A"), pytest.param(16, id="16A")])
def test_cloud_does_not_switch_off_from_elevated_current(current: float, monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=current, current_prev=current)

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is True


def test_cloud_keeps_elevated_current_until_regulation_lowers_it(monkeypatch):
    """Der Boden senkt nicht: der Strom laeuft ueber die Ueberschussregelung nach unten."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=8, current_prev=8)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 8


def test_cloud_catches_charge_that_regulation_zeroed(monkeypatch):
    """Faellt die Zuteilung unter min_current, faengt der Boden sie im selben Zyklus wieder auf."""
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint(current=0, current_prev=8)
    monkeypatch.setattr(filter_chargepoints, "get_chargepoints_by_chargemode", Mock(return_value=[cp]))

    # execution
    bat_buffer.apply_min_current_floor()

    # evaluation
    assert cp.data.set.current == 6
    assert bat_buffer.switch_off_decision(cp) is True
