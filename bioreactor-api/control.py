"""
Run control engine for the bioreactor API.

Runs a background control loop (1 Hz) that drives the peltier either by stepping
through an uploaded schedule (`duty,direction,hold_s` CSV) or by holding a PID
temperature setpoint. CSV recording can run independently of temperature control. It
mirrors hardware_testing/heater_gui.py's schedule runner and safety cutoffs:
abort (peltier off) if the bath temperature reads NaN for 15 consecutive samples
or leaves the [2, 60] °C window.

The loop lives here (on the Pi, next to the hardware) rather than on the remote
monitor so that a dropped network link can never strand the heater — the safety
supervision runs locally regardless of the tunnel/droplet.

Also works in simulation mode (no `Bioreactor`): it advances the schedule/PID in
software and reflects state into the API's sim_state dict so the UI can be
developed without a Pi. No data file is written in simulation.
"""
import os
import csv
import math
import time
import shutil
import random
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable

logger = logging.getLogger(__name__)

# Safety window for an unattended run — mirror heater_gui.py constants.
TEMP_MAX_C = 60.0
TEMP_MIN_C = 2.0
MAX_NAN_SAMPLES = 15
SAMPLE_PERIOD_S = 1.0
DEFAULT_GAINS = {'kp': 12.0, 'ki': 0.015, 'kd': 0.0}

# Serializes all bioreactor hardware access (I2C / GPIO) between the control-loop
# thread and FastAPI request threads, which otherwise hit the same bus with no
# coordination. Re-entrant so a single tick can nest peltier + sensor calls.
# main.py acquires this around its hardware reads/writes too.
HARDWARE_LOCK = threading.RLock()


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN/Inf) with None so a value is safe for
    Starlette's JSONResponse (it serializes with allow_nan=False and 500s on NaN).
    self.last defaults absent sensors (e.g. ambient_temp / peltier_current on a rig that
    lacks them) to float('nan'), so status() would 500 during a run without this.
    Rebuilds containers, leaving the source (self.last) untouched for the internal
    NaN-based safety logic."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# --- Data-file retention + disk guard --------------------------------------
# Retention only ever touches files THIS engine creates (these suffixes) at the
# top level of the data dir — never the historical/committed data, the
# bioreactor's own files, or subdirectories.
RUN_FILE_SUFFIXES = ('_peltier_schedule.csv', '_pid_run.csv', '_program.csv', '_recording.csv')


class InsufficientStorageError(Exception):
    """Raised when there isn't enough free disk to safely start a run."""


def prune_run_files(data_dir, max_total_mb, keep):
    """Delete oldest API-generated run CSVs so their combined size stays under
    ``max_total_mb``, always keeping at least ``keep`` of the most recent.

    Scope is strictly top-level files ending in RUN_FILE_SUFFIXES. Returns the
    list of removed paths.
    """
    if not data_dir or not os.path.isdir(data_dir):
        return []
    entries = []
    for name in os.listdir(data_dir):
        if not name.endswith(RUN_FILE_SUFFIXES):
            continue
        p = os.path.join(data_dir, name)
        try:
            if os.path.isfile(p):
                entries.append((p, os.path.getmtime(p), os.path.getsize(p)))
        except OSError:
            continue
    entries.sort(key=lambda t: t[1], reverse=True)   # newest first
    max_bytes = max(0, max_total_mb) * 1024 * 1024
    # Always keep at least the single newest run file, even if it alone exceeds the
    # cap — never delete the most recent run to satisfy a size budget.
    keep = max(1, keep)
    total = 0
    cutoff = len(entries)          # index of the first file to delete (keep [:cutoff])
    for i, (_p, _m, size) in enumerate(entries):
        total += size
        if i >= keep and total > max_bytes:
            cutoff = i
            break
    removed = []
    for p, _m, _s in entries[cutoff:]:
        try:
            os.remove(p)
            removed.append(p)
            # also remove the program-JSON saved beside a _program.csv, if present
            if p.endswith('_program.csv'):
                sib = p[:-4] + '.json'
                if os.path.isfile(sib):
                    os.remove(sib)
        except OSError as e:
            logger.warning("could not prune %s: %s", p, e)
    return removed


def _free_mb(path):
    """Free megabytes on the filesystem holding ``path``, or None if unknown."""
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return None


