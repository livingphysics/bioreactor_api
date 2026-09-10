"""
IR-gated optical-density sampler.

A background thread that measures OD by pulsing the IR LED for every reading:

    LED on (od_led_power)  ->  settle  ->  read source  ->  post-read pause  ->  LED off

Sources are grouped by KIND ('adc' = ADS1115 channels, 'eyespy' = ADS1114 boards) and
keyed by voltage-source NAME. When both kinds are present it INTERLEAVES them: one kind
per LED pulse, alternating, so each kind samples at half the pulse rate. latest() is
{source name: volts}; main.py maps it onto the canonical OD measurements.

The whole gated measurement runs under HARDWARE_LOCK so the LED can't be toggled
mid-measurement by the heater loop / other I2C readers. The most recent reading is
exposed via latest() for /api/state and the history sampler.

When disabled (the frontend "LED off" for OD), the loop idles and the LED stays off.
Independent of the passive temp-history sampler.
"""
import time
import random
import logging
import threading

logger = logging.getLogger(__name__)


class ODSampler:
    def __init__(self):
        self._lock = threading.Lock()      # guards config + latest
        self._thread = None
        self._stop = threading.Event()

        # dependencies (set via configure)
        self._hw_lock = None               # control.HARDWARE_LOCK
        self._set_led = None               # callable(power_percent)
        self._read_fns = {}                # {kind: fn(source_name)->volts}
        self._sources = []                 # [(kind, [source names...]), ...]
        self._ring_dodge = None            # callable(active): ring off for the read, then restore
        self._sim = False

        # config (frontend-settable)
        self._enabled = True
        self._led_power = 10.0
        self._settle_s = 0.5
        self._post_read_s = 0.1
        self._period_s = 1.0

        # state
        self._latest = {}                  # {source name: volts}
        self._latest_t = 0                 # ms
        self._src_idx = 0

    # ------------------------------------------------------------------ setup
    def configure(self, *, hw_lock, set_led, read_fns, sources, sim,
                  enabled=True, led_power=10.0, settle_s=0.5, post_read_s=0.1, period_s=1.0,
                  ring_dodge=None):
        with self._lock:
            self._hw_lock = hw_lock
            self._set_led = set_led
            self._read_fns = read_fns
            self._sources = [(name, chans) for (name, chans) in sources if chans]
            self._ring_dodge = ring_dodge
            self._sim = sim
            self._enabled = enabled
            self._led_power = max(0.0, min(float(led_power), 100.0))
            self._settle_s = max(0.0, float(settle_s))
            self._post_read_s = max(0.0, float(post_read_s))
            self._period_s = max(0.2, float(period_s))
            self._latest = {}
            self._src_idx = 0

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    @property
    def led_power(self) -> float:
        """Current IR LED % (the live dropdown value); also used by the heater
        run loop so run-CSV OD is illuminated identically to the live buffer."""
        with self._lock:
            return self._led_power

    def start(self):
        if not self._sources or self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="od-sampler")
        self._thread.start()
        logger.info("OD sampler started (kinds=%s, %.1f%% LED, %.2fs settle, %.1fs period)",
                    [s[0] for s in self._sources], self._led_power, self._settle_s, self._period_s)

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and threading.current_thread() is not t:
            t.join(timeout=3.0)
        # make sure the LED is left off
        try:
            if not self._sim and self._set_led:
                with self._hw_lock:
                    self._set_led(0)
        except Exception:
            pass

    # ------------------------------------------------------------------ control
    def set_config(self, enabled=None, led_power=None):
        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
                # Drop any prior reading so a re-enable can't surface stale OD as
                # "live" until a fresh gated pulse completes; restart interleaving.
                self._latest = {}
                self._latest_t = 0
                self._src_idx = 0
            if led_power is not None:
                self._led_power = max(0.0, min(float(led_power), 100.0))
            cfg = self._status_locked()
        # if just disabled, turn the LED off promptly
        if enabled is False and not self._sim and self._set_led:
            try:
                with self._hw_lock:
                    self._set_led(0)
            except Exception:
                pass
        return cfg

    def latest(self):
        with self._lock:
            if not self._enabled or not self._latest:
                return None   # sampling off / nothing measured yet -> plot shows a gap
            # If the sampler thread has stalled/died, don't keep surfacing an old
            # reading as live — report a gap once the newest pulse is too stale.
            stale_ms = max(10.0, 5.0 * self._period_s) * 1000.0
            if self._latest_t and (int(time.time() * 1000) - self._latest_t) > stale_ms:
                return None
            return dict(self._latest)

    def status(self):
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        return {
            "enabled": self._enabled,
            "led_power": self._led_power,
            "settle_s": self._settle_s,
            "period_s": self._period_s,
            "kinds": [s[0] for s in self._sources],
        }

    # ------------------------------------------------------------------ loop
    def _run(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                with self._lock:
                    enabled = self._enabled
                    srcs = list(self._sources)
                if enabled and srcs:
                    self._measure_once(srcs)
            except Exception as e:
                logger.error("OD sample failed: %s", e, exc_info=True)
            self._stop.wait(max(0.0, self._period_s - (time.time() - t0)))

    def _measure_once(self, srcs):
        # interleave: one KIND per pulse (all its sources read together), alternating
        kind, chans = srcs[self._src_idx % len(srcs)]
        self._src_idx += 1
        readings = {}

        if self._sim:
            for i, ch in enumerate(chans):
                readings[ch] = round(0.5 + 0.3 * i + random.uniform(-0.02, 0.02), 4)
        else:
            with self._lock:
                power = self._led_power
                settle, post = self._settle_s, self._post_read_s
            fn = self._read_fns.get(kind)
            # Atomic gated measurement: hold the bus so nothing toggles the LED mid-read.
            with self._hw_lock:
                dodged = False
                try:
                    # Dodge: turn the ring OFF for the whole IR-on window so its light
                    # can't contaminate the read (and it's off while the IR PWM couples
                    # SPI noise into the ring's data line). Restored in `finally`, which
                    # also re-asserts the colour and so corrects any noise glitch.
                    if self._ring_dodge:
                        try:
                            self._ring_dodge(True)
                            dodged = True
                        except Exception:
                            pass
                    self._set_led(power)             # IR on
                    time.sleep(settle)               # settle
                    for ch in chans:
                        try:
                            v = fn(ch) if fn else None
                        except Exception:
                            v = None
                        readings[ch] = None if (isinstance(v, float) and v != v) else v
                    time.sleep(post)                 # post-read pause
                finally:
                    try:
                        self._set_led(0)             # IR off (always)
                    except Exception:
                        pass
                    if dodged:
                        try:
                            self._ring_dodge(False)  # restore the ring (always)
                        except Exception:
                            pass

        with self._lock:
            self._latest.update(readings)
            self._latest_t = int(time.time() * 1000)


# Module-level singleton
od_sampler = ODSampler()
