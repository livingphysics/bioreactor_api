"""
CO2 + O2 gas sampler.

A background thread that reads the gas sensors every `period_s` (default 5s) and
caches the latest values for /api/state and the history buffer. Independent of runs.

Two sensor kinds, because they have very different read costs:

  'atlas'  — Atlas Scientific EZO. Each "R" read needs a ~1.5s processing wait. To
             avoid starving the shared I2C bus, the wait is done WITHOUT the lock:
             write "R" (under HARDWARE_LOCK) -> sleep the processing delay (no lock)
             -> read the result (under HARDWARE_LOCK) -> parse. So the lock is held
             only for the two brief transfers, not the whole read.

  'direct' — a self-contained read_fn() that does the whole transaction itself
             (e.g. the Senseair K33, ~50ms). Called once under HARDWARE_LOCK; too
             short to be worth splitting, and the fn owns its own bus handle.
"""
import time
import random
import logging
import threading

logger = logging.getLogger(__name__)


class GasSampler:
    def __init__(self):
        self._lock = threading.Lock()      # guards config + latest
        self._thread = None
        self._stop = threading.Event()

        self._hw_lock = None               # control.HARDWARE_LOCK
        self._sensors = []                 # [{'name','kind','cast', + per-kind keys}]
        self._sim = False
        self._period = 5.0

        self._latest = {}                  # {'co2': int|None, 'o2': float|None}
        self._latest_t = 0                 # ms

    # ------------------------------------------------------------------ setup
    def configure(self, *, hw_lock, sensors, sim, period_s=5.0):
        with self._lock:
            self._hw_lock = hw_lock
            self._sensors = list(sensors)
            self._sim = sim
            self._period = max(1.0, float(period_s))
            self._latest = {s['name']: None for s in self._sensors}

    @property
    def has_sensors(self) -> bool:
        return bool(self._sensors)

    def start(self):
        if not self._sensors or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gas-sampler")
        self._thread.start()
        logger.info("Gas sampler started (sensors=%s, every %.1fs)",
                    [s['name'] for s in self._sensors], self._period)

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=4.0)

    def latest(self):
        with self._lock:
            return dict(self._latest)

    def status(self):
        with self._lock:
            return {"period_s": self._period, "sensors": [s['name'] for s in self._sensors]}

    # ------------------------------------------------------------------ loop
    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            for s in self._sensors:
                if self._stop.is_set():
                    break
                try:
                    val = self._read_one(s)
                except Exception as e:
                    logger.error("Gas read (%s) failed: %s", s.get('name'), e)
                    val = None
                with self._lock:
                    self._latest[s['name']] = val
                    self._latest_t = int(time.time() * 1000)
            self._stop.wait(max(0.0, self._period - (time.time() - t0)))

    def _read_one(self, s):
        name, cast = s['name'], s['cast']
        if self._sim:
            return random.randint(400, 1500) if name == 'co2' else round(random.uniform(19.5, 21.0), 2)
        if s.get('kind') == 'direct':
            # Self-contained read (K33: ~50ms) — one short transaction under the lock.
            with self._hw_lock:
                val = s['read_fn']()
            return None if val is None else cast(val)

        device, delay_ms = s['device'], s['delay']
        # write the read command (bus), release the lock for the processing wait,
        # then read back the result (bus).
        with self._hw_lock:
            device.write("R")
        if self._stop.wait(delay_ms / 1000.0):
            return None                                   # interrupted by shutdown
        with self._hw_lock:
            resp = device.read("R")
        data = getattr(resp, "data", None)
        if not data:
            return None
        try:
            text = data.decode(errors="ignore").strip()
        except AttributeError:
            text = str(data).strip()
        if not text:
            return None
        return cast(float(text.split()[0]))


# Module-level singleton
gas_sampler = GasSampler()
