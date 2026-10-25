"""One-process background monitor and optional accelerated demo clock."""

import logging
import threading
import time

log = logging.getLogger(__name__)


class DemoWorker:
    def __init__(self, runtime, bridge=None):
        self.runtime, self.bridge = runtime, bridge
        self._stop = threading.Event()
        self._clock_lock = threading.Lock()
        self.rate = 0
        self._next = 0
        self._thread = None
        self.last_error = None

    def set_rate(self, seconds):
        if type(seconds) is not int or (seconds != 0 and not 5 <= seconds <= 120):
            raise ValueError("Use 0 to pause, or 5 to 120 seconds per simulated day")
        with self._clock_lock:
            self.rate = seconds
            self._next = time.monotonic() + seconds

    def run_once(self):
        with self._clock_lock:
            advance = self.rate and time.monotonic() >= self._next
            if advance:
                self._next = time.monotonic() + self.rate
        if advance:
            self.runtime.advance()
        else:
            self.runtime.tick()
        if self.bridge:
            self.bridge.flush()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.error("Demo worker failed: %s", self.last_error)
            self._stop.wait(1)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("Worker already started")
        self._thread = threading.Thread(target=self._run, name="steady-demo", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
