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

Zusätzlich gilt eine Sonnenstands-Sperre: steht die Sonne tiefer als
``MIN_SUN_ELEVATION``, puffert der Speicher nicht mehr. Der Puffer soll eine
Lücke im Tagesverlauf überbrücken, nicht am Abend den Speicher in das Auto
umladen -- zu dem Zeitpunkt kommt kein Überschuss mehr nach.

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
import math
from datetime import datetime, timezone
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
FLOOR_WOULD_START = ("Mindeststrom aus dem Speicher-Puffer wird nicht gesetzt, da aktuell nicht geladen wird. Der "
                     "Puffer hält eine Ladung, er startet keine.")
SWITCH_OFF_VETOED = "Abschaltung wird verhindert, da der Speicher mit {}% die Ladung puffert."
SUN_TOO_LOW = "Speicher-Puffer deaktiviert: Sonnenstand {:.1f}° liegt unter {:.0f}°."
CLAMPED = ("Nutzbare Speicher-Leistung wird von {:.0f}W auf {:.0f}W begrenzt, damit sich der Puffer wieder "
           "füllen kann.")

# Standort für die Sonnenstandsberechnung. openWB kennt keine Koordinaten, daher
# fest hinterlegt. Ein Fehler von 1° verschiebt den Schaltzeitpunkt um wenige
# Minuten und ist für eine Dämmerungs-Sperre unkritisch.
LATITUDE = 51.16
LONGITUDE = 10.45

# Unterhalb dieser Sonnenhöhe liefert die Anlage keinen nennenswerten Überschuss
# mehr, der Puffer würde also nur noch den Speicher ins Auto umladen.
MIN_SUN_ELEVATION = 20.0

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


def sun_elevation(when: Optional[datetime] = None) -> float:
    """Sonnenhöhe über dem Horizont in Grad.

    Verfahren niedriger Genauigkeit aus dem Astronomical Almanac, Fehler unter
    0,01° -- fuer eine Dämmerungs-Sperre um Größenordnungen mehr als nötig und
    ohne zusätzliche Abhängigkeit.
    """
    when = when or datetime.now(timezone.utc)
    # Tage seit J2000.0; 2440587.5 ist das Julianische Datum der Unix-Epoche.
    n = when.timestamp() / 86400.0 + 2440587.5 - 2451545.0
    mean_longitude = math.radians((280.460 + 0.9856474 * n) % 360)
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360)
    ecliptic_longitude = mean_longitude + math.radians(
        1.915 * math.sin(mean_anomaly) + 0.020 * math.sin(2 * mean_anomaly))
    obliquity = math.radians(23.439 - 0.0000004 * n)
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_longitude))
    right_ascension = math.atan2(math.cos(obliquity) * math.sin(ecliptic_longitude),
                                 math.cos(ecliptic_longitude))
    greenwich_sidereal = ((18.697374558 + 24.06570982441908 * n) % 24) * 15
    hour_angle = math.radians(greenwich_sidereal + LONGITUDE) - right_ascension
    latitude = math.radians(LATITUDE)
    return math.degrees(math.asin(math.sin(latitude) * math.sin(declination) +
                                  math.cos(latitude) * math.cos(declination) * math.cos(hour_angle)))


def sun_is_high_enough() -> bool:
    """Steht die Sonne hoch genug, dass noch Überschuss nachkommt?"""
    try:
        return sun_elevation() >= MIN_SUN_ELEVATION
    except Exception:
        # Fail open: ohne belastbaren Sonnenstand bleibt es beim bisherigen Verhalten.
        log.exception("Fehler im Speicher-Puffer")
        return True


def buffering() -> bool:
    """Wertet den Hysterese-Latch aus und gibt ihn zurück."""
    global _buffering
    if not active():
        _buffering = False
        return False
    try:
        pv_config = _pv_config()
        soc = data.data.bat_all_data.data.get.soc
        if not sun_is_high_enough():
            if _buffering:
                log.info(SUN_TOO_LOW.format(sun_elevation(), MIN_SUN_ELEVATION))
            _buffering = False
            return False
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