class ScheduleError(ValueError):
    """Raised when an uploaded schedule fails to parse or validate."""


def parse_schedule(text: str, max_heat: float = 70.0, max_cool: float = 100.0) -> List[Dict[str, Any]]:
    """Parse a heater schedule CSV into a list of steps.

    Format (same as hardware_testing/peltier_schedule_example.csv): lines of
    ``duty,direction,hold_s`` with optional ``#`` comment lines and an optional
    ``duty,direction,hold_s`` header row. duty is 0-100 (capped per direction),
    direction is ``heat`` or ``cool``, hold_s > 0.

    Returns [{'duty': float, 'direction': str, 'hold_s': float}, ...].
    Raises ScheduleError on any malformed/out-of-range row.
    """
    steps: List[Dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split(',')]
        if parts and parts[0].lower() == 'duty':
            continue  # header row
        if len(parts) < 3:
            raise ScheduleError(f"line {lineno}: expected 'duty,direction,hold_s', got {raw!r}")
        try:
            duty = float(parts[0])
            direction = parts[1].lower()
            hold_s = float(parts[2])
        except ValueError:
            raise ScheduleError(f"line {lineno}: could not parse numbers in {raw!r}")
        if direction not in ('heat', 'cool'):
            raise ScheduleError(f"line {lineno}: direction must be 'heat' or 'cool', got {parts[1]!r}")
        if hold_s <= 0:
            raise ScheduleError(f"line {lineno}: hold_s must be > 0, got {hold_s}")
        limit = max_heat if direction == 'heat' else max_cool
        if not (0.0 <= duty <= limit):
            raise ScheduleError(f"line {lineno}: duty {duty} out of range [0, {limit}] for '{direction}'")
        steps.append({'duty': duty, 'direction': direction, 'hold_s': hold_s})
    if not steps:
        raise ScheduleError("no schedule steps found")
    return steps


