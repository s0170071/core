"""Instant-trigger MQTT helper.

Subscribes to a curated whitelist of "user-action" topics on the LIVE tree
(post-SetData / post-SubData) and sets a `threading.Event` that wakes the
main loop in `packages/main.py` so that a forced algorithm tick runs
immediately, instead of waiting for the next periodic 10-second tick.

See `instantgui.md` for the full design rationale.
"""
import logging
import threading
import time

import paho.mqtt.client as mqtt

from control import data

log = logging.getLogger(__name__)

# Module-level handle to the live InstantTrigger instance. Used by other
# subsystems (e.g. phase_switch) to request a one-shot algorithm tick
# without having to plumb the trigger Event through their own APIs.
_instance = None  # type: "InstantTrigger | None"

# All filters target the LIVE tree (post-SetData / post-SubData). The literal
# `/set/` segments inside `openWB/chargepoint/<n>/set/...` are path segments,
# NOT the `openWB/set/` validation prefix.
INSTANT_TOPIC_FILTERS = (
    # Whole charge template, republished per CP by SetData.
    "openWB/chargepoint/+/set/charge_template",
    # Same template, republished under the vehicle-template tree.
    "openWB/vehicle/template/charge_template/+",
    # Manual lock toggle (scalar publish).
    "openWB/chargepoint/+/set/manual_lock",
    # CP config (vehicle assignment, SoC reconfig, …).
    "openWB/chargepoint/+/config",
)


# Topics that publish a single scalar value per user action and never
# arrive in bursts. We fire the trigger event immediately for these and
# skip the 150 ms debounce window. Compared on the literal MQTT topic
# string after the wildcard match.
IMMEDIATE_TOPIC_SUFFIXES = (
    "/set/manual_lock",
)


class InstantTrigger:
    """Subscribes to UI-action topics and sets an event that wakes the main
    loop so that one algorithm tick is executed immediately instead of
    waiting for the next periodic tick.
    """

    # Cooldown safety margin on top of `control_interval / 2`.
    COOLDOWN_SAFETY_MARGIN_S = 3.0

    def __init__(self,
                 trigger_event: threading.Event,
                 debounce_s: float = 0.15):
        self._trigger_event = trigger_event
        self._debounce_s = debounce_s
        self._lock = threading.Lock()
        self._pending_timer = None  # type: threading.Timer | None
        self._last_user_trigger_ts = 0.0
        # Per-topic last-seen payload bytes. Used to suppress algorithm
        # echo (whole charge_template republish on every tick) without a
        # time-based cooldown that would also delay real user actions
        # landing right after an algorithm run.
        self._last_payload = {}  # type: dict
        # First message per topic comes from the retained value when we
        # subscribe at startup. Don't fire on those — only on later
        # changes. Tracked per-topic.
        self._seen = set()  # type: set
        self._client = mqtt.Client(client_id="openWB-instant-trigger",
                                   clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        global _instance
        _instance = self

    # ---- public ----------------------------------------------------------
    def start(self, host: str = "localhost", port: int = 1886) -> None:
        try:
            self._client.connect(host, port, keepalive=60)
            self._client.loop_start()
            log.info("InstantTrigger gestartet.")
        except Exception:
            log.exception("InstantTrigger: Verbindung zum Broker fehlgeschlagen")

    def stop(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            log.exception("InstantTrigger: Fehler beim Stoppen")
        with self._lock:
            if self._pending_timer is not None:
                self._pending_timer.cancel()
                self._pending_timer = None

    # ---- MQTT callbacks --------------------------------------------------
    def _on_connect(self, client, userdata, flags, rc):
        try:
            for f in INSTANT_TOPIC_FILTERS:
                client.subscribe(f, qos=2)
            log.debug("InstantTrigger: Abonniert: %s", INSTANT_TOPIC_FILTERS)
        except Exception:
            log.exception("InstantTrigger: Fehler beim Abonnieren")

    def _on_message(self, client, userdata, msg):
        try:
            payload = bytes(msg.payload) if msg.payload is not None else b""
            with self._lock:
                first_time = msg.topic not in self._seen
                last = self._last_payload.get(msg.topic)
                self._last_payload[msg.topic] = payload
                self._seen.add(msg.topic)
            if first_time:
                # Retained value delivered on subscribe — establish baseline,
                # do not fire.
                return
            if payload == last:
                # Algorithm-echo or other re-publish with identical content.
                # Real change to the same value is impossible to distinguish
                # via MQTT, but for our trigger filters (chargemode template,
                # manual_lock, cp config) an unchanged payload means there is
                # nothing for the algorithm to react to anyway.
                return
            # Scalar single-shot topics bypass the debounce window because
            # they never arrive as a burst.
            if any(msg.topic.endswith(suffix) for suffix in IMMEDIATE_TOPIC_SUFFIXES):
                self._fire()
                return
            self._schedule_trigger()
        except Exception:
            log.exception("InstantTrigger: Fehler beim Planen des Triggers")

    # ---- debounce -------------------------------------------------------
    def _schedule_trigger(self) -> None:
        with self._lock:
            # Debounce: coalesce bursts (whole-template publish fans out to
            # multiple sub-topics) into a single trigger. No time-based
            # cooldown — content dedup in `_on_message` is responsible for
            # suppressing algorithm echo.
            if self._pending_timer is not None:
                self._pending_timer.cancel()
            self._pending_timer = threading.Timer(self._debounce_s, self._fire)
            self._pending_timer.daemon = True
            self._pending_timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._pending_timer = None
            self._last_user_trigger_ts = time.monotonic()
        log.debug("InstantTrigger: signalling main loop to reschedule handler10Sec.")
        self._trigger_event.set()

    def request_tick(self, delay_s: float = 0.0, reason: str = "") -> None:
        """Bypass the MQTT debounce/cooldown and request an immediate (or
        delayed) algorithm tick. Used by internal subsystems that already
        know they need to re-run the algorithm soon (e.g. after a phase
        switch settles)."""
        def _do() -> None:
            log.debug(
                "InstantTrigger: post-action tick requested (%s).", reason or "-")
            self._trigger_event.set()
        if delay_s <= 0:
            _do()
            return
        t = threading.Timer(delay_s, _do)
        t.daemon = True
        t.start()


def request_algorithm_tick(delay_s: float = 0.0, reason: str = "") -> bool:
    """Module-level convenience: ask the live InstantTrigger to fire an
    algorithm tick after `delay_s` seconds. Returns False if no instance
    has been created yet (e.g. very early at boot)."""
    inst = _instance
    if inst is None:
        return False
    inst.request_tick(delay_s=delay_s, reason=reason)
    return True