def discharge_allowance() -> float:
    """Speicher-Leistung, die bei der *Einschalt*-Entscheidung nicht als Überschuss zählen darf.

    Oberhalb von ``max_bat_soc`` gibt ``bat_all`` ``bat_power_discharge`` als
    nutzbare Leistung frei und schlägt sie über ``charging_power_left`` auf den
    Roh-Überschuss auf. Für eine laufende Ladung ist genau das gewollt -- der
    Speicher soll sie puffern. Die Einschaltschwelle darf sie aber nicht
    überwinden, sonst startet eine Ladung ohne echten PV-Überschuss.

    :return: Watt, die vom Einschalt-Überschuss abzuziehen sind.
    """
    try:
        if not active():
            return 0.0
        bat_all_set = data.data.bat_all_data.data.set
        pv_config = _pv_config()
        if not (pv_config.bat_power_discharge_active and bat_all_set.hysteresis_discharge):
            return 0.0
        # Nie mehr abziehen, als tatsächlich freigegeben wurde: der Rest von
        # charging_power_left ist die echte Speicher-Leistung und bleibt Überschuss.
        return min(pv_config.bat_power_discharge, max(bat_all_set.charging_power_left, 0))
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return 0.0


def clamp_charging_power_left(charging_power_left: float) -> float:
    """Begrenzt die Speicher-Leistung, die der Regelung als Überschuss angeboten wird.

    Upstream schlägt im Band zwischen ``min_bat_soc`` und ``max_bat_soc`` die
    *Ladeleistung* des Speichers auf die freigegebene Entladeleistung auf. Kommt die
    Sonne nach einer Wolke zurück, nimmt das Fahrzeug damit genau die Leistung weg,
    mit der sich der Puffer wieder füllen müsste -- der SoC bleibt am unteren Rand
    kleben, statt zurück auf ``max_bat_soc`` zu laufen.

    Während gepuffert wird zählt deshalb nur der *entladende* Anteil der
    Speicher-Leistung. Oberhalb von ``max_bat_soc`` bleibt es bei Upstream: dort soll
    der Speicher abgeben, statt über die Grenze hinaus zu horten.

    Die Entladefreigabe selbst hängt bewusst nicht am Ladestrom. Sie wird unverändert
    angeboten, den Strom stellt die Überschussregelung ein -- so läuft das Fahrzeug
    aus einem höheren Strom heraus kontrolliert nach unten, statt abgeschaltet zu
    werden.

    Der Rückgabewert ist nie größer als der übergebene: der Puffer schränkt ein, er
    gibt nie zusätzliche Leistung frei.
    """
    try:
        if not active() or not buffering():
            return charging_power_left
        pv_config = _pv_config()
        bat_all_get = data.data.bat_all_data.data.get
        if bat_all_get.soc > pv_config.max_bat_soc:
            return charging_power_left
        discharge_rate = pv_config.bat_power_discharge if pv_config.bat_power_discharge_active else 0
        limit = discharge_rate + min(0, bat_all_get.power)
        if limit < charging_power_left:
            log.info(CLAMPED.format(charging_power_left, limit))
            return limit
        return charging_power_left
    except Exception:
        log.exception("Fehler im Speicher-Puffer")
        return charging_power_left


def _charge_is_flowing(chargepoint) -> bool:
    """Läuft an diesem Ladepunkt gerade wirklich eine Ladung?

    Der Zustand allein reicht nicht: ``SWITCH_OFF_DELAY`` und die
    Phasenumschalt-Zustände gehören zu ``CHARGING_STATES``, führen aber einen
    Soll-Strom von 0. Wer das verwechselt, lässt den Puffer eine Ladung *starten*
    statt sie zu *halten* -- und umgeht damit :func:`may_start`.
    """
    return bool(chargepoint.data.set.current_prev or chargepoint.data.get.charge_state)


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
            # Nur eine tatsächlich laufende Ladung wird gehalten. Bei einem Veto bleibt der
            # Ladepunkt in CHARGING_STATES, und die Mindeststrom-Stufe würde ihn ohne
            # Einschaltschwelle wieder anfahren.
            if not _charge_is_flowing(chargepoint):
                return None
            log.info(f"LP {chargepoint.num}: " + SWITCH_OFF_VETOED.format(data.data.bat_all_data.data.get.soc))
            return True
        if not sun_is_high_enough():
            # Kein harter Stopp bei tiefer Sonne: der Puffer zieht sich nur zurück,
            # abgeschaltet wird nach der normalen Überschuss-Logik von Upstream.
            return None
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
    angehoben, nie gesenkt, und nur bei einer bereits laufenden Ladung.
    """
    from control.algorithm.filter_chargepoints import get_chargepoints_by_chargemode
    try:
        if not buffering():
            return
        for cp in get_chargepoints_by_chargemode(CONSIDERED_CHARGE_MODES_BAT_BUFFER):
            control_parameter = cp.data.control_parameter
            if control_parameter.state not in CHARGING_STATES:
                continue
            if not _charge_is_flowing(cp):
                log.info(f"LP {cp.num}: " + FLOOR_WOULD_START)
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
