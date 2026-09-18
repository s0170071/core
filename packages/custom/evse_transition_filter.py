"""Begrenzt die Rate der Lade-/Stopp-Wechsel an der EVSE.

Manche Fahrzeuge gehen in einen Fehlerzustand, aus dem sie sich nicht mehr
selbst befreien, wenn die Ladefreigabe zu schnell hintereinander entzogen und
wieder erteilt wird. Dieser Filter erzwingt daher eine Mindest-Ein- und eine
Mindest-Aus-Zeit auf Register 1000.

Drei Eigenschaften, die gelten müssen:

1. **Nur der Wechsel 0 <-> ungleich 0 wird begrenzt.** Eine Änderung von 6A auf
   10A läuft immer durch, sonst wäre die Überschussregelung ausgehebelt.
2. **Der Filter verzögert, er verwirft nicht.** Bei ``False`` kehrt
   ``set_current`` vor ``write_register`` zurück, ``evse_current`` behält also
   weiterhin den zuletzt vom Gerät *gelesenen* Wert. Die Regelung stellt ihren
   Wunsch im nächsten Zyklus erneut, ein dauerhafter Befehl landet also, sobald
   das Fenster abgelaufen ist. Ein Ein-Zyklus-Zappler wird vollständig
   geschluckt: der 0-Schreibzugriff wird unterdrückt, der darauf folgende
   ungleich-0-Schreibzugriff ist wegen des unveränderten ``evse_current`` ein
   No-Op.
3. **``force=True`` umgeht den Filter und bewaffnet die Timer nicht.**
   Erzwungene Schreibzugriffe sind administrative Aktionen (Phasenumschaltung,
   CP-Unterbrechung), keine Regelungsentscheidungen. Würden sie die Timer
   setzen, bliebe die Ladung nach einer Phasenumschaltung 5 Minuten aus.

Der Zustand liegt bewusst auf Modul-Ebene und wird über ``evse_id`` getrennt
gehalten.
"""
import logging
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from threading import Lock
from typing import Dict, List, Optional
import time

from helpermodules.logger import FORMAT_STR_SHORT, PERSISTENT_LOG_PATH

# Aus dem alten Fork übernommen und im Feld bestätigt. Die Fenster sind
# absichtlich deutlich länger als switch_off_delay der Regelung; siehe
# Abschnitt 4.4 des Portierungsplans. Nicht "passend" kürzen.
MIN_ON_TIME_S = 300.0    # wie lange ein ungleich-0-Wert stehen muss, bevor 0 geschrieben werden darf
MIN_OFF_TIME_S = 300.0   # wie lange eine 0 stehen muss, bevor wieder ungleich 0 geschrieben werden darf

# Reine Diagnose, ohne Einfluss auf das Verhalten.
TOGGLE_WINDOW_S = 120.0
TOGGLE_WARN_COUNT = 3

LOG_FILE = "openwb_evse_relay.log"

log = logging.getLogger("evse_relay")


def _setup_logger() -> None:
    log.propagate = False
    try:
        handler = RotatingFileHandler(PERSISTENT_LOG_PATH + LOG_FILE, maxBytes=1000000, backupCount=1)
    except OSError:
        # Kein Log-Verzeichnis (z.B. Entwicklungsrechner): an den Root-Logger
        # durchreichen, statt den Import scheitern zu lassen.
        log.propagate = True
        return
    handler.setFormatter(logging.Formatter(FORMAT_STR_SHORT))
    log.addHandler(handler)


if not log.handlers:
    _setup_logger()


@dataclass
class _EvseState:
    last_zero_ts: float = 0.0
    last_nonzero_ts: float = 0.0
    # None: es wurde noch nichts geschrieben, es gibt also nichts zu begrenzen.
    last_was_zero: Optional[bool] = None
    transitions: List[float] = field(default_factory=list)


_states: Dict[int, _EvseState] = {}
# Der interne Ladepunkt läuft in einem eigenen Thread, daher ein Lock.
_lock = Lock()


def _state(evse_id: int) -> _EvseState:
    state = _states.get(evse_id)
    if state is None:
        state = _EvseState()
        _states[evse_id] = state
    return state


def allow_write(evse_id: int, formatted_current: int, force: bool = False) -> bool:
    """Darf dieser Schreibzugriff auf Register 1000 jetzt raus?

    :return: True -> schreiben. False -> diesen Zyklus überspringen, die
             Regelung stellt den Wunsch im nächsten Zyklus erneut.
    """
    if force:
        return True
    try:
        with _lock:
            state = _state(evse_id)
            is_zero = formatted_current == 0
            if state.last_was_zero is None or is_zero == state.last_was_zero:
                # Erster Schreibzugriff, oder reine Änderung der Stromstärke.
                return True
            now = time.monotonic()
            if is_zero:
                elapsed = now - state.last_nonzero_ts
                if elapsed < MIN_ON_TIME_S:
                    log.warning(f"EVSE id={evse_id}: zero-write suppressed "
                                f"({elapsed:.1f}s since last non-zero, min-on={MIN_ON_TIME_S:.0f}s)")
                    return False
            else:
                elapsed = now - state.last_zero_ts
                if elapsed < MIN_OFF_TIME_S:
                    log.warning(f"EVSE id={evse_id}: non-zero-write suppressed "
                                f"({elapsed:.1f}s since last zero-write, min-off={MIN_OFF_TIME_S:.0f}s)")
                    return False
            return True
    except Exception:
        # Im Zweifel schreiben lassen: Upstream-Verhalten ist das sichere Fallback.
        log.exception("Fehler im EVSE-Übergangsfilter")
        return True


def record_write(evse_id: int, formatted_current: int, force: bool = False) -> None:
    """Meldet einen tatsächlich erfolgten Schreibzugriff zurück.

    Erzwungene Schreibzugriffe werden bewusst nicht vermerkt, sonst würde die
    Ladung nach einer Phasenumschaltung für MIN_OFF_TIME_S blockiert.
    """
    if force:
        return
    try:
        with _lock:
            state = _state(evse_id)
            is_zero = formatted_current == 0
            now = time.monotonic()
            if is_zero:
                state.last_zero_ts = now
            else:
                state.last_nonzero_ts = now
            if state.last_was_zero is not None and is_zero != state.last_was_zero:
                state.transitions.append(now)
                state.transitions = [t for t in state.transitions if now - t <= TOGGLE_WINDOW_S]
                if len(state.transitions) >= TOGGLE_WARN_COUNT:
                    log.warning(f"EVSE id={evse_id}: {len(state.transitions)} zero/non-zero toggles "
                                f"in {TOGGLE_WINDOW_S:.0f}s — possible relay loop!")
            state.last_was_zero = is_zero
    except Exception:
        log.exception("Fehler im EVSE-Übergangsfilter")


def reset() -> None:
    """Verwirft den kompletten Zustand. Nur für Tests."""
    with _lock:
        _states.clear()
