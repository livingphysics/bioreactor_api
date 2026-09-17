"""
Relay controller for the bioreactor API.

Thin layer over the RelayDriver (which stores each relay's state, so toggle + GET
work). Maps the API's open/closed/toggle commands to the driver's energized bool:

    closed = energized (driver ON)   -- e.g. NO contact closed
    open   = de-energized (OFF)      -- the boot state

and manages the timed "command-wait-toggle" action: run a command now, then toggle
the relay after a delay (a one-shot timed pulse). Hardware access is delegated to
injected set/get fns, so this module runs unchanged in simulation. GPIO writes
must not wait for the I2C hardware lock.
"""
import time
import logging
import threading

logger = logging.getLogger(__name__)


class RelaySafetyError(Exception):
    """A guarded relay refused a dose (rate limit / CO2 too high)."""


class RelayController:
    _CMD_ENERGIZED = {'open': False, 'closed': True}   # command -> driver state
    COMMANDS = ('open', 'closed', 'toggle')

    def __init__(self):
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._on_change = None
        self._set_fn = None        # set_fn(name, energized: bool)
        self._get_fn = None        # get_fn() -> {name: energized bool}
        self._names = []
        self._timers = {}          # name -> (Timer, fire_at_epoch)
        self._guards = {}          # name -> {max_duration_s, min_interval_s, co2_max_ppm}
        self._co2_fn = None        # () -> current CO2 ppm (or None)
        self._last_dose = {}       # name -> epoch of last dose
        self._closed_total = {}    # name -> cumulative energized (closed) seconds
        self._closed_since = {}    # name -> monotonic time of successful ON write

    def configure(self, *, set_fn, get_fn, names, guards=None, co2_fn=None, on_change=None):
        self._set_fn = set_fn
        self._get_fn = get_fn
        self._names = list(names)
        self._guards = {k: v for k, v in (guards or {}).items() if k in self._names}
        self._co2_fn = co2_fn
        self._on_change = on_change

    # ----------------------------------------------------------------- helpers
    def _energized(self, name) -> bool:
        return bool((self._get_fn() or {}).get(name, False))

    def _cancel_timer(self, name):
        with self._lock:
            entry = self._timers.pop(name, None)
        if entry:
            entry[0].cancel()

    def _set(self, name, energized):
        """Drive the relay AND accumulate cumulative closed (energized) time, so brief
        doses — which can start and end entirely between CSV/history samples — are still
        captured. Every relay state change routes through here."""
        with self._state_lock:
            # Timestamp completed writes, not requests: a delayed OFF must count
            # as energized time, and a failed OFF must leave the interval open.
            self._set_fn(name, energized)
            now = time.monotonic()
            since = self._closed_since.get(name)
            if energized and since is None:
                self._closed_since[name] = now
            elif not energized and since is not None:
                self._closed_total[name] = self._closed_total.get(name, 0.0) + (now - since)
                self._closed_since[name] = None
            if self._on_change:
                self._on_change(self.closed_seconds())

    def closed_seconds(self) -> dict:
        """Cumulative closed (energized) seconds per relay, including any in-progress
        close. Monotonic per process — diff successive samples for per-interval dose time."""
        with self._state_lock:
            now = time.monotonic()
            out = {}
            for name in self._names:
                total = self._closed_total.get(name, 0.0)
                since = self._closed_since.get(name)
                out[name] = round(total + (now - since if since is not None else 0.0), 3)
            return out

    # --------------------------------------------------------------------- API
    def _target(self, name, command) -> bool:
        if command == 'toggle':
            return not self._energized(name)
        if command in self._CMD_ENERGIZED:
            return self._CMD_ENERGIZED[command]
        raise ValueError(f"bad relay command {command!r} (use {'/'.join(self.COMMANDS)})")

    def apply(self, name, command) -> str:
        """Run one command ('open'|'closed'|'toggle'). Supersedes any pending timed
        toggle. Returns the new state string. Closing a guarded relay is a dose."""
        if name not in self._names:
            raise KeyError(name)
        target = self._target(name, command)
        if target and name in self._guards:      # closing a guarded relay = a dose
            return self._dose(name)              # manages its own timer; raises (untouched) if blocked
        self._cancel_timer(name)                 # a fresh command wins over a scheduled toggle/dose
        self._set(name, target)
        return 'closed' if target else 'open'

    def timed(self, name, command, duration_s) -> str:
        """command-wait-toggle: run `command` now, then toggle after `duration_s`. For a
        guarded relay, closing is a single auto-reverting dose capped at max_duration_s."""
        if name not in self._names:
            raise KeyError(name)
        target = self._target(name, command)
        if target and name in self._guards:
            return self._dose(name, requested=float(duration_s))
        state = self.apply(name, command)     # cancels any prior timer
        duration_s = float(duration_s)
        if duration_s > 0:
            t = threading.Timer(duration_s, self._fire_toggle, args=(name,))
            t.daemon = True
            with self._lock:
                self._timers[name] = (t, time.time() + duration_s)
            t.start()
        return state

    # ------------------------------------------------------------ safety-guarded dose
    def _dose(self, name, requested=None) -> str:
        """Close a guarded relay as a rate-limited, CO2-gated, auto-reverting dose.
        Raises RelaySafetyError if refused."""
        g = self._guards[name]
        cap = g.get('co2_max_ppm')
        if cap is not None:
            co2 = None
            try:
                co2 = self._co2_fn() if self._co2_fn else None
            except Exception:
                co2 = None
            if co2 is None or co2 > cap:
                raise RelaySafetyError(
                    f"{name} dose blocked: CO2 "
                    f"{'unknown' if co2 is None else f'{co2:.0f} ppm'} (limit {cap} ppm)")
        now = time.time()
        interval = g.get('min_interval_s', 0.0)
        wait = interval - (now - self._last_dose.get(name, -1e12))
        if wait > 0:
            raise RelaySafetyError(f"{name}: one dose per {interval:.0f}s — wait {wait:.0f}s")
        maxd = float(g.get('max_duration_s', 1.0))
        dur = maxd if not requested else max(0.05, min(float(requested), maxd))
        self._last_dose[name] = now
        self._cancel_timer(name)
        self._set(name, True)                         # dose ON (closed)
        t = threading.Timer(dur, self._end_dose, args=(name,))
        t.daemon = True
        with self._lock:
            self._timers[name] = (t, now + dur)
        t.start()
        logger.info("%s dose: closed for %.2fs", name, dur)
        return 'closed'

    def _end_dose(self, name):
        with self._lock:
            self._timers.pop(name, None)
        try:
            self._set(name, False)                    # auto-revert to open
        except Exception as e:
            logger.error("%s dose-end failed: %s", name, e)

    def _fire_toggle(self, name):
        with self._lock:
            self._timers.pop(name, None)
        try:
            self.apply(name, 'toggle')
            logger.info("Relay %s: timed toggle fired", name)
        except Exception as e:
            logger.error("relay timed toggle failed for %s: %s", name, e)

    def states(self) -> dict:
        g = self._get_fn() or {}
        return {n: ('closed' if g.get(n, False) else 'open') for n in self._names}

    def status(self) -> dict:
        now = time.time()
        with self._lock:
            pending = {n: round(max(0.0, fire_at - now), 1) for n, (_, fire_at) in self._timers.items()}
        out = {'states': self.states(), 'pending': pending,
               'closed_seconds': self.closed_seconds()}
        if self._guards:
            out['guards'] = {}
            for n, g in self._guards.items():
                last = self._last_dose.get(n)
                cooldown = max(0.0, g.get('min_interval_s', 0.0) - (now - last)) if last else 0.0
                out['guards'][n] = {
                    'max_duration_s': g.get('max_duration_s'),
                    'min_interval_s': g.get('min_interval_s'),
                    'co2_max_ppm': g.get('co2_max_ppm'),
                    'cooldown_s': round(cooldown, 1),
                }
        return out

    def stop(self):
        with self._lock:
            timers = [e[0] for e in self._timers.values()]
            self._timers.clear()
        for t in timers:
            t.cancel()


# Module-level singleton used by main.py
relay_controller = RelayController()
