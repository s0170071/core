from typing import List
from unittest.mock import Mock

import pytest

from custom import evse_transition_filter as filt


@pytest.fixture(autouse=True)
def reset_state() -> None:
    # Der Zustand lebt auf Modul-Ebene und muss zwischen den Tests weg.
    filt.reset()


@pytest.fixture
def clock(monkeypatch) -> List[float]:
    """Ersetzt time.monotonic durch eine steuerbare Uhr. clock[0] ist "jetzt"."""
    now = [1000.0]
    monkeypatch.setattr(filt.time, "monotonic", lambda: now[0])
    return now


def write(evse_id: int, current: int, force: bool = False) -> bool:
    """Bildet die Aufrufsequenz in evse.set_current nach.

    :return: True, wenn der Schreibzugriff tatsächlich rausgegangen wäre.
    """
    if not filt.allow_write(evse_id, current, force):
        return False
    filt.record_write(evse_id, current, force)
    return True


# Eigenschaft 1: nur 0 <-> ungleich 0 wird begrenzt


def test_erster_schreibzugriff_geht_immer_durch(clock):
    assert write(1, 6) is True


def test_erster_schreibzugriff_darf_auch_null_sein(clock):
    assert write(1, 0) is True


def test_stromaenderung_wird_nicht_begrenzt(clock):
    assert write(1, 6) is True
    clock[0] += 1
    assert write(1, 10) is True
    clock[0] += 1
    assert write(1, 16) is True
    clock[0] += 1
    assert write(1, 6) is True


def test_wiederholte_null_wird_nicht_begrenzt(clock):
    assert write(1, 0) is True
    clock[0] += 1
    assert write(1, 0) is True


# Mindest-Ein-Zeit


def test_abschaltung_vor_ablauf_der_mindest_ein_zeit_wird_unterdrueckt(clock):
    assert write(1, 6) is True
    clock[0] += filt.MIN_ON_TIME_S - 1
    assert write(1, 0) is False


def test_abschaltung_nach_ablauf_der_mindest_ein_zeit_geht_durch(clock):
    assert write(1, 6) is True
    clock[0] += filt.MIN_ON_TIME_S
    assert write(1, 0) is True


def test_mindest_ein_zeit_laeuft_ab_dem_letzten_ungleich_null_wert(clock):
    assert write(1, 6) is True
    clock[0] += filt.MIN_ON_TIME_S - 10
    # Eine Stromänderung setzt das Fenster neu auf, denn sie schreibt einen
    # neuen ungleich-0-Wert.
    assert write(1, 10) is True
    clock[0] += 20
    assert write(1, 0) is False


# Mindest-Aus-Zeit


def test_einschaltung_vor_ablauf_der_mindest_aus_zeit_wird_unterdrueckt(clock):
    assert write(1, 0) is True
    clock[0] += filt.MIN_OFF_TIME_S - 1
    assert write(1, 6) is False


def test_einschaltung_nach_ablauf_der_mindest_aus_zeit_geht_durch(clock):
    assert write(1, 0) is True
    clock[0] += filt.MIN_OFF_TIME_S
    assert write(1, 6) is True


# Eigenschaft 2: der Filter verzögert, er verwirft nicht


def test_dauerhafter_wunsch_landet_nach_ablauf_des_fensters(clock):
    assert write(1, 6) is True
    # Die Regelung stellt den Abschaltwunsch in jedem Zyklus erneut.
    for _ in range(10):
        clock[0] += 10
        assert write(1, 0) is False
    clock[0] += filt.MIN_ON_TIME_S
    assert write(1, 0) is True


def test_ein_zyklus_zappler_wird_geschluckt(clock):
    """0 unterdrückt -> der Zustand bleibt "ein" -> das folgende 6A ist frei."""
    assert write(1, 6) is True
    clock[0] += 5
    assert write(1, 0) is False
    clock[0] += 5
    # Nicht durch die Mindest-Aus-Zeit blockiert, denn es gab keine Abschaltung.
    assert write(1, 6) is True


def test_unterdrueckter_schreibzugriff_bewaffnet_die_timer_nicht(clock):
    assert write(1, 6) is True
    clock[0] += 10
    assert write(1, 0) is False
    clock[0] += filt.MIN_ON_TIME_S - 10
    # Insgesamt MIN_ON_TIME_S seit dem 6A, der Versuch dazwischen ändert nichts.
    assert write(1, 0) is True


# Eigenschaft 3: force umgeht den Filter und bewaffnet die Timer nicht


def test_force_umgeht_die_mindest_ein_zeit(clock):
    assert write(1, 6) is True
    clock[0] += 1
    assert write(1, 0, force=True) is True


def test_force_bewaffnet_die_mindest_aus_zeit_nicht(clock):
    """Sonst bliebe die Ladung nach einer Phasenumschaltung 5 Minuten aus."""
    assert write(1, 6) is True
    clock[0] += 1
    assert write(1, 0, force=True) is True
    clock[0] += 6
    assert write(1, 6) is True


def test_force_bewaffnet_die_mindest_ein_zeit_nicht(clock):
    assert write(1, 0) is True
    clock[0] += filt.MIN_OFF_TIME_S
    assert write(1, 6, force=True) is True
    clock[0] += 1
    # Der erzwungene Einschaltvorgang hat den Zustand nicht auf "ein" gesetzt,
    # der Filter sieht weiterhin die letzte reguläre 0.
    assert write(1, 0) is True


# Trennung nach evse_id


def test_zustand_wird_je_evse_getrennt_gefuehrt(clock):
    assert write(1, 6) is True
    assert write(2, 6) is True
    clock[0] += 1
    assert write(1, 0) is False
    clock[0] += filt.MIN_ON_TIME_S
    assert write(2, 0) is True


# Diagnose


def warnings(mock_log) -> str:
    return " ".join(str(call.args[0]) for call in mock_log.warning.call_args_list)


def test_relay_loop_warnung_ab_drei_wechseln(clock, monkeypatch):
    mock_log = Mock()
    monkeypatch.setattr(filt, "log", mock_log)
    # record_write direkt aufrufen: allow_write würde die schnellen Wechsel
    # unterdrücken, gezählt werden aber nur tatsächlich erfolgte Schreibzugriffe.
    for current in (6, 0, 6, 0):
        filt.record_write(1, current)
        clock[0] += 1
    assert "possible relay loop" in warnings(mock_log)


def test_keine_relay_loop_warnung_ausserhalb_des_fensters(clock, monkeypatch):
    mock_log = Mock()
    monkeypatch.setattr(filt, "log", mock_log)
    for current in (6, 0, 6, 0):
        filt.record_write(1, current)
        clock[0] += filt.TOGGLE_WINDOW_S + 1
    assert "possible relay loop" not in warnings(mock_log)


# Fail-open


def test_fehler_im_filter_laesst_den_schreibzugriff_durch(monkeypatch, clock):
    assert write(1, 6) is True
    clock[0] += 1
    monkeypatch.setattr(filt, "_state", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(filt, "log", Mock())
    assert filt.allow_write(1, 0) is True


def test_fehler_beim_protokollieren_wird_nicht_durchgereicht(monkeypatch, clock):
    monkeypatch.setattr(filt, "_state", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(filt, "log", Mock())
    filt.record_write(1, 0)
