"""Background runner that decouples phase A (loadvars) from the 10 s
algorithm tick.

See `decouple.md` in the workspace root for the design rationale.

Thread-safety contract
----------------------
* Only this runner calls `loadvars_.get_values()` and the final snapshot
  copy `data.data.copy_data()`.
* The algorithm (`HandlerAlgorithm.handler10Sec`) acquires
  `snapshot_lock()` for the entire phase B/C duration.
* The runner takes `snapshot_lock()` only around the final
  `data.data.copy_data()` call. During `get_values()` itself the runner
  does NOT hold the lock — `loadvars.get_values()` mutates
  `data.data.bat_data` / `pv_data` / `counter_data` incrementally via
  `copy_module_data()`, exactly as in the original synchronous flow. The
  algorithm reads from `data.data.*` either between cycles (lock free)
  or during the final copy (serialised by the lock).
"""
import logging
import threading
import time
from typing import Optional

from control import data
from modules import loadvars

log = logging.getLogger(__name__)


class LoadvarsRunner:
    # Minimum gap between two consecutive phase-A cycles. Avoids hammering
    # modbus devices when nothing requested a fresh snapshot.
    MIN_CYCLE_GAP_S = 1.0

    def __init__(self, loadvars_: "loadvars.Loadvars"):
        self._loadvars = loadvars_
        self._stop = threading.Event()
        self._snapshot_lock = threading.RLock()
        self._cycle_done = threading.Event()
        self._fresh_request = threading.Event()
        self._last_cycle_end = 0.0
        self._last_cycle_duration = 0.0
        self._cycle_count = 0
        self._thread: Optional[threading.Thread] = None

    # ---- public API used by main.py -------------------------------------
    def start(self) -> None:
        # Kick off a first cycle immediately so the algorithm has fresh
        # data on its initial tick.
        self._fresh_request.set()
        self._thread = threading.Thread(
            target=self._run, name="LoadvarsRunner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._fresh_request.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def snapshot_lock(self) -> threading.RLock:
        """Held by the algorithm during phase B/C so the runner does not
        overwrite the snapshot mid-decision."""
        return self._snapshot_lock

    def request_fresh_and_wait(self, timeout_s: float) -> bool:
        """Ask the runner to start the next cycle immediately and block
        until it finished (or `timeout_s` elapsed). Returns True if a
        fresh cycle completed within the timeout."""
        self._cycle_done.clear()
        self._fresh_request.set()
        return self._cycle_done.wait(timeout=timeout_s)

    def last_snapshot_age_s(self) -> float:
        if self._last_cycle_end == 0.0:
            return float("inf")
        return time.monotonic() - self._last_cycle_end

    def cycle_count(self) -> int:
        return self._cycle_count

    # ---- worker ---------------------------------------------------------
    def _run(self) -> None:
        log.info("LoadvarsRunner gestartet.")
        while not self._stop.is_set():
            try:
                self._fresh_request.wait()
                if self._stop.is_set():
                    break
                self._fresh_request.clear()

                t0 = time.monotonic()
                # Phase A: fetch values from all configured devices and
                # publish them to SubData via the existing MQTT pipeline.
                # Internally calls data.data.copy_module_data() multiple
                # times — that is intentional and identical to the
                # behaviour today inside handler10Sec.
                try:
                    self._loadvars.get_values()
                except Exception:
                    log.exception("LoadvarsRunner: get_values failed")

                # Final snapshot of SubData -> data.data under the lock so
                # the algorithm sees a coherent state when it next runs.
                try:
                    with self._snapshot_lock:
                        data.data.copy_data()
                except Exception:
                    log.exception("LoadvarsRunner: copy_data failed")

                self._last_cycle_duration = time.monotonic() - t0
                self._last_cycle_end = time.monotonic()
                self._cycle_count += 1
                self._cycle_done.set()
                log.debug(
                    "LoadvarsRunner: cycle %d done in %.2fs",
                    self._cycle_count, self._last_cycle_duration)

                # Auto-rearm so phase A keeps refreshing in the background
                # even when nobody explicitly requested a fresh cycle.
                if not self._fresh_request.is_set():
                    if self._stop.wait(self.MIN_CYCLE_GAP_S):
                        break
                    self._fresh_request.set()
            except Exception:
                log.exception("LoadvarsRunner cycle failed")
                # Avoid tight-looping on a persistent failure.
                if self._stop.wait(2):
                    break
        log.info("LoadvarsRunner gestoppt.")
