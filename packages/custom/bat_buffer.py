"""Hausspeicher als Puffer für die PV-Ladung.

Erweitert den bestehenden Modus ``BatConsiderationMode.MIN_SOC_BAT`` um eine
Hysterese zwischen ``min_bat_soc`` und ``max_bat_soc``:

===========================  ====================================================
Speicher-SoC                 Verhalten
===========================  ====================================================
``> max_bat_soc``            Ladung darf starten, normale Überschussregelung
``min_bat_soc..max_bat_soc`` Ladung läuft weiter, mindestens mit ``min_current``
``< min_bat_soc``            Ladung wird sofort gestoppt
===========================  ====================================================

Der Latch ist eine reine Funktion aus (vorheriger Latch, aktueller SoC). Der SoC
ändert sich innerhalb eines Regelzyklus nicht, daher ist die Auswertung
idempotent und es ist keine Synchronisation auf den Zyklus nötig.

Der Latch liegt bewusst auf Modul-Ebene: ``data.copy_data()`` kopiert in jedem
Zyklus den kompletten Daten-Baum, eigener Zustand im Baum ginge dabei verloren.

Alle Funktionen sind so gebaut, dass sie im Fehlerfall das Verhalten von
Upstream unverändert lassen ("fail open"): Ausnahmen werden protokolliert und
mit dem neutralen Rückgabewert quittiert, nie nach oben durchgereicht.
"""
import logging
from typing import Optional

from control import data
from control.algorithm.chargemodes import CHARGEMODES
from control.chargemode import Chargemode
from control.chargepoint.chargepoint_state import CHARGING_STATES, ChargepointState
from control.limiting_value import LimitingValue

# control.bat_all und control.algorithm.filter_chargepoints ziehen beide
# control.chargepoint.chargepoint und darüber control.ev.ev nach. Da ev.py
# seinerseits dieses Modul importiert, werden diese beiden erst zur Laufzeit
# importiert. sys.modules cacht das, der Aufruf kostet nur einen Dict-Zugriff.

log = logging.getLogger(__name__)

# Nur der reine PV-Modus (beide Prioritäten). Scheduled- und Eco-Laden im
# PV-Submodus bleiben bewusst unberührt, damit deren Zeitpunkt-Logik nicht gegen
# den Mindeststrom arbeitet.
CONSIDERED_CHARGE_MODES_BAT_BUFFER = CHARGEMODES[14:16]

SWITCH_ON_BLOCKED = ("Ladung wird nicht gestartet, da der Speicher-SoC von {}% nicht über der Grenze von {}% liegt.")
SWITCH_OFF_BAT_EMPTY = ("Ladung wird sofort gestoppt, da der Speicher-SoC von {}% unter die Grenze von {}% gefallen "
                        "ist.")
FLOOR_SKIPPED = ("Mindeststrom aus dem Speicher-Puffer wird nicht gesetzt, da der Ladepunkt durch eine harte "
                 "Strom-Begrenzung limitiert ist ({}).")

# Hysterese-Latch: True, solange der Speicher die Ladung puffern darf.
_buffering = False


def _pv_config():
    return data.data.general_data.data.chargemode_config.pv_charging


def active() -> bool:
    """Ist die Funktion überhaupt zuständig?

    Nur wenn ein Speicher konfiguriert ist und der Nutzer den Modus
    ``min_soc_bat_mode`` gewählt hat. In allen anderen Fällen verhält sich
    openWB exakt wie Upstream.
    """
    from control.bat_all import BatConsiderationMode
    try:
        if not data.data.bat_all_data.data.config.configured or len(data.data.bat_data) < 1:
            return False
        return _pv_config().bat_mode == BatConsiderationMode.MIN_SOC_BAT.value
    except (AttributeError, KeyError):
        # Datenbaum noch nicht initialisiert
        return False


def buffering() -> bool:
    """Wertet den Hysterese-Latch aus und gibt ihn zurück."""
    global _buffering
    if not active():
        _buffering = False
        return False
    try:
        pv_config = _pv_config()
        soc = data.data.bat_all_data.data.get.soc
        if soc > pv_config.max_bat_soc:
            if not _buffering:
                log.info(f"Speicher-Puffer aktiviert: SoC {soc}% über max_bat_soc {pv_config.max_bat_soc}%.")
            _buffering = True
        elif soc < pv_config.min_bat_soc:
            if _buffering:
                log.info(f"Speicher-Puffer erschöpft: SoC {soc}% unter min_bat_soc {pv_config.min_bat_soc}%.")
            _buffering = False
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
    return _buffering


def may_start() -> bool:
    """Darf eine Ladung *neu* beginnen?

    Bewusst strenger als :func:`buffering`: der Latch bleibt im Band zwischen
    min_bat_soc und max_bat_soc gesetzt, damit eine *laufende* Ladung
    weiterläuft. Eine *neue* Ladung darf aber nur oberhalb von max_bat_soc
    starten, sonst würde ein später eingesteckter Ladepunkt den Speicher
    anzapfen, der bereits am Entladen ist.
    """
    if not active():
        return True
    try:
        return data.data.bat_all_data.data.get.soc > _pv_config().max_bat_soc
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return True


def _in_scope(control_parameter) -> bool:
    return (control_parameter.chargemode == Chargemode.PV_CHARGING and
            control_parameter.submode == Chargemode.PV_CHARGING)


