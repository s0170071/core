from dataclasses import dataclass
from unittest.mock import Mock
import pytest

from control import data
from control.chargepoint.chargepoint import Chargepoint
from control.chargepoint.chargepoint_state import ChargepointState
from control.chargepoint.chargepoint_template import CpTemplate
from control.ev.ev import Ev
from control.ev.ev_template import EvTemplate
from control.process import Process


@pytest.fixture()
def mock_data() -> None:
    data.data_init(Mock())


@dataclass
class UpdateStateParams:
    name: str
    set_current: float
    current_offset: float
    cp_max_current: int
    min_current: int
    phases: int
    chargepoint_state: ChargepointState
    expected_current: float


update_state_params = [
    UpdateStateParams(
        name="no offset - current unchanged",
        set_current=10, current_offset=0, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=10,
    ),
    UpdateStateParams(
        name="positive offset - current raised",
        set_current=10, current_offset=2, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=12,
    ),
    UpdateStateParams(
        name="negative offset - current lowered",
        set_current=10, current_offset=-2, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=8,
    ),
    UpdateStateParams(
        name="negative offset - floored at min_current",
        set_current=7, current_offset=-4, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=6,  # 7 - 4 = 3 < min_current=6
    ),
    UpdateStateParams(
        name="positive offset - clamped to CP max",
        set_current=30, current_offset=5, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=32,  # 30 + 5 = 35 > cp_max=32
    ),
    UpdateStateParams(
        name="current is 0 - offset never applied",
        set_current=0, current_offset=5, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.CHARGING_ALLOWED,
        expected_current=0,
    ),
    UpdateStateParams(
        name="PERFORMING_PHASE_SWITCH forces 0 - offset not applied",
        set_current=10, current_offset=3, cp_max_current=32,
        min_current=6, phases=3,
        chargepoint_state=ChargepointState.PERFORMING_PHASE_SWITCH,
        expected_current=0,
    ),
]


@pytest.mark.parametrize("params", update_state_params, ids=[p.name for p in update_state_params])
def test_update_state_current_offset(params: UpdateStateParams, mock_data, monkeypatch):
    # --- setup chargepoint ---
    cp = Chargepoint(0, None)
    cp.template = CpTemplate()
    cp.template.data.max_current_multi_phases = params.cp_max_current
    cp.template.data.max_current_single_phase = params.cp_max_current
    cp.data.set.current = params.set_current
    cp.data.control_parameter.phases = params.phases
    cp.data.control_parameter.state = params.chargepoint_state
    cp.data.control_parameter.min_current = params.min_current
    cp.data.get.plug_state = True

    # --- setup EV with offset ---
    ev = Ev(0)
    ev.ev_template = EvTemplate()
    ev.data.current_offset = params.current_offset
    ev.ev_template.data.prevent_phase_switch = False
    ev.ev_template.data.min_current = params.min_current
    ev.ev_template.data.max_current_multi_phases = params.cp_max_current
    ev.ev_template.data.max_current_single_phase = params.cp_max_current
    cp.data.set.charging_ev_data = ev

    # Mock the initial check_min_max_current safety re-clamp so it passes
    # the current through unchanged, isolating the offset logic under test.
    monkeypatch.setattr(
        Ev, "check_min_max_current",
        Mock(return_value=(params.set_current, None))
    )

    # --- run ---
    Process()._update_state(cp)

    # --- assert ---
    assert cp.data.set.current == params.expected_current