class RunController:
    """Single background control loop for schedule / PID / program runs.

    Thread-safe: `start_*`, `stop`, and `status` may be called from FastAPI
    request threads; the control loop runs on its own daemon thread. All shared
    state is guarded by a re-entrant lock.
    """

    def __init__(self):
        self._lock = threading.RLock()          # guards run state
        self._lifecycle = threading.Lock()      # serializes start/stop transitions
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        # Dependencies (set via configure()).
        self._bio = None
        self._sim = True
        self._sim_state: Optional[dict] = None
        self._io = None                       # bioreactor_v3.src.io module
        self._pid: Optional[Callable] = None  # temperature_pid_controller
        self._measure: Optional[Callable] = None  # measure_and_record_sensors
        self._data_dir: Optional[str] = None
        self._max_heat = 70.0
        self._max_cool = 100.0
        self._retention_max_mb = 1000    # cap total size of API run files
        self._retention_keep = 10        # always keep at least this many newest runs
        self._min_free_mb = 500          # refuse to start a run below this free space
        self._od_power_fn = None         # callable -> live IR LED % for the per-tick OD read
        self._od_latest_fn = None        # callable -> cached OD dict (from the OD sampler)
        self._gas_latest_fn = None       # callable -> cached {'co2','o2'} (from the gas sampler)
        self._ring_apply_fn = None       # callable(color) -> apply program ring command (+ shadow)
        self._stirrer_apply_fn = None    # callable(duty)  -> apply program stirrer command
        self._pump_apply_fn = None       # callable(interval_s, duty) -> set pump dosing regime
        self._pump_stop_fn = None        # callable() -> stop pump dosing
        self._relay_apply_fn = None      # callable(name, 'open'|'closed') -> set a relay
        self._od_apply_fn = None         # callable(power, enabled) -> set OD sampler config
        self._co2_apply_fn = None
        self._co2_stop_fn = None
        self._co2_status_fn = None

        self._recording = False
        self._record_owner = None
        self._record_path = None
        self._record_started = None
        self._record_error = None
        self._record_evt = None
        self._record_thread = None
        self._reset_state()

    # ------------------------------------------------------------------ setup
    def configure(self, *, bio, sim, sim_state, io_module, pid_func, measure_func,
                  data_dir, max_heat, max_cool,
                  retention_max_mb=1000, retention_keep=10, min_free_mb=500,
                  od_power_fn=None, od_latest_fn=None, gas_latest_fn=None,
                  ring_apply_fn=None, stirrer_apply_fn=None,
                  pump_apply_fn=None, pump_stop_fn=None, relay_apply_fn=None,
                  od_apply_fn=None, co2_apply_fn=None, co2_stop_fn=None, co2_status_fn=None):
        with self._lock:
            self._bio = bio
            self._sim = sim
            self._sim_state = sim_state
            self._io = io_module
            self._pid = pid_func
            self._measure = measure_func
            self._data_dir = data_dir
            self._max_heat = max_heat
            self._max_cool = max_cool
            self._retention_max_mb = retention_max_mb
            self._retention_keep = retention_keep
            self._min_free_mb = min_free_mb
            self._od_power_fn = od_power_fn
            self._od_latest_fn = od_latest_fn
            self._gas_latest_fn = gas_latest_fn
            self._ring_apply_fn = ring_apply_fn
            self._stirrer_apply_fn = stirrer_apply_fn
            self._pump_apply_fn = pump_apply_fn
            self._pump_stop_fn = pump_stop_fn
            self._relay_apply_fn = relay_apply_fn
            self._od_apply_fn = od_apply_fn
            self._co2_apply_fn = co2_apply_fn
            self._co2_stop_fn = co2_stop_fn
            self._co2_status_fn = co2_status_fn

    def prune(self):
        """Prune old run files now (e.g. on startup). No-op in simulation."""
        if self._sim or not self._data_dir or self._recording:
            return
        try:
            removed = prune_run_files(self._data_dir, self._retention_max_mb, self._retention_keep)
            if removed:
                logger.info("Startup: pruned %d old run file(s)", len(removed))
        except Exception as e:
            logger.warning("Startup pruning failed: %s", e)

    def _prepare_storage(self):
        """Free space + verify headroom before a run. Real mode only.

        Prunes old run files, then raises InsufficientStorageError if free disk
        is still below the configured floor. Called before the run is marked
        active, so a full disk cleanly refuses the run instead of half-starting.
        """
        if self._sim or not self._data_dir or self._recording:
            return
        try:
            os.makedirs(self._data_dir, exist_ok=True)
        except OSError:
            pass
        try:
            removed = prune_run_files(self._data_dir, self._retention_max_mb, self._retention_keep)
            if removed:
                logger.info("Pruned %d old run file(s) to stay under %d MB",
                            len(removed), self._retention_max_mb)
        except Exception as e:
            logger.warning("Run-file pruning failed: %s", e)
        free = _free_mb(self._data_dir)
        if free is not None and free < self._min_free_mb:
            raise InsufficientStorageError(
                f"only {free:.0f} MB free at the data directory; "
                f"need at least {self._min_free_mb} MB to start a run")

    @property
    def max_heat(self) -> float:
        return self._max_heat

    @property
    def max_cool(self) -> float:
        return self._max_cool

    def _reset_state(self):
        self.mode = 'idle'            # 'idle' | 'schedule' | 'pid' | 'program'
        self.active = False
        self.steps: Optional[List[Dict[str, Any]]] = None
        self.step_idx = -1
        self.seg_end: Optional[float] = None
        self.run_t0: Optional[float] = None
        self.setpoint: Optional[float] = None
        self.gains: Optional[Dict[str, float]] = None
        self.data_file: Optional[str] = None
        self.completed = False
        self.aborted = False
        self.abort_reason: Optional[str] = None
        self.nan_count = 0
        self.tick_errors = 0
        self.last: Dict[str, Any] = {}
        # multi-track program state
        self.program = None                        # program.Program | None
        self._program_json = None                  # raw program JSON (saved beside the run CSV)
        self.program_end: Optional[float] = None   # run_t0 + duration (None = until tracks done)
        self._track_state: List[Dict[str, Any]] = []  # per-track {idx, seg_end, state, step}
        self._overrides: set = set()               # devices manually overridden this segment
        self._applied: Dict[str, Any] = {}          # last command applied per device (for status)

    # ---------------------------------------------------------------- control
    def start_schedule(self, steps: List[Dict[str, Any]]):
        with self._lifecycle:
            with self._lock:
                if self.active:
                    raise RuntimeError("a run is already active")
            # prune + disk guard before marking active (may raise InsufficientStorageError)
            self._prepare_storage()
            with self._lock:
                self._reset_state()
                self.mode = 'schedule'
                self.steps = steps
                self.step_idx = -1
                self.seg_end = None
                self._begin()

    def start_pid(self, setpoint: float, kp: float, ki: float, kd: float):
        with self._lifecycle:
            with self._lock:
                if self.active:
                    raise RuntimeError("a run is already active")
            with self._lock:
                self._reset_state()
                self.mode = 'pid'
                self.setpoint = float(setpoint)
                self.gains = {'kp': float(kp), 'ki': float(ki), 'kd': float(kd)}
                # Clear any PID integrator state left on the bioreactor from a prior run.
                if self._bio is not None:
                    for attr in ('_temp_integral', '_temp_last_error',
                                 '_temp_last_time', '_temp_last_derivative'):
                        if hasattr(self._bio, attr):
                            delattr(self._bio, attr)
                self._begin()

    def start_program(self, program, gains: Optional[Dict[str, float]] = None, raw_json=None):
        """Start a multi-track program (see program.Program). Runs all tracks in
        parallel; each applies its command once per step boundary. `raw_json` (the
        uploaded program text) is saved beside the run CSV for reproducibility."""
        with self._lifecycle:
            with self._lock:
                if self.active:
                    raise RuntimeError("a run is already active")
            self._prepare_storage()
            with self._lock:
                self._reset_state()
                self.mode = 'program'
                self.program = program
                self._program_json = raw_json
                self.gains = {**DEFAULT_GAINS, **(gains or {})}
                self._track_state = [
                    {'idx': -1, 'seg_end': None, 'state': 'run', 'step': None}
                    for _ in program.tracks
                ]
                self._overrides = set()
                self._applied = {}
                if self._bio is not None:   # clear any stale PID integrator state
                    for attr in ('_temp_integral', '_temp_last_error',
                                 '_temp_last_time', '_temp_last_derivative'):
                        if hasattr(self._bio, attr):
                            delattr(self._bio, attr)
                self._begin()

    def note_override(self, device: str):
        """Called when a device is manually set via the API during a program run, so
        the schedule leaves it alone until that track's next step reclaims it (and, for
        the peltier, the PID is suspended until then)."""
        with self._lock:
            if self.active and self.mode == 'program':
                self._overrides.add(device)

    def _begin(self):
        """Start the loop. Caller must hold the lock."""
        # Open the data file first: if it fails (e.g. disk full), we haven't yet
        # marked the run active, so the controller isn't left wedged.
        if self.mode != 'pid' and not self._recording:
            self._open_data_file(owner='run')
        if self._recording:
            self.data_file = self._record_path
        self.active = True
        self.run_t0 = time.time()
        if self.mode == 'program' and self.program is not None and self.program.duration_s is not None:
            self.program_end = self.run_t0 + self.program.duration_s
        self.nan_count = 0
        self.completed = False
        self.aborted = False
        self.abort_reason = None
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="run-control")
        self._thread.start()
        logger.info("Run started: mode=%s", self.mode)

    def stop(self, reason: Optional[str] = None) -> dict:
        """Stop any active run and turn the peltier off. Safe to call when idle.

        Held under _lifecycle for the whole teardown (including the thread join)
        so a concurrent start_* cannot open a new run/data-file underneath it.
        The join is outside _lock, so it can't deadlock with the control thread's
        own _lock acquisition in _tick.
        """
        with self._lifecycle:
            with self._lock:
                was_active = self.active
                self.active = False
                if reason and was_active:
                    self.abort_reason = reason
                thread = self._thread
            self._stop_evt.set()
            if thread and thread.is_alive() and threading.current_thread() is not thread:
                thread.join(timeout=3.0)
            self._all_off()
            self._stop_program_co2()
            if self.mode == 'program' and self._pump_stop_fn:
                try:
                    self._pump_stop_fn()   # a program owns the pumps; stopping it stops dosing
                except Exception as e:
                    logger.error("pump stop on run stop failed: %s", e)
            with self._lock:
                if self._record_owner == 'run':
                    self._close_data_file()
            if was_active:
                logger.info("Run stopped%s", f" ({reason})" if reason else "")
        return self.status()

    # ---------------------------------------------------------------- data IO
    def recording_status(self):
        with self._lock:
            return {'active': self._recording,
                    'owner': self._record_owner,
                    'data_file': os.path.basename(self._record_path) if self._record_path else None,
                    'elapsed_s': round(time.monotonic() - self._record_started, 1)
                        if self._recording else None,
                    'error': self._record_error, 'simulation': self._sim}

    def start_recording(self):
        with self._lifecycle, self._lock:
            if not self._recording:
                self._prepare_storage()
                self._open_data_file(owner='manual')
            return self.recording_status()

    def stop_recording(self):
        with self._lifecycle:
            with self._lock:
                thread = self._record_thread
                self._close_data_file()
            if thread and thread is not threading.current_thread():
                thread.join(timeout=3)
            return self.recording_status()

    def _open_data_file(self, owner):
        # Caller holds _lock. Microseconds + exclusive create prevent overwriting
        # a previous recording when Start/Stop is clicked within the same second.
        path = None
        if not self._sim:
            os.makedirs(self._data_dir, exist_ok=True)
            ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            suffix = 'recording' if owner == 'manual' else {
                'schedule': 'peltier_schedule', 'program': 'program'}[self.mode]
            path = os.path.join(self._data_dir, f"{ts}_{suffix}.csv")
            f = open(path, 'x', newline='')
            try:
                writer = csv.DictWriter(f, fieldnames=self._bio.fieldnames)
                writer.writeheader()
                f.flush()
            except Exception:
                f.close()
                os.remove(path)
                raise
            self._bio.out_file = f
            self._bio.out_file_path = path
            self._bio.writer = writer
            self._bio.csv_write_error = None
        self._record_path = path
        self.data_file = path
        self._record_started = time.monotonic()
        self._record_error = None
        self._record_owner = owner
        self._recording = True
        # The control loop records its own samples while active; this thread
        # fills in while idle, under the same lock, with no concurrent reads.
        event = threading.Event()
        self._record_evt = event
        self._record_thread = threading.Thread(target=self._record_loop, args=(event,),
                                               daemon=True, name='csv-recording')
        self._record_thread.start()
        if path and owner == 'run' and self.mode == 'program' and self._program_json:
            try:
                with open(path[:-4] + '.json', 'w') as jf:
                    jf.write(self._program_json)
            except Exception as e:
                logger.warning("could not save program JSON beside CSV: %s", e)

    def _close_data_file(self, error=None):
        if self._record_evt:
            self._record_evt.set()
        self._recording = False
        self._record_owner = None
        if error:
            self._record_error = str(error)
            logger.error("CSV recording stopped: %s", error)
        if self._bio is not None:
            f = getattr(self._bio, 'out_file', None)
            try:
                if f is not None:
                    f.close()
            except Exception as e:
                self._record_error = str(e)
            finally:
                self._bio.writer = None
                self._bio.out_file = None

    def _record_loop(self, event):
        while not event.is_set():
            tick_start = time.monotonic()
            with self._lock:
                # Check this session's event again after acquiring the lock:
                # a rapid restart must never resurrect an old recorder thread.
                if event.is_set():
                    return
                if not self.active and not self._sim:
                    try:
                        self._read_sample()
                    except Exception as e:
                        self._close_data_file(error=e)
            event.wait(max(0.0, SAMPLE_PERIOD_S - (time.monotonic() - tick_start)))

    def _read_sample(self):
        elapsed = (time.monotonic() - self._record_started if self._recording
                   else time.time() - self.run_t0)
        od_cache = self._od_latest_fn() if self._od_latest_fn else None
        gas_cache = (self._gas_latest_fn() if self._gas_latest_fn else None) or {}
        led_power = self._od_power_fn() if self._od_power_fn else 10.0
        if self._recording:
            free = _free_mb(self._data_dir)
            if free is not None and free < self._min_free_mb:
                reason = f"low disk space ({free:.0f} MB free)"
                if self._record_owner == 'run':
                    self._finish(completed=False, abort=reason)
                self._close_data_file(error=reason)
        with HARDWARE_LOCK:
            data = self._measure(self._bio, elapsed=elapsed, led_power=led_power,
                                 od_override=od_cache,
                                 co2_override=gas_cache.get('co2'),
                                 o2_override=gas_cache.get('o2'), use_cached=True)
        if self._recording and getattr(self._bio, 'csv_write_error', None):
            self._close_data_file(error=self._bio.csv_write_error)
        return data

    # ------------------------------------------------------------- actuation
    def _apply_peltier(self, duty: float, direction: str):
        if self._sim:
            self._sim_state['peltier_duty'] = duty
            self._sim_state['peltier_direction'] = direction
            return
        with HARDWARE_LOCK:
            if duty <= 0:
                self._io.stop_peltier(self._bio)
            else:
                self._io.set_peltier_power(self._bio, duty, forward=direction)

    def _all_off(self):
        try:
            if self._sim:
                if self._sim_state is not None:
                    self._sim_state['peltier_duty'] = 0.0
            elif self._io is not None and self._bio is not None:
                with HARDWARE_LOCK:
                    self._io.stop_peltier(self._bio)
        except Exception as e:
            logger.error("Failed to stop peltier: %s", e)

    # ----------------------------------------------------------------- loop
    def _run(self):
        while not self._stop_evt.is_set():
            tick_start = time.time()
            try:
                self._tick()
                self.tick_errors = 0
            except Exception as e:
                # Fail safe: an unexpected error must not leave the peltier driven.
                # Cut power immediately, and abort the run if errors persist so a
                # wedged bus can't hold the heater on while the loop retries.
                logger.error("Run control tick error: %s", e, exc_info=True)
                self._all_off()
                self._stop_program_co2()
                self.tick_errors += 1
                if self.tick_errors >= MAX_NAN_SAMPLES:
                    with self._lock:
                        self._finish(completed=False,
                                     abort=f"{self.tick_errors} consecutive control-loop errors")
            if not self.active:
                break
            self._stop_evt.wait(max(0.0, SAMPLE_PERIOD_S - (time.time() - tick_start)))

    def _tick(self):
        with self._lock:
            if not self.active:
                return
            if self.mode == 'schedule':
                self._advance_schedule()
                if not self.active:   # schedule just completed
                    return
                self._sample_and_supervise()
            elif self.mode == 'pid':
                # One temperature read per tick: _sample_and_supervise reads + runs
                # the safety window/NaN checks, then (if still active) the PID drives
                # the peltier from that SAME reading — never a second, independent read
                # that the supervisor didn't see.
                self._sample_and_supervise()
                if self.active and not self._sim:
                    temp = self.last.get('temperature')
                    with HARDWARE_LOCK:
                        self._pid(self._bio, setpoint=self.setpoint,
                                  current_temp=temp, **self.gains)
            elif self.mode == 'program':
                self._program_tick()

    # -------------------------------------------------------------- program mode
    def _program_tick(self):
        now = time.time()
        if self.program_end is not None and now >= self.program_end:
            self._finish(completed=True)
            return
        # 1. advance every track; apply commands at step boundaries (ring/stirrer/heater
        #    are set once here; temp just sets self.setpoint for the per-tick PID below)
        for i in range(len(self._track_state)):
            self._advance_track(i, now)
            if not self.active:
                return
        if self._co2_status_fn and any(tr.device == 'relay:CO2' for tr in self.program.tracks):
            co2 = self._co2_status_fn()
            if co2.get('owner') == 'program' and co2.get('fault'):
                self._finish(completed=False, abort='CO2 controller: '+co2['fault'])
                return
        # 2. whole-program end (duration reached, or every track exhausted)
        if self._program_finished(now):
            self._finish(completed=True)
            return
        # 3. read temp + log the CSV row + safety checks (sets self.last)
        self._sample_and_supervise()
        if not self.active:
            return
        # 4. drive the peltier from the current temp step's PID, unless it's open-loop
        #    (heater step / no peltier track) or manually overridden this segment
        if self.setpoint is not None and 'peltier' not in self._overrides and not self._sim:
            temp = self.last.get('temperature')
            if temp is not None and temp == temp:   # not NaN
                with HARDWARE_LOCK:
                    self._pid(self._bio, setpoint=self.setpoint, current_temp=temp, **self.gains)

    def _advance_track(self, i: int, now: float):
        ts = self._track_state[i]
        if ts['state'] in ('done', 'hold'):
            return  # 'done' = finished; 'hold' = open-ended step, stays active forever
        if ts['seg_end'] is not None and now < ts['seg_end']:
            return  # still holding the current step
        track = self.program.tracks[i]
        ts['idx'] += 1
        if ts['idx'] >= len(track.steps):
            if track.repeat:
                ts['idx'] = 0
            else:
                ts['state'] = 'done'          # finished its last finite step
                self._end_track_device(track.device)
                return
        step = track.steps[ts['idx']]
        ts['step'] = step
        self._apply_step(step)
        self._overrides.discard(step.device)  # schedule reclaims the device
        if step.duration_s is None:
            ts['seg_end'] = None
            ts['state'] = 'hold'              # hold-to-end: stays active (PID keeps running)
        else:
            ts['seg_end'] = now + step.duration_s
            ts['state'] = 'run'

    def _apply_step(self, step):
        self._applied[step.device] = {'command': step.command, 'value': step.value}
        if step.command == 'temp':
            self.setpoint = float(step.value)     # PID drives toward this each tick
            return
        self.setpoint = None if step.device == 'peltier' else self.setpoint
        if step.command == 'heater':
            v = float(step.value)
            self._apply_peltier(abs(v), 'heat' if v >= 0 else 'cool')
        elif step.command == 'ring':
            color = tuple(int(x) for x in step.value)
            if self._ring_apply_fn:
                self._ring_apply_fn(color)          # handles sim/real + /api/state shadow
            elif self._sim:
                self._sim_state['ring'] = color
            elif self._io is not None:
                with HARDWARE_LOCK:
                    self._io.set_ring_light(self._bio, color)
        elif step.command == 'stirrer':
            duty = float(step.value)
            if self._stirrer_apply_fn:
                self._stirrer_apply_fn(duty)
            elif self._sim:
                self._sim_state['stirrer'] = duty
            elif self._io is not None:
                with HARDWARE_LOCK:
                    self._io.set_stirrer_speed(self._bio, duty)
        elif step.command == 'pump':
            v = step.value                        # {'duty': 0-100, 'interval': seconds, 'rate'?: ml/s}
            if self._pump_apply_fn:
                self._pump_apply_fn(v['interval'], v['duty'], v.get('rate'))
        elif step.command == 'relay':
            v = step.value                        # {'name': <relay>, 'state': 'open'|'closed'}
            if self._relay_apply_fn:
                self._relay_apply_fn(v['name'], v['state'])
        elif step.command == 'od':
            v = step.value                        # {'power': 0-100, 'enabled'?: bool}
            if self._od_apply_fn:
                self._od_apply_fn(v['power'], v.get('enabled'))
        elif step.command == 'co2':
            if not self._co2_apply_fn:
                raise RuntimeError('CO2 controller is unavailable')
            try:
                remaining = max(0.0, self.program_end-time.time()) if self.program_end is not None else None
                if step.value is not False and remaining is not None and remaining <= 0:
                    return
                self._co2_apply_fn(step.value, duration_s=remaining)
            except (ValueError, RuntimeError) as e:
                self._finish(completed=False, abort=f'CO2 control could not start: {e}')

    def _stop_program_co2(self):
        if self.mode == 'program' and self._co2_stop_fn:
            self._co2_stop_fn()

    def _end_track_device(self, device: str):
        # A non-repeating track ran out of steps: release the device. The peltier is
        # turned OFF for safety and pumps stop dosing; ring/stirrer keep their last value.
        if device == 'peltier':
            self.setpoint = None
            self._all_off()
        elif device == 'pump' and self._pump_stop_fn:
            self._pump_stop_fn()
        elif device == 'relay:CO2':
            self._stop_program_co2()

    def _program_finished(self, now: float) -> bool:
        if self.program_end is not None:
            return now >= self.program_end
        return all(ts['state'] == 'done' for ts in self._track_state)

    def _advance_schedule(self):
        now = time.time()
        if self.seg_end is not None and now < self.seg_end:
            return  # still holding the current step
        self.step_idx += 1
        if self.step_idx >= len(self.steps):
            self._finish(completed=True)
            return
        step = self.steps[self.step_idx]
        self._apply_peltier(step['duty'], step['direction'])
        self.seg_end = now + step['hold_s']

    def _sample_and_supervise(self):
        if self._sim:
            duty = float(self._sim_state.get('peltier_duty', 0.0))
            direction = self._sim_state.get('peltier_direction', 'heat')
            temp = round(24.0 + random.uniform(-0.3, 0.3), 3)
            ambient = round(22.0 + random.uniform(-0.3, 0.3), 3)
            # unsigned supply current, signed negative for heating (matches GUI convention)
            current = round((duty / 100.0) * 5.0 * (1 if direction == 'cool' else -1), 3)
            self.last = {'temperature': temp, 'ambient_temp': ambient,
                         'peltier_current': current, 'peltier_duty': duty,
                         'direction': direction}
            return

        try:
            data = self._read_sample()
        except Exception as e:
            logger.error("measure_and_record_sensors failed: %s", e)
            data = {}

        temp = data.get('temperature', float('nan'))
        ambient = data.get('ambient_temp', float('nan'))
        raw_current = data.get('peltier_current', float('nan'))
        forward = data.get('peltier_forward', 1.0)
        # INA228 reads unsigned; sign negative when heating (forward == 0), per GUI.
        current = raw_current if (forward != forward or forward) else -raw_current
        self.last = {
            'temperature': temp,
            'ambient_temp': ambient,
            'peltier_current': current,
            'peltier_duty': data.get('peltier_duty', float('nan')),
            'direction': 'cool' if forward else 'heat',
        }

        # Safety supervision (mirror heater_gui._tick)
        if temp != temp:  # NaN
            self.nan_count += 1
            if self.nan_count >= MAX_NAN_SAMPLES:
                self._finish(completed=False,
                             abort=f"no valid bath temperature for {self.nan_count} samples")
        else:
            self.nan_count = 0
            if temp > TEMP_MAX_C or temp < TEMP_MIN_C:
                self._finish(completed=False,
                             abort=f"bath {temp:.1f} °C outside [{TEMP_MIN_C:.0f}, {TEMP_MAX_C:.0f}] °C")

    def _finish(self, completed: bool, abort: Optional[str] = None):
        """End the run from inside the control thread. Caller holds the lock."""
        self.active = False
        self.completed = completed
        if abort:
            self.aborted = True
            self.abort_reason = abort
            logger.warning("Run aborted: %s", abort)
        self._stop_evt.set()
        self._all_off()
        self._stop_program_co2()
        if self.mode == 'program' and self._pump_stop_fn:
            try:
                self._pump_stop_fn()
            except Exception as e:
                logger.error("pump stop on finish failed: %s", e)
        if self._record_owner == 'run':
            self._close_data_file()

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        with self._lock:
            st: Dict[str, Any] = {
                'active': self.active,
                'mode': self.mode,
                'completed': self.completed,
                'aborted': self.aborted,
                'abort_reason': self.abort_reason,
                'data_file': os.path.basename(self.data_file) if self.data_file else None,
                'last': self.last,
            }
            if self.run_t0 is not None:
                st['elapsed_s'] = round(time.time() - self.run_t0, 1)
            if self.mode == 'schedule' and self.steps is not None:
                st['step'] = min(max(0, self.step_idx + 1), len(self.steps))
                st['total_steps'] = len(self.steps)
                if 0 <= self.step_idx < len(self.steps):
                    st['current_step'] = self.steps[self.step_idx]
                st['remaining_steps'] = max(0, len(self.steps) - self.step_idx - 1)
                if self.seg_end is not None and self.active:
                    st['step_remaining_s'] = round(self.seg_end - time.time(), 1)
            if self.mode == 'pid':
                st['setpoint'] = self.setpoint
                st['gains'] = self.gains
            if self.mode == 'program' and self.program is not None:
                st['program_name'] = self.program.name
                st['setpoint'] = self.setpoint
                st['overrides'] = sorted(self._overrides)
                if self.program_end is not None and self.active:
                    st['remaining_s'] = round(self.program_end - time.time(), 1)
                tracks = []
                for i, ts in enumerate(self._track_state):
                    tr = self.program.tracks[i]
                    step = ts.get('step')
                    tracks.append({
                        'name': tr.name, 'device': tr.device, 'state': ts['state'],
                        'repeat': tr.repeat, 'total_steps': len(tr.steps),
                        'step': (ts['idx'] % len(tr.steps)) + 1 if ts['idx'] >= 0 else 0,
                        'current': ({'command': step.command, 'value': step.value} if step else None),
                        'step_remaining_s': (round(ts['seg_end'] - time.time(), 1)
                                             if ts['seg_end'] is not None and self.active else None),
                    })
                st['tracks'] = tracks
            return _json_safe(st)


# Module-level singleton used by main.py
runner = RunController()