def block_switch_on(counter, chargepoint) -> bool:
    """Start-Sperre: Ladung darf erst starten, wenn der SoC über ``max_bat_soc`` liegt.

    :return: True, wenn der Aufrufer sofort zurückkehren soll. Der Hook hat dann
             Status und Zeitstempel bereits gesetzt. False -> Upstream macht weiter.
    """
    try:
        control_parameter = chargepoint.data.control_parameter
        if not _in_scope(control_parameter) or not active():
            return False
        # buffering() muss trotzdem laufen, damit der Latch auch dann gepflegt
        # wird, wenn gerade kein Ladepunkt lädt.
        buffering()
        if may_start():
            return False
        pv_config = _pv_config()
        if control_parameter.state == ChargepointState.SWITCH_ON_DELAY:
            # Eine bereits laufende Einschaltverzögerung abbrechen und die dafür
            # reservierte Leistung exakt so zurückgeben, wie Upstream es beim
            # Unterschreiten der Einschaltschwelle tut. Ohne diese Rückgabe
            # blockiert das Leck jedes künftige Einschalten.
            counter.data.set.reserved_surplus -= pv_config.switch_on_threshold * control_parameter.phases
            control_parameter.timestamp_switch_on_off = None
        control_parameter.state = ChargepointState.NO_CHARGING_ALLOWED
        chargepoint.set_state_and_log(SWITCH_ON_BLOCKED.format(
            data.data.bat_all_data.data.get.soc, pv_config.max_bat_soc))
        return True
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return False


def switch_off_decision(chargepoint) -> Optional[bool]:
    """Abschalt-Entscheidung.

    :return: True -> Abschaltung verhindern, weiterladen.
             False -> sofort abschalten.
             None -> Upstream entscheidet.
    """
    try:
        control_parameter = chargepoint.data.control_parameter
        if not _in_scope(control_parameter) or not active():
            return None
        if chargepoint.data.set.charging_ev_data.ev_template.data.prevent_charge_stop:
            # Fahrzeuge, die eine Unterbrechung nicht verkraften, werden nicht
            # angefasst; sonst würde der Speicher leergezogen.
            return None
        if buffering():
            return True
        # Puffer erschöpft. Bewusst ohne Abschaltverzögerung: es gibt nichts mehr
        # abzuwarten. Die Verzögerung der eigentlichen Hardware-Schreibzugriffe
        # übernimmt der EVSE-Filter (Feature 1), damit das Fahrzeug nicht in einen
        # Fehlerzustand läuft.
        if control_parameter.state == ChargepointState.SWITCH_OFF_DELAY:
            # Eine laufende Abschaltverzögerung wird übersprungen, die dafür
            # freigegebene Leistung muss zurückgenommen werden.
            data.data.counter_all_data.get_evu_counter().data.set.released_surplus -= (
                chargepoint.data.set.required_power)
            control_parameter.timestamp_switch_on_off = None
        control_parameter.state = ChargepointState.NO_CHARGING_ALLOWED
        chargepoint.set_state_and_log(SWITCH_OFF_BAT_EMPTY.format(
            data.data.bat_all_data.data.get.soc, _pv_config().min_bat_soc))
        return False
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return None


def apply_min_current_floor() -> None:
    """Hebt den Soll-Strom puffernder Ladepunkte wieder auf ``min_current`` an.

    Läuft als letzter Schritt des Regel-Algorithmus, also nach allen Stufen, die
    den Strom auf 0 bzw. None setzen könnten. Der Wert wird ausschließlich
    angehoben, nie gesenkt.
    """
    from control.algorithm.filter_chargepoints import get_chargepoints_by_chargemode
    try:
        if not buffering():
            return
        for cp in get_chargepoints_by_chargemode(CONSIDERED_CHARGE_MODES_BAT_BUFFER):
            control_parameter = cp.data.control_parameter
            if control_parameter.state not in CHARGING_STATES:
                continue
            # Der angehobene Strom ist bei den Zähler-Diffs nicht mehr gebucht.
            # Gegenüber dem Speicher ist genau das gewollt, eine physikalische
            # Grenze muss aber respektiert werden.
            limit = control_parameter.limit
            limiting_value = limit.limiting_value if limit else None
            if limiting_value is not None and limiting_value != LimitingValue.POWER:
                log.info(f"LP {cp.num}: {FLOOR_SKIPPED.format(limiting_value.name)}")
                continue
            min_current = control_parameter.min_current
            if (cp.data.set.current or 0) < min_current:
                log.info(f"LP {cp.num}: Speicher-Puffer hält den Ladepunkt auf {min_current}A.")
                cp.data.set.current = min_current
    except Exception:
        log.exception("Fehler im Speicher-Puffer")


def suppress_3_to_1(control_parameter) -> bool:
    """Unterdrückt die automatische Rückschaltung 3->1 Phasen während des Pufferns.

    Der Auslöser von Upstream ist ``surplus <= 0`` -- exakt die Bedingung, die der
    Puffer überbrücken soll. Ohne diese Sperre würde sofort zurückgeschaltet und
    das Halten auf ``min_current`` liefe ins Leere.

    Die Hochschaltung 1->3 bleibt unangetastet.
    """
    try:
        if not _in_scope(control_parameter):
            return False
        return buffering()
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return False
