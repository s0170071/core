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


def make_chargepoint(state: ChargepointState = ChargepointState.CHARGING_ALLOWED,
                     current: Optional[float] = 0,
                     chargemode: Chargemode = Chargemode.PV_CHARGING,
                     submode: Chargemode = Chargemode.PV_CHARGING,
                     prevent_charge_stop: bool = False,
                     limiting_value: Optional[LimitingValue] = None) -> Mock:
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


# switch_off_decision()

def test_switch_off_decision_vetoes_while_buffering(monkeypatch):
    # setup
    monkeypatch.setattr(bat_buffer, "_buffering", True)
    data.data.bat_all_data.data.get.soc = 60
    cp = make_chargepoint()

    # execution / evaluation
    assert bat_buffer.switch_off_decision(cp) is True


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
