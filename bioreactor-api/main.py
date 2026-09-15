"""
Bioreactor API — minimal FastAPI server wrapping bioreactor_v3 hardware.

Start with: HARDWARE_MODE=simulation uvicorn main:app --port 9000
Interactive docs: http://localhost:9000/docs
"""
import os
import sys
import math
import random
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import camera
from auth import verify_token, limiter, RATE_LIMIT
from control import (runner, parse_schedule, ScheduleError, HARDWARE_LOCK,
                     InsufficientStorageError, TEMP_MIN_C, TEMP_MAX_C)
from program import parse_program, ProgramError, expand_tracks
from history import history
from od_sampler import od_sampler
from gas_sampler import gas_sampler
from pump_controller import pump_controller
from relay_controller import relay_controller, RelaySafetyError

# Rolling sensor-history: legacy single-file buffer (migrated once on first boot of
# the daily-archive version) + the daily-archive directory (history/YYYY-MM-DD.jsonl).
HISTORY_FILE = Path(__file__).parent / 'sensor_history.json'
# Archive dir is env-overridable so a simulation/test instance can point at a scratch
# path and never append fake points to the production archive (set BIOREACTOR_HISTORY_DIR).
HISTORY_DIR = Path(os.getenv('BIOREACTOR_HISTORY_DIR') or (Path(__file__).parent / 'history'))

# Directory where the bioreactor writes its data CSVs (run files live here).
DATA_DIR = Path(__file__).parent / 'bioreactor_v3' / 'src' / 'bioreactor_data'

# Add bioreactor_v3 parent to path so we can import bioreactor_v3.src.*
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
co2_controller = None
bioreactor = None  # Bioreactor instance (None in simulation mode)
simulation_mode = True
initialized_components: Dict[str, bool] = {}

# Optical density. The sampler always reads every AVAILABLE source (od + eyespy);
# Optical plan (bioreactor_v3/src/optics.py): named VOLTAGE SOURCES (ADS1115 channels /
# eyespy boards) and the canonical OD MEASUREMENTS OD_45/OD_ref/OD_90/OD_135 mapped onto
# them. `optics` is the resolved OpticalPlan; `od_available` is True when at least one
# enabled OD measurement's hardware component is up. The OD sampler's cache is keyed by
# SOURCE name; /api/state maps it onto OD measurement names.
optics = None
od_available = False

# Last commanded LED power (the driver doesn't report it back, so we shadow it here).
led_power = 0.0

# Last commanded ring-light colour, shadowed so /api/state can surface it for the
# always-visible "ring" status card without a per-poll hardware read.
ring_color = {'red': 0, 'green': 0, 'blue': 0}


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

# -- LED
class LEDControlRequest(BaseModel):
    power: float = Field(ge=0, le=100, description="LED power 0-100%")

class LEDState(BaseModel):
    status: str
    power: float
    active: bool

# -- Peltier
class PeltierControlRequest(BaseModel):
    duty_cycle: float = Field(ge=0, le=100, description="PWM duty cycle 0-100%")
    direction: str = Field(pattern="^(heat|cool|forward|reverse)$", description="heat/cool/forward/reverse")

class PeltierState(BaseModel):
    status: str
    duty_cycle: float
    direction: str
    active: bool

# -- Stirrer
class StirrerControlRequest(BaseModel):
    duty_cycle: float = Field(ge=0, le=100, description="PWM duty cycle 0-100%")

class StirrerState(BaseModel):
    status: str
    duty_cycle: float
    active: bool

# -- Ring Light
class RingLightControlRequest(BaseModel):
    red: int = Field(ge=0, le=255)
    green: int = Field(ge=0, le=255)
    blue: int = Field(ge=0, le=255)
    pixel_index: Optional[int] = Field(None, ge=0, description="Specific pixel or None for all")

class RingLightState(BaseModel):
    status: str
    red: int
    green: int
    blue: int
    active: bool

# -- Pumps
class PumpControlRequest(BaseModel):
    pump_name: str = Field(description="Pump identifier (e.g. 'inflow', 'outflow')")
    velocity: float = Field(description="Flow rate in mL/s (positive=forward, negative=reverse)")

class PumpState(BaseModel):
    status: str
    pump_name: str
    velocity: float
    active: bool

class PumpRunRequest(BaseModel):
    duration: float = Field(gt=0, description="Cycle interval in seconds")
    duty_cycle: float = Field(ge=0, le=100, description="Duty cycle 0-100% (fraction of the interval to pump)")
    flow_rate: Optional[float] = Field(default=None, ge=0, description="Flow rate ml/s while pumping; omit to keep the current/default")

# -- Relays
class RelayControlRequest(BaseModel):
    relay_name: str = Field(description="Relay identifier (name from config.RELAYS)")
    command: str = Field(description="open | closed | toggle")

class RelayTimedRequest(BaseModel):
    relay_name: str = Field(description="Relay identifier")
    command: str = Field(description="open | closed | toggle — run now, then toggle after duration")
    duration: float = Field(gt=0, description="seconds to wait before the toggle")

class RelayState(BaseModel):
    status: str
    states: Dict[str, str]                    # name -> 'open' | 'closed'
    pending: Dict[str, float] = {}            # name -> seconds left on a timed toggle
    guards: Dict[str, Any] = {}               # name -> safety limits + cooldown (guarded relays)

# -- Sensors (response only)
class TemperatureState(BaseModel):
    status: str
    temperature: Optional[float]
    unit: str = "celsius"

class ODState(BaseModel):
    status: str
    voltages: list
    names: list = []
    unit: str = "volts"

class EyespyState(BaseModel):
    status: str
    voltages: list
    names: list = []
    unit: str = "volts"

class AmbientTempState(BaseModel):
    status: str
    temperature: Optional[float]
    unit: str = "celsius"

class PeltierCurrentState(BaseModel):
    status: str
    current: Optional[float]
    unit: str = "amps"

# -- PID run
class PIDRequest(BaseModel):
    setpoint: float = Field(description="Target bath temperature (°C)")
    kp: float = Field(12.0, description="Proportional gain")
    ki: float = Field(0.015, description="Integral gain")
    kd: float = Field(0.0, description="Derivative gain")


# ---------------------------------------------------------------------------
# Simulation state (tracks what actuators are "set to" in sim mode)
# ---------------------------------------------------------------------------
sim_state = {
    'led_power': 0.0,
    'peltier_duty': 0.0,
    'peltier_direction': 'forward',
    'stirrer_duty': 0.0,
    'ring_r': 0, 'ring_g': 0, 'ring_b': 0,
    'pump_name': '', 'pump_velocity': 0.0,
    'pump_velocities': {'inflow': 0.0, 'outflow': 0.0},
    'relays': {},
}


# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bioreactor, simulation_mode, initialized_components

    hardware_mode = os.getenv("HARDWARE_MODE", "simulation")
    simulation_mode = (hardware_mode != "real")

    logger.info(f"Hardware mode: {hardware_mode}")

    if not simulation_mode:
        try:
            from bioreactor_v3.src.bioreactor import Bioreactor
            from bioreactor_v3.src import io as bio_io
            from bioreactor_v3.src.utils import (
                temperature_pid_controller, measure_and_record_sensors,
            )
            from config import Config
            config = Config()
            bioreactor = Bioreactor(config)
            initialized_components = dict(bioreactor._initialized)
            logger.info(f"Hardware initialized: {initialized_components}")
            # The bioreactor opens a startup data file (header only). The run
            # engine manages its own per-run files, so release + remove the stray
            # startup CSV now (nothing has written to it yet) so it can't linger as
            # the "latest" download.
            try:
                startup_path = getattr(bioreactor, 'out_file_path', None)
                if getattr(bioreactor, 'out_file', None) is not None:
                    bioreactor.out_file.close()
                bioreactor.writer = None
                bioreactor.out_file = None
                if startup_path and os.path.exists(startup_path):
                    os.remove(startup_path)
            except Exception as e:
                logger.warning(f"Could not release startup data file: {e}")
            runner.configure(
                bio=bioreactor, sim=False, sim_state=None, io_module=bio_io,
                pid_func=temperature_pid_controller, measure_func=measure_and_record_sensors,
                data_dir=str(DATA_DIR),
                max_heat=getattr(config, 'PELTIER_MAX_DUTY_HEAT', 70.0),
                max_cool=getattr(config, 'PELTIER_MAX_DUTY_COOL', 100.0),
                retention_max_mb=getattr(config, 'DATA_RETENTION_MAX_MB', 1000),
                retention_keep=getattr(config, 'DATA_RETENTION_KEEP', 10),
                min_free_mb=getattr(config, 'DATA_MIN_FREE_MB', 500),
                od_power_fn=lambda: od_sampler.led_power,       # live dropdown value
                od_latest_fn=lambda: od_sampler.latest(),       # cached OD for run-CSV (fast tick)
                gas_latest_fn=lambda: gas_sampler.latest(),     # cached CO2/O2 for run-CSV
                ring_apply_fn=_program_apply_ring,              # program ring cmd -> strip + shadow
                stirrer_apply_fn=_program_apply_stirrer,        # program stirrer cmd
                pump_apply_fn=lambda interval, duty, rate=None: pump_controller.set_regime(interval, duty, rate),
                pump_stop_fn=pump_controller.off,
                relay_apply_fn=_program_apply_relay,
                co2_apply_fn=lambda target: co2_controller.program(target),
                co2_stop_fn=lambda: co2_controller.stop(owner="program") if co2_controller else None,
                co2_status_fn=lambda: co2_controller.status() if co2_controller else {},
                od_apply_fn=lambda power, enabled: od_sampler.set_config(led_power=power, enabled=enabled),
            )
            runner.prune()  # trim old run files on startup
        except Exception as e:
            # Deliberately fatal. HARDWARE_MODE=real must never silently degrade to
            # simulated readings: a run driven by fake sensor data is worse than no
            # run at all, and the failure would otherwise be invisible to anyone
            # reading /api/state. Individual components are still allowed to fail --
            # Bioreactor records those in _initialized and their endpoints return 503
            # -- so this only fires when the rig as a whole cannot come up.
            logger.error(f"Hardware init failed: {e}", exc_info=True)
            raise RuntimeError(
                "HARDWARE_MODE=real but hardware initialization failed; refusing to "
                "start in simulation mode. Fix the hardware/config, or set "
                "HARDWARE_MODE=simulation explicitly if you want mock data."
            ) from e
    else:
        logger.info("Simulation mode — no hardware")
        # In simulation, pretend all non-infrastructure components are initialized
        from config import Config
        config = Config()
        for name, enabled in config.INIT_COMPONENTS.items():
            if name != 'i2c' and enabled:
                initialized_components[name] = True

    if simulation_mode:
        runner.configure(
            bio=None, sim=True, sim_state=sim_state, io_module=None,
            pid_func=None, measure_func=None, data_dir=str(DATA_DIR),
            max_heat=70.0, max_cool=100.0,
            pump_apply_fn=lambda interval, duty, rate=None: pump_controller.set_regime(interval, duty, rate),
            pump_stop_fn=pump_controller.off,
            relay_apply_fn=_program_apply_relay,
            co2_apply_fn=lambda target: co2_controller.program(target),
            co2_stop_fn=lambda: co2_controller.stop(owner="program") if co2_controller else None,
            co2_status_fn=lambda: co2_controller.status() if co2_controller else {},
            od_apply_fn=lambda power, enabled: od_sampler.set_config(led_power=power, enabled=enabled),
        )

    # Optical-density sources available (from config.py via what actually initialized).
    global optics, od_available
    optics = bioreactor.optics if bioreactor is not None else _resolve_optics(config)
    for _e in optics.errors:
        logger.error("Optical config: %s", _e)
    for _w in optics.warnings:
        logger.warning("Optical config: %s", _w)
    od_available = any(initialized_components.get(optics.sources[s].component, False)
                       for s in optics.od.values())
    logger.info("Optical plan: %s; od_available=%s", _describe_optics(optics), od_available)

    # Seed the ring-light shadow from the driver's current colour (best-effort).
    global ring_color
    if not simulation_mode and bioreactor and initialized_components.get('ring_light'):
        try:
            from bioreactor_v3.src.io import get_ring_light_color
            with HARDWARE_LOCK:
                c = get_ring_light_color(bioreactor)
            if c:
                ring_color = {'red': int(c[0]), 'green': int(c[1]), 'blue': int(c[2])}
        except Exception as e:
            logger.warning("Could not read initial ring-light colour: %s", e)

    # IR-gated OD sampler: pulses the LED per reading (on -> settle -> read -> off).
    # Started before history so OD is available.
    # One LED pulse per SOURCE KIND (all ADS1115 channels together, all eyespy boards
    # together), interleaved when both kinds exist. The cache ({source: V}) feeds /api/state
    # (mapped onto OD measurements), the history buffer and the run CSV (od_override).
    src_groups = [(kind, [s.name for s in optics.sources_of(kind)
                          if initialized_components.get(s.component, False)])
                  for kind in ('adc', 'eyespy')]
    if any(names for _k, names in src_groups):
        od_ring_dodge = None
        if simulation_mode:
            od_set_led, od_read_fns = (lambda p: None), {}
        else:
            from bioreactor_v3.src.io import set_led as _od_set_led, read_voltage as _od_rv
            od_set_led = lambda p: _od_set_led(bioreactor, p)
            _read_src = lambda name: _od_rv(bioreactor, name)      # both kinds, by source name
            od_read_fns = {'adc': _read_src, 'eyespy': _read_src}
            # Dodge the ring around each OD read (off during the IR-on window, restored
            # after): keeps its light off the photodiodes and off through the IR-PWM
            # noisy window; the restore re-asserts the colour, correcting any glitch.
            if initialized_components.get('ring_light'):
                od_ring_dodge = _ring_dodge
        od_sampler.configure(
            hw_lock=HARDWARE_LOCK, set_led=od_set_led, read_fns=od_read_fns,
            sources=src_groups,
            sim=simulation_mode,
            enabled=getattr(config, 'OD_SAMPLE_ENABLED', True),
            led_power=getattr(config, 'OD_LED_POWER', 10.0),
            settle_s=getattr(config, 'OD_SETTLE_S', 0.5),
            post_read_s=getattr(config, 'OD_POST_READ_S', 0.1),
            period_s=getattr(config, 'OD_PULSE_PERIOD_S', 1.0),
            ring_dodge=od_ring_dodge,
        )
        od_sampler.start()

    # CO2 + O2 gas sampler: polled in the background and cached for /api/state +
    # history so the poll path stays fast (an Atlas read alone takes ~1.5s).
    gas_sensors = []
    _gas_delay = int(getattr(config, 'GAS_READ_DELAY_MS', 1500))
    for _name, _comp, _cfg_attr, _cast in (
        ('co2', 'co2_sensor', 'co2_sensor_config', (lambda v: int(round(v)))),
        ('o2', 'o2_sensor', 'o2_sensor_config', (lambda v: float(v))),
    ):
        if not initialized_components.get(_comp):
            continue
        _entry = {'name': _name, 'kind': 'atlas', 'device': None,
                  'delay': _gas_delay, 'cast': _cast}
        if not simulation_mode and bioreactor is not None:
            _scfg = getattr(bioreactor, _cfg_attr, {}) or {}
            _dev = _scfg.get('atlas_device')
            if _dev is not None:
                _entry['device'] = _dev
            elif _name == 'co2' and str(_scfg.get('type', '')).startswith('sensair'):
                # Senseair K33: no device object, io.read_co2 does the whole
                # transaction itself and dispatches on co2_sensor_config['type'].
                from bioreactor_v3.src.io import read_co2 as _read_co2
                _entry = {'name': _name, 'kind': 'direct', 'cast': _cast,
                          'read_fn': (lambda: _read_co2(bioreactor))}
            else:
                continue
        gas_sensors.append(_entry)
    if gas_sensors:
        gas_sampler.configure(hw_lock=HARDWARE_LOCK, sensors=gas_sensors, sim=simulation_mode,
                              period_s=getattr(config, 'GAS_SAMPLE_PERIOD_S', 5.0))
        gas_sampler.start()
        logger.info("Gas sensors available: %s", [s['name'] for s in gas_sensors])

    # Timed-dose pump controller: cycles inflow/outflow on a background thread from
    # a (interval, duty) regime set by POST /api/pumps/run or program 'pump' tracks.
    if initialized_components.get('pumps'):
        if simulation_mode:
            def _pump_run(name, rate):
                sim_state['pump_velocities'][name] = rate
            def _pump_stop(name):
                sim_state['pump_velocities'][name] = 0.0
        else:
            import time as _pt
            from bioreactor_v3.src.io import change_pump as _change_pump, stop_pump as _stop_pump
            _pump_on_since = {}
            def _pump_run(name, rate):
                with HARDWARE_LOCK:
                    _change_pump(bioreactor, name, rate)
                if rate and rate > 0:
                    _pump_on_since[name] = _pt.time()
            def _pump_stop(name):
                with HARDWARE_LOCK:
                    _stop_pump(bioreactor, name)
                # accumulate cumulative ON-time so the run CSV's pump_<name>_time_s tracks usage
                t0 = _pump_on_since.pop(name, None)
                if t0 is not None and hasattr(bioreactor, 'pump_run_times'):
                    bioreactor.pump_run_times[name] = bioreactor.pump_run_times.get(name, 0.0) + (_pt.time() - t0)
        pump_controller.configure(
            run_fn=_pump_run, stop_fn=_pump_stop,
            rate_ml_per_sec=getattr(config, 'PUMP_RUN_ML_PER_SEC', 1.0),
            inflow_ratio=getattr(config, 'PUMP_INFLOW_TIME_RATIO', 0.95),
        )
        pump_controller.start()

    # Relay controller: open/closed/toggle by name + timed command-wait-toggle. The
    # RelayDriver (real) / sim_state (sim) stores each relay's energized state.
    if initialized_components.get('relays'):
        _relay_names = list(getattr(config, 'RELAYS', {}).keys())
        for _n in _relay_names:
            sim_state['relays'].setdefault(_n, False)
        _relay_changed = None
        if simulation_mode:
            def _relay_set(name, energized):
                sim_state['relays'][name] = bool(energized)
            def _relay_get():
                return dict(sim_state['relays'])
        else:
            from bioreactor_v3.src.io import relay_on, relay_off, get_all_relay_states
            if bioreactor is not None and not hasattr(bioreactor, 'relay_closed_times'):
                bioreactor.relay_closed_times = {n: 0.0 for n in _relay_names}
            def _relay_set(name, energized):
                # GPIO writes do not use I2C; valve closure must not wait for that bus.
                if not (relay_on if energized else relay_off)(bioreactor, name):
                    raise RuntimeError(f"Relay {name} write failed")
            def _relay_changed(totals):
                # Mirror only after successful GPIO writes and time accounting.
                if bioreactor is not None and hasattr(bioreactor, 'relay_closed_times'):
                    bioreactor.relay_closed_times.update(totals)
            def _relay_get():
                with HARDWARE_LOCK:
                    return get_all_relay_states(bioreactor)
        relay_controller.configure(
            set_fn=_relay_set, get_fn=_relay_get, names=_relay_names,
            guards=getattr(config, 'RELAY_SAFETY', {}),
            co2_fn=lambda: gas_sampler.latest().get('co2'),   # for the CO2-gated dose guard
            on_change=_relay_changed,
        )
        # Add relay columns to the run CSV: measure_and_record_sensors already writes
        # each relay's state into the row, but only if the name is in bioreactor.fieldnames.
        if not simulation_mode and bioreactor is not None and hasattr(bioreactor, 'fieldnames'):
            for _n in _relay_names:
                for _col in (_n, f"relay_{_n}_closed_s"):   # instantaneous 0/1 + cumulative closed-time
                    if _col not in bioreactor.fieldnames:
                        bioreactor.fieldnames.append(_col)

    global co2_controller
    from co2_controller import CO2API
    co2_controller = CO2API(config, gas_sampler, relay_controller, initialized_components)

    # Rolling sensor-history buffer (samples continuously, independent of runs).
    if getattr(config, 'HISTORY_ENABLED', True):
        history.configure(
            sample_fn=_read_signals,
            archive_dir=str(HISTORY_DIR),
            interval_s=getattr(config, 'HISTORY_INTERVAL_S', 10),
            window_s=int(getattr(config, 'HISTORY_WINDOW_H', 24)) * 3600,
            retention_days=int(getattr(config, 'HISTORY_RETENTION_DAYS', 365)),
            legacy_path=str(HISTORY_FILE),
        )
        history.start()

    yield

    co2_controller.stop()
    gas_sampler.stop()
    od_sampler.stop()
    pump_controller.stop()
    relay_controller.stop()
    history.stop()
    runner.stop()
    if bioreactor:
        bioreactor.finish()
        logger.info("Hardware cleanup complete")


# ---------------------------------------------------------------------------
# Create app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Bioreactor API",
    description="Minimal REST API for bioreactor_v3 hardware control",
    version="0.1.0",
    lifespan=lifespan,
    dependencies=[Depends(verify_token)],
)

# Rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_component(name: str):
    """Raise 503 if component is not initialized."""
    if not initialized_components.get(name, False):
        raise HTTPException(status_code=503, detail=f"{name} not available")


# ---------------------------------------------------------------------------
# System endpoints (always available)
# ---------------------------------------------------------------------------

@app.get("/health")
@limiter.limit(RATE_LIMIT)
async def health(request: Request):
    return {
        "status": "healthy",
        "hardware_mode": "simulation" if simulation_mode else "real",
        "hardware_available": bioreactor is not None,
        "initialized_components": initialized_components,
    }


@app.get("/api/state")
@limiter.limit(RATE_LIMIT)
async def state(request: Request):
    """Aggregate run-relevant signals in one call (for the live monitor).

    Returns bath temp, ambient temp, signed peltier current, peltier duty/direction,
    and the current run status. Unavailable components report null.
    """
    import time as _time

    def _sensor(name, reader):
        if not initialized_components.get(name, False):
            return None
        try:
            v = reader()
        except Exception as e:
            logger.warning("state read failed for %s: %s", name, e)
            return None
        if v is not None and isinstance(v, float) and math.isnan(v):
            return None
        return v

    if simulation_mode:
        temperature = round(36.5 + random.uniform(-0.5, 0.5), 2) if initialized_components.get('temp_sensor') else None
        ambient = round(22.0 + random.uniform(-1.0, 1.0), 2) if initialized_components.get('ambient_temp') else None
        current = round(random.uniform(0.0, 0.05), 3) if initialized_components.get('peltier_current') else None
        peltier = {"duty_cycle": sim_state['peltier_duty'],
                   "direction": sim_state['peltier_direction'],
                   "active": sim_state['peltier_duty'] > 0} if initialized_components.get('peltier_driver') else None
    else:
        from bioreactor_v3.src.io import (
            get_temperature, read_ambient_temp, read_peltier_current, get_peltier_state,
        )
        # Serialize bus access against the run control loop (see control.HARDWARE_LOCK).
        with HARDWARE_LOCK:
            temperature = _sensor('temp_sensor', lambda: get_temperature(bioreactor, sensor_index=0))
            ambient = _sensor('ambient_temp', lambda: read_ambient_temp(bioreactor))
            current = _sensor('peltier_current', lambda: read_peltier_current(bioreactor))
            peltier = None
            forward = True
            if initialized_components.get('peltier_driver'):
                ps = get_peltier_state(bioreactor)
                if ps is not None:
                    duty, forward = ps
                    peltier = {"duty_cycle": duty,
                               "direction": "cool" if forward else "heat",
                               "active": duty > 0}
        # INA228 reads unsigned; sign negative when heating (forward False), per GUI convention
        if current is not None and not forward:
            current = -current

    _gas = gas_sampler.latest()

    return {
        "status": "success",
        "timestamp": _time.time(),
        "temperature": temperature,
        "ambient_temp": ambient,
        "peltier_current": current,
        "peltier": peltier,
        "run": runner.status(),
        "co2_control": co2_controller.status() if co2_controller else None,
        "co2": _gas.get('co2') if initialized_components.get('co2_sensor') else None,
        "o2": _gas.get('o2') if initialized_components.get('o2_sensor') else None,
        "od": _read_od(),
        "od_measurements": dict(optics.od) if optics is not None else {},
        "od_available": od_available,
        "od_sampling": od_sampler.status() if od_sampler.has_sources else None,
        "voltages": _read_voltages(),
        "led": {"power": led_power, "active": led_power > 0} if initialized_components.get('led') else None,
        "ring": {**ring_color, "active": any(ring_color.values())} if initialized_components.get('ring_light') else None,
        "stirrer": _stirrer_state(),
        "pumps": pump_controller.status() if initialized_components.get('pumps') else None,
        "relays": relay_controller.status() if initialized_components.get('relays') else None,
    }


@app.get("/api/capabilities")
@limiter.limit(RATE_LIMIT)
async def capabilities(request: Request):
    """Discover available components and their endpoint patterns."""
    caps = {}
    actuators = ['led', 'peltier_driver', 'stirrer', 'ring_light', 'pumps', 'relays']
    sensors = ['temp_sensor', 'ambient_temp', 'optical_density', 'eyespy_adc', 'co2_sensor', 'o2_sensor', 'peltier_current']

    for name in actuators:
        if initialized_components.get(name):
            caps[name] = {
                "type": "actuator",
                "control": f"/api/{name}/control",
                "state": f"/api/{name}/state",
            }
    for name in sensors:
        if initialized_components.get(name):
            caps[name] = {
                "type": "sensor",
                "state": f"/api/{name}/state",
            }
    if optics is not None and optics.od:
        caps["od"] = {"type": "sensor", "state": "/api/od/state",
                      "measurements": dict(optics.od), "available": od_available}
    if optics is not None and optics.sources:
        caps["voltages"] = {"type": "sensor", "state": "/api/voltages",
                            "each": "/api/voltage/{name}", "names": list(optics.sources)}
    return caps


# ---------------------------------------------------------------------------
# LED
# ---------------------------------------------------------------------------

@app.post("/api/led/control", response_model=LEDState)
@limiter.limit(RATE_LIMIT)
async def led_control(request: Request, req: LEDControlRequest):
    require_component('led')
    global led_power
    if simulation_mode:
        sim_state['led_power'] = req.power
        led_power = req.power
        return LEDState(status="success", power=req.power, active=req.power > 0)
    from bioreactor_v3.src.io import set_led
    with HARDWARE_LOCK:
        set_led(bioreactor, req.power)
    led_power = req.power   # shadow it (driver doesn't report last power)
    return LEDState(status="success", power=req.power, active=req.power > 0)


@app.get("/api/led/state", response_model=LEDState)
@limiter.limit(RATE_LIMIT)
async def led_state(request: Request):
    require_component('led')
    if simulation_mode:
        p = sim_state['led_power']
        return LEDState(status="success", power=p, active=p > 0)
    led = getattr(bioreactor, 'led_driver', None)
    power = getattr(led, '_last_power', 0.0) if led else 0.0
    return LEDState(status="success", power=power, active=power > 0)


# ---------------------------------------------------------------------------
# Peltier
# ---------------------------------------------------------------------------

@app.post("/api/peltier_driver/control", response_model=PeltierState)
@limiter.limit(RATE_LIMIT)
async def peltier_control(request: Request, req: PeltierControlRequest):
    require_component('peltier_driver')
    # During a legacy schedule/PID run manual control is blocked; during a program run
    # it's allowed and becomes an override (holds until the peltier track's next step).
    if runner.active and runner.mode != 'program':
        raise HTTPException(status_code=409,
                            detail="a run (schedule/PID) is active; stop it before manual control")
    if simulation_mode:
        sim_state['peltier_duty'] = req.duty_cycle
        sim_state['peltier_direction'] = req.direction
    else:
        from bioreactor_v3.src.io import set_peltier_power
        with HARDWARE_LOCK:
            set_peltier_power(bioreactor, req.duty_cycle, req.direction)
    runner.note_override('peltier')   # no-op unless a program is running
    return PeltierState(status="success", duty_cycle=req.duty_cycle, direction=req.direction, active=req.duty_cycle > 0)


@app.get("/api/peltier_driver/state", response_model=PeltierState)
@limiter.limit(RATE_LIMIT)
async def peltier_state(request: Request):
    require_component('peltier_driver')
    if simulation_mode:
        d = sim_state['peltier_duty']
        return PeltierState(status="success", duty_cycle=d, direction=sim_state['peltier_direction'], active=d > 0)
    from bioreactor_v3.src.io import get_peltier_state
    state = get_peltier_state(bioreactor)
    if state:
        duty, fwd = state
        return PeltierState(status="success", duty_cycle=duty, direction="forward" if fwd else "reverse", active=duty > 0)
    return PeltierState(status="success", duty_cycle=0, direction="forward", active=False)


# ---------------------------------------------------------------------------
# Stirrer
# ---------------------------------------------------------------------------

@app.post("/api/stirrer/control", response_model=StirrerState)
@limiter.limit(RATE_LIMIT)
async def stirrer_control(request: Request, req: StirrerControlRequest):
    require_component('stirrer')
    if simulation_mode:
        sim_state['stirrer_duty'] = req.duty_cycle
        return StirrerState(status="success", duty_cycle=req.duty_cycle, active=req.duty_cycle > 0)
    from bioreactor_v3.src.io import set_stirrer_speed
    set_stirrer_speed(bioreactor, req.duty_cycle)
    runner.note_override('stirrer')   # release from the schedule until the next stirrer step
    return StirrerState(status="success", duty_cycle=req.duty_cycle, active=req.duty_cycle > 0)


@app.get("/api/stirrer/state", response_model=StirrerState)
@limiter.limit(RATE_LIMIT)
async def stirrer_state(request: Request):
    require_component('stirrer')
    if simulation_mode:
        d = sim_state['stirrer_duty']
        return StirrerState(status="success", duty_cycle=d, active=d > 0)
    driver = getattr(bioreactor, 'stirrer_driver', None)
    duty = getattr(driver, '_duty', 0.0) if driver else 0.0
    return StirrerState(status="success", duty_cycle=duty, active=duty > 0)


# ---------------------------------------------------------------------------
# Ring Light
# ---------------------------------------------------------------------------

@app.post("/api/ring_light/control", response_model=RingLightState)
@limiter.limit(RATE_LIMIT)
async def ring_light_control(request: Request, req: RingLightControlRequest):
    require_component('ring_light')
    global ring_color
    if simulation_mode:
        sim_state['ring_r'] = req.red
        sim_state['ring_g'] = req.green
        sim_state['ring_b'] = req.blue
        ring_color = {'red': req.red, 'green': req.green, 'blue': req.blue}
        runner.note_override('ring')
        active = any([req.red, req.green, req.blue])
        return RingLightState(status="success", red=req.red, green=req.green, blue=req.blue, active=active)
    from bioreactor_v3.src.io import set_ring_light
    with HARDWARE_LOCK:
        set_ring_light(bioreactor, (req.red, req.green, req.blue), pixel=req.pixel_index)
    ring_color = {'red': req.red, 'green': req.green, 'blue': req.blue}
    runner.note_override('ring')   # release from the schedule until the next ring step
    active = any([req.red, req.green, req.blue])
    return RingLightState(status="success", red=req.red, green=req.green, blue=req.blue, active=active)


@app.get("/api/ring_light/state", response_model=RingLightState)
@limiter.limit(RATE_LIMIT)
async def ring_light_state(request: Request):
    require_component('ring_light')
    if simulation_mode:
        r, g, b = sim_state['ring_r'], sim_state['ring_g'], sim_state['ring_b']
        return RingLightState(status="success", red=r, green=g, blue=b, active=any([r, g, b]))
    from bioreactor_v3.src.io import get_ring_light_color
    color = get_ring_light_color(bioreactor)
    if color:
        r, g, b = color
        return RingLightState(status="success", red=r, green=g, blue=b, active=any([r, g, b]))
    return RingLightState(status="success", red=0, green=0, blue=0, active=False)


# ---------------------------------------------------------------------------
# Pumps
# ---------------------------------------------------------------------------

@app.post("/api/pumps/control", response_model=PumpState)
@limiter.limit(RATE_LIMIT)
async def pumps_control(request: Request, req: PumpControlRequest):
    require_component('pumps')
    if simulation_mode:
        sim_state['pump_name'] = req.pump_name
        sim_state['pump_velocity'] = req.velocity
        return PumpState(status="success", pump_name=req.pump_name, velocity=req.velocity, active=req.velocity != 0)
    from bioreactor_v3.src.io import change_pump
    change_pump(bioreactor, req.pump_name, req.velocity)
    return PumpState(status="success", pump_name=req.pump_name, velocity=req.velocity, active=req.velocity != 0)


@app.get("/api/pumps/state", response_model=PumpState)
@limiter.limit(RATE_LIMIT)
async def pumps_state(request: Request):
    require_component('pumps')
    if simulation_mode:
        return PumpState(status="success", pump_name=sim_state['pump_name'], velocity=sim_state['pump_velocity'], active=sim_state['pump_velocity'] != 0)
    return PumpState(status="success", pump_name="all", velocity=0.0, active=bool(getattr(bioreactor, 'pumps', None)))


@app.post("/api/pumps/run")
@limiter.limit(RATE_LIMIT)
async def pumps_run(request: Request, req: PumpRunRequest):
    """Start (or update) continuous media-exchange dosing: every `duration` seconds,
    run outflow for duration*duty and inflow for 0.95*duration*duty (duty 0-100%).
    duty 0 stops it. Cycles until POST /api/pumps/stop or a new regime."""
    require_component('pumps')
    pump_controller.set_regime(req.duration, req.duty_cycle, req.flow_rate)
    runner.note_override('pump')   # a program's pump track yields to this until its next step
    return {"status": "success", **pump_controller.status()}


@app.post("/api/pumps/dose")
@limiter.limit(RATE_LIMIT)
async def pumps_dose(request: Request, req: PumpRunRequest):
    """Run a SINGLE dose — outflow for duration*duty, inflow for 0.95*duration*duty
    (duty 0-100%) — then stop. Same body as /run; doesn't repeat."""
    require_component('pumps')
    pump_controller.dose(req.duration, req.duty_cycle, req.flow_rate)
    runner.note_override('pump')
    return {"status": "success", **pump_controller.status()}


@app.post("/api/pumps/stop")
@limiter.limit(RATE_LIMIT)
async def pumps_stop(request: Request):
    """Stop pump dosing (both pumps off)."""
    require_component('pumps')
    pump_controller.off()
    runner.note_override('pump')
    return {"status": "success", **pump_controller.status()}


# ---------------------------------------------------------------------------
# Relays
# ---------------------------------------------------------------------------

@app.post("/api/relays/control", response_model=RelayState)
@limiter.limit(RATE_LIMIT)
async def relays_control(request: Request, req: RelayControlRequest):
    """Set a relay: command is open | closed | toggle."""
    require_component('relays')
    try:
        co2_controller.manual(req.relay_name, req.command)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no relay named '{req.relay_name}'")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RelaySafetyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    runner.note_override(f"relay:{req.relay_name}")
    return RelayState(status="success", **relay_controller.status())


@app.post("/api/relays/timed", response_model=RelayState)
@limiter.limit(RATE_LIMIT)
async def relays_timed(request: Request, req: RelayTimedRequest):
    """command-wait-toggle: run `command` now, then toggle the relay after `duration` s."""
    require_component('relays')
    try:
        co2_controller.manual(req.relay_name, req.command, req.duration)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no relay named '{req.relay_name}'")
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RelaySafetyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    runner.note_override(f"relay:{req.relay_name}")
    return RelayState(status="success", **relay_controller.status())


@app.get("/api/relays/state", response_model=RelayState)
@limiter.limit(RATE_LIMIT)
async def relays_state(request: Request):
    require_component('relays')
    return RelayState(status="success", **relay_controller.status())


# ---------------------------------------------------------------------------
# Temperature Sensor
# ---------------------------------------------------------------------------

@app.get("/api/temp_sensor/state", response_model=TemperatureState)
@limiter.limit(RATE_LIMIT)
async def temp_sensor_state(request: Request):
    require_component('temp_sensor')
    if simulation_mode:
        return TemperatureState(status="success", temperature=round(36.5 + random.uniform(-0.5, 0.5), 2))
    from bioreactor_v3.src.io import get_temperature
    temp = get_temperature(bioreactor, sensor_index=0)
    if temp is not None and isinstance(temp, float) and math.isnan(temp):
        temp = None
    return TemperatureState(status="success", temperature=temp)


# ---------------------------------------------------------------------------
# Ambient Temperature Sensor (PCT2075)
# ---------------------------------------------------------------------------

@app.get("/api/ambient_temp/state", response_model=AmbientTempState)
@limiter.limit(RATE_LIMIT)
async def ambient_temp_state(request: Request):
    require_component('ambient_temp')
    if simulation_mode:
        return AmbientTempState(status="success", temperature=round(22.0 + random.uniform(-1.0, 1.0), 2))
    from bioreactor_v3.src.io import read_ambient_temp
    temp = read_ambient_temp(bioreactor)
    if temp is not None and isinstance(temp, float) and math.isnan(temp):
        temp = None
    return AmbientTempState(status="success", temperature=temp)


# ---------------------------------------------------------------------------
# Atlas CO2 / O2 gas sensors
# Served from the gas sampler's cache — a live Atlas read takes ~1.5s, too slow
# for a request path, so the background sampler polls them every GAS_SAMPLE_PERIOD_S.
# ---------------------------------------------------------------------------

@app.get("/api/co2_sensor/state")
@limiter.limit(RATE_LIMIT)
async def co2_sensor_state(request: Request):
    require_component('co2_sensor')
    return {"status": "success", "co2_ppm": gas_sampler.latest().get('co2')}


@app.get("/api/o2_sensor/state")
@limiter.limit(RATE_LIMIT)
async def o2_sensor_state(request: Request):
    require_component('o2_sensor')
    return {"status": "success", "o2_percent": gas_sampler.latest().get('o2')}


# ---------------------------------------------------------------------------
# Optical Density
# ---------------------------------------------------------------------------

@app.get("/api/optical_density/state", response_model=ODState)
@limiter.limit(RATE_LIMIT)
async def od_state(request: Request):
    """Deprecated alias: un-gated live voltages of the ADS1115 sources, positional in plan
    order (`names` gives the order). Prefer GET /api/od/state or GET /api/voltages."""
    require_component('optical_density')
    names = _source_names('adc')
    return ODState(status="success", names=names, voltages=[_live_voltage(n) for n in names])


# ---------------------------------------------------------------------------
# Eyespy ADC
# ---------------------------------------------------------------------------

@app.get("/api/eyespy_adc/state", response_model=EyespyState)
@limiter.limit(RATE_LIMIT)
async def eyespy_state(request: Request):
    """Deprecated alias: un-gated live voltages of the eyespy sources, positional in plan
    order (`names` gives the order). Prefer GET /api/od/state or GET /api/voltages."""
    require_component('eyespy_adc')
    names = _source_names('eyespy')
    return EyespyState(status="success", names=names, voltages=[_live_voltage(n) for n in names])


# ---------------------------------------------------------------------------
# Peltier Current Sensor (INA228)
# ---------------------------------------------------------------------------

@app.get("/api/peltier_current/state", response_model=PeltierCurrentState)
@limiter.limit(RATE_LIMIT)
async def peltier_current_state(request: Request):
    require_component('peltier_current')
    if simulation_mode:
        return PeltierCurrentState(status="success", current=round(random.uniform(0.0, 6.0), 3))
    from bioreactor_v3.src.io import read_peltier_current
    current = read_peltier_current(bioreactor)
    if current is not None and isinstance(current, float) and math.isnan(current):
        current = None
    return PeltierCurrentState(status="success", current=current)


# ---------------------------------------------------------------------------
# Run control engine (schedule + PID) — runs on the Pi, next to the hardware
# ---------------------------------------------------------------------------

@app.post("/api/run/schedule")
@limiter.limit(RATE_LIMIT)
async def run_schedule(request: Request):
    """Upload a peltier schedule CSV (duty,direction,hold_s) and start running it.

    Body is the raw CSV text (Content-Type text/plain or text/csv). Same format
    as heater_gui / peltier_schedule_example.csv.
    """
    require_component('peltier_driver')
    raw = await request.body()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="schedule body must be UTF-8 text")
    try:
        steps = parse_schedule(text, max_heat=runner.max_heat, max_cool=runner.max_cool)
    except ScheduleError as e:
        raise HTTPException(status_code=400, detail=f"invalid schedule: {e}")
    try:
        runner.start_schedule(steps)
    except InsufficientStorageError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    total_hold = round(sum(s['hold_s'] for s in steps), 1)
    return {"status": "success", "mode": "schedule",
            "total_steps": len(steps), "total_duration_s": total_hold,
            **runner.status()}


@app.post("/api/run/pid")
@limiter.limit(RATE_LIMIT)
async def run_pid(request: Request, req: PIDRequest):
    """Start a PID run that holds the bath at `setpoint` °C (heater_gui PID mode)."""
    require_component('peltier_driver')
    if not simulation_mode:
        require_component('temp_sensor')
    try:
        runner.start_pid(req.setpoint, req.kp, req.ki, req.kd)
    except InsufficientStorageError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success", "mode": "pid", **runner.status()}


@app.post("/api/run/program")
@limiter.limit(RATE_LIMIT)
async def run_program(request: Request):
    """Upload + run a multi-device program (JSON). Parallel per-device tracks step
    through their commands; manual dashboard changes override a device until its
    track's next step. Body is the program JSON (see program.py)."""
    require_component('peltier_driver')
    raw = await request.body()
    limits = {'max_heat': runner.max_heat, 'max_cool': runner.max_cool,
              'temp_min': TEMP_MIN_C, 'temp_max': TEMP_MAX_C}
    try:
        prog = parse_program(raw.decode('utf-8'), limits=limits)
    except (UnicodeDecodeError, ProgramError) as e:
        raise HTTPException(status_code=400, detail=f"invalid program: {e}")
    try:
        for tr in prog.tracks:
            for step in tr.steps:
                if step.command == 'co2' and step.value is not False:
                    co2_controller.validate(step.value)
        if co2_controller.status()['active'] and any(tr.device == 'relay:CO2' for tr in prog.tracks):
            raise RuntimeError('CO2 control is already active')
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # temp steps need a temp sensor for the PID
    if not simulation_mode and any(
            s.command == 'temp' for tr in prog.tracks for s in tr.steps):
        require_component('temp_sensor')
    try:
        runner.start_program(prog, gains=getattr(prog, 'gains', None), raw_json=raw.decode('utf-8'))
    except InsufficientStorageError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success", "mode": "program", **runner.status()}


@app.post("/api/run/program/preview")
@limiter.limit(RATE_LIMIT)
async def run_program_preview(request: Request):
    """Validate a program and return its per-track segment timeline for a preview.
    Doesn't run anything. Returns {valid:false, error} (200) on a bad program so the
    editor can show it inline."""
    raw = await request.body()
    limits = {'max_heat': runner.max_heat, 'max_cool': runner.max_cool,
              'temp_min': TEMP_MIN_C, 'temp_max': TEMP_MAX_C}
    try:
        prog = parse_program(raw.decode('utf-8'), limits=limits)
    except (UnicodeDecodeError, ProgramError) as e:
        return {"valid": False, "error": str(e)}
    return {"valid": True, "name": prog.name, "duration_s": prog.duration_s,
            **expand_tracks(prog)}


@app.post("/api/run/stop")
@limiter.limit(RATE_LIMIT)
async def run_stop(request: Request):
    """Stop any active schedule/PID run and turn the peltier off."""
    co2_controller.stop()
    status = runner.stop(reason="stopped via API")
    return {"status": "success", **status}


@app.get("/api/run/status")
@limiter.limit(RATE_LIMIT)
async def run_status(request: Request):
    """Current run state (mode, step/progress, last sample, abort reason)."""
    return runner.status()


# ---------------------------------------------------------------------------
# Sensor history (rolling 24h buffer for the live plot)
# ---------------------------------------------------------------------------

@app.get("/api/history")
@limiter.limit(RATE_LIMIT)
async def api_history(request: Request, since: int = 0):
    """Rolling history of temp/ambient/current. `?since=<ms>` returns only points
    newer than that timestamp (cheap incremental polling)."""
    return {"status": "success", "interval_s": history.interval_s,
            "od_measurements": dict(optics.od) if optics is not None else {},
            "od_available": od_available,
            "archive_earliest_ms": history.earliest_ms(),
            "points": history.get(since_ms=since)}


@app.get("/api/history/range")
@limiter.limit(RATE_LIMIT)
async def api_history_range(request: Request, start: int = 0, end: int = 0):
    """History for an arbitrary [start, end] ms range, read straight from the on-disk
    daily archive (the rolling /api/history only spans the last ~24h in memory). Backs
    the 'since program start' plot view; long ranges are downsampled server-side. Same
    payload shape as /api/history so the frontend can reuse its ingest path."""
    return {"status": "success", "interval_s": history.interval_s,
            "od_measurements": dict(optics.od) if optics is not None else {},
            "od_available": od_available,
            "points": history.read_range(start_ms=start, end_ms=(end or None))}


@app.get("/api/od/state")
@limiter.limit(RATE_LIMIT)
async def od_measurements_state(request: Request):
    """The canonical OD measurements (OD_45 / OD_ref / OD_90 / OD_135) from the last
    IR-gated sampler pulse, plus the voltage source feeding each. A null value means the
    measurement has not been taken yet or sampling is off."""
    if optics is None or not optics.od:
        raise HTTPException(status_code=503, detail="No OD measurements configured (OD_MEASUREMENTS)")
    return {"status": "success",
            "od": _read_od() or {k: None for k in optics.od},
            "measurements": dict(optics.od),
            "available": od_available,
            "sampling": od_sampler.status() if od_sampler.has_sources else None,
            "unit": "volts"}


@app.get("/api/voltages")
@limiter.limit(RATE_LIMIT)
async def voltages_state(request: Request):
    """Every named voltage source: kind, the OD measurements it feeds, its last IR-gated
    reading (`gated`, from the sampler) and an instantaneous un-gated reading taken now
    (`live`, IR LED not pulsed)."""
    if optics is None or not optics.sources:
        raise HTTPException(status_code=503, detail="No voltage sources configured (VOLTAGE_SOURCES)")
    gated = _read_voltages() or {}
    out = {}
    for name, spec in optics.sources.items():
        avail = bool(initialized_components.get(spec.component, False))
        out[name] = {"kind": spec.kind, "component": spec.component, "available": avail,
                     "od": optics.od_names_for_source(name),
                     "gated": gated.get(name), "live": _live_voltage(name) if avail else None}
    return {"status": "success", "voltages": out, "unit": "volts"}


@app.get("/api/voltage/{name}")
@limiter.limit(RATE_LIMIT)
async def voltage_state(request: Request, name: str):
    """One named voltage source (see GET /api/voltages for the fields)."""
    if optics is None or name not in optics.sources:
        raise HTTPException(status_code=404, detail=f"unknown voltage source '{name}'"
                            + (f"; known: {list(optics.sources)}" if optics is not None else ""))
    spec = optics.sources[name]
    if not initialized_components.get(spec.component, False):
        raise HTTPException(status_code=503, detail=f"'{name}' needs component '{spec.component}', which is not available")
    gated = (_read_voltages() or {}).get(name)
    return {"status": "success", "name": name, "kind": spec.kind, "component": spec.component,
            "od": optics.od_names_for_source(name), "gated": gated, "live": _live_voltage(name),
            "unit": "volts"}


class ODSamplingRequest(BaseModel):
    enabled: Optional[bool] = Field(default=None, description="turn IR-gated OD sampling on/off")
    led_power: Optional[float] = Field(default=None, ge=0, le=100,
                                       description="IR LED %% used for each gated reading")


@app.post("/api/od/sampling")
@limiter.limit(RATE_LIMIT)
async def set_od_sampling(request: Request, req: ODSamplingRequest):
    """Control the IR-gated OD measurement: on/off + per-reading LED power.

    The LED only lights briefly during each gated reading (never steady-on); `enabled`
    gates whether the sampler runs at all, `led_power` sets the illumination level."""
    if not od_sampler.has_sources:
        raise HTTPException(status_code=503, detail="No optical-density source available")
    if req.enabled is None and req.led_power is None:
        raise HTTPException(status_code=400, detail="provide 'enabled' and/or 'led_power'")
    cfg = od_sampler.set_config(enabled=req.enabled, led_power=req.led_power)
    runner.note_override('od')   # a program's od track yields to this until its next step
    return {"status": "success", "od_sampling": cfg}


# ---------------------------------------------------------------------------
# Data files (download the most recent bioreactor run CSV)
# ---------------------------------------------------------------------------

def _list_data_files():
    """Return run CSVs under DATA_DIR (recursive), newest first, with metadata."""
    files = []
    if DATA_DIR.is_dir():
        for p in DATA_DIR.rglob('*.csv'):
            try:
                stat = p.stat()
            except OSError:
                continue
            files.append((p, stat.st_mtime, stat.st_size))
    files.sort(key=lambda t: t[1], reverse=True)
    return files


@app.get("/api/data/list")
@limiter.limit(RATE_LIMIT)
async def data_list(request: Request):
    """List available data CSVs (newest first)."""
    return {"status": "success", "files": [
        {"name": p.relative_to(DATA_DIR).as_posix(),
         "size_bytes": size,
         "modified": mtime}
        for p, mtime, size in _list_data_files()
    ]}


@app.get("/api/data/latest")
@limiter.limit(RATE_LIMIT)
async def data_latest(request: Request):
    """Download the most recently modified data CSV."""
    files = _list_data_files()
    if not files:
        raise HTTPException(status_code=404, detail="no data files found")
    path = files[0][0]
    return FileResponse(str(path), media_type='text/csv', filename=path.name)


# ---------------------------------------------------------------------------
# Camera (Pi camera snapshot via rpicam-still)
# ---------------------------------------------------------------------------

@app.get("/api/camera/snapshot")
@limiter.limit(RATE_LIMIT)
async def camera_snapshot(request: Request,
                          rotation: Optional[int] = None,
                          hflip: Optional[bool] = None,
                          vflip: Optional[bool] = None,
                          zoom: Optional[float] = None):
    """Return a single JPEG frame from the Pi camera.

    Optional query params override the configured defaults:
    rotation (0|180), hflip (bool), vflip (bool), zoom (>=1.0, centered digital zoom).
    """
    config = _get_config()
    if not getattr(config, 'CAMERA_ENABLED', True) or not camera.available():
        raise HTTPException(status_code=503, detail="camera not available")
    rot = int(rotation) if rotation is not None else int(getattr(config, 'CAMERA_ROTATION', 0))
    if rot not in (0, 180):
        raise HTTPException(status_code=400, detail="rotation must be 0 or 180")
    try:
        jpeg = await run_in_threadpool(
            camera.capture_jpeg,
            width=getattr(config, 'CAMERA_WIDTH', 1280),
            height=getattr(config, 'CAMERA_HEIGHT', 720),
            rotation=rot,
            hflip=(bool(getattr(config, 'CAMERA_HFLIP', False)) if hflip is None else hflip),
            vflip=(bool(getattr(config, 'CAMERA_VFLIP', False)) if vflip is None else vflip),
            zoom=(float(getattr(config, 'CAMERA_ZOOM', 1.0)) if zoom is None else zoom),
            quality=getattr(config, 'CAMERA_QUALITY', 90),
        )
    except camera.CameraError as e:
        raise HTTPException(status_code=503, detail=f"camera: {e}")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _actuator_signals():
    """Ring RGB / stirrer duty / IR-LED power / active temperature setpoint for the
    history sampler. All come from shadows or the control lock, so this MUST be called
    OUTSIDE HARDWARE_LOCK to avoid lock-order inversion with the control thread."""
    stir = _stirrer_state()
    sig = {
        "ring": ([ring_color['red'], ring_color['green'], ring_color['blue']]
                 if initialized_components.get('ring_light') else None),
        "stirrer": stir['duty'] if stir else None,
        "ir_power": od_sampler.led_power if initialized_components.get('led') else None,
        "setpoint": runner.status().get('setpoint'),   # None unless a PID/program targets a temp
        "co2_target_ppm": (co2_controller.status().get('target_ppm')
                           if co2_controller and co2_controller.status().get('active') else None),
        "pump_duty": None,
        "pump_time_s": None,
        "relays": None,
        "relay_closed_s": None,
    }
    if initialized_components.get('pumps'):
        ps = pump_controller.status()
        sig["pump_duty"] = ps['duty'] if ps['active'] else 0.0   # 0-100%, 0 when idle
        # Cumulative pump ON-time captures brief doses (diff successive samples for the
        # per-interval amount). None in sim, where there's no bioreactor to track it.
        if bioreactor is not None and hasattr(bioreactor, 'pump_run_times'):
            sig["pump_time_s"] = dict(bioreactor.pump_run_times)
    if initialized_components.get('relays'):
        sig["relays"] = {n: (1 if s == 'closed' else 0)
                         for n, s in relay_controller.states().items()}
        # Cumulative closed-time per relay — captures doses shorter than the sample interval.
        sig["relay_closed_s"] = relay_controller.closed_seconds()
    return sig


def _read_signals():
    """Monitor signals for the history sampler: bath temp, ambient, signed peltier
    current, gas (co2/o2), OD, plus actuator/control state (signed peltier duty, ring,
    stirrer, IR-LED power, setpoint). Sensor reads are guarded by HARDWARE_LOCK in real
    mode; actuator state is read outside it. Missing components report None.
    """
    if simulation_mode:
        _gas = gas_sampler.latest()
        _pdir = sim_state.get('peltier_direction', 'cool')
        _pd = sim_state.get('peltier_duty', 0.0)
        pduty = (_pd if _pdir == 'cool' else -_pd) if initialized_components.get('peltier_driver') else None
        return {
            "temperature": round(36.5 + random.uniform(-0.5, 0.5), 2) if initialized_components.get('temp_sensor') else None,
            "ambient_temp": round(22.0 + random.uniform(-1.0, 1.0), 2) if initialized_components.get('ambient_temp') else None,
            "peltier_current": round(random.uniform(0.0, 0.05), 3) if initialized_components.get('peltier_current') else None,
            "co2": _gas.get('co2') if initialized_components.get('co2_sensor') else None,
            "o2": _gas.get('o2') if initialized_components.get('o2_sensor') else None,
            "od": _read_od(),
            "volt": _read_unmapped_voltages(),
            "peltier_duty": pduty,
            **_actuator_signals(),
        }
    from bioreactor_v3.src.io import (
        get_temperature, read_ambient_temp, read_peltier_current, get_peltier_state,
    )

    def _rd(name, fn):
        if not initialized_components.get(name):
            return None
        try:
            v = fn()
        except Exception:
            return None
        if v is not None and isinstance(v, float) and math.isnan(v):
            return None
        return v

    with HARDWARE_LOCK:
        temp = _rd('temp_sensor', lambda: get_temperature(bioreactor, sensor_index=0))
        ambient = _rd('ambient_temp', lambda: read_ambient_temp(bioreactor))
        current = _rd('peltier_current', lambda: read_peltier_current(bioreactor))
        forward = True
        pduty = None
        if initialized_components.get('peltier_driver'):
            ps = get_peltier_state(bioreactor)
            if ps is not None:
                duty_val, forward = ps
                pduty = duty_val if forward else -duty_val   # + cool, - heat (matches signed current)
    if current is not None and not forward:
        current = -current   # sign negative when heating, matching /api/state
    _gas = gas_sampler.latest()
    return {"temperature": temp, "ambient_temp": ambient, "peltier_current": current,
            "co2": _gas.get('co2') if initialized_components.get('co2_sensor') else None,
            "o2": _gas.get('o2') if initialized_components.get('o2_sensor') else None,
            "od": _read_od(),
            "volt": _read_unmapped_voltages(),
            "peltier_duty": pduty,
            **_actuator_signals()}


def _read_od():
    """Latest IR-gated OD measurements {OD_x: volts|None}, mapped from the sampler cache
    ({source: volts}); None if no OD measurement is configured, sampling is off, or
    nothing has been measured yet. The gated measurement (LED on -> read -> off) happens
    on the od_sampler thread, not here."""
    if optics is None or not optics.od:
        return None
    cache = od_sampler.latest()
    if cache is None:
        return None
    return {od: cache.get(src) for od, src in optics.od.items()}


def _read_voltages():
    """Latest IR-gated reading of every voltage source ({source: volts|None}) or None."""
    if optics is None or not optics.sources:
        return None
    cache = od_sampler.latest()
    if cache is None:
        return None
    return {name: cache.get(name) for name in optics.sources}


def _read_unmapped_voltages():
    """Gated readings of sources no OD measurement consumes (archived as history 'volt')."""
    cache = _read_voltages()
    if not cache or optics is None:
        return None
    return {n: cache.get(n) for n in optics.unmapped_sources()} or None


def _source_names(kind):
    """Names of the plan's sources of one kind ('adc' | 'eyespy'), plan order."""
    return [s.name for s in optics.sources_of(kind)] if optics is not None else []


def _live_voltage(name):
    """Instantaneous un-gated reading of one source (IR LED not pulsed), None on error.
    Serialized on HARDWARE_LOCK against the samplers and the run loop."""
    if simulation_mode:
        return round(random.uniform(0.5, 2.5), 4)
    if bioreactor is None:
        return None
    from bioreactor_v3.src.io import read_voltage
    try:
        with HARDWARE_LOCK:
            v = read_voltage(bioreactor, name, quiet=True)
    except Exception as e:
        logger.warning("live voltage read failed for %s: %s", name, e)
        return None
    return None if (v is None or (isinstance(v, float) and math.isnan(v))) else v


def _load_optics_module():
    """bioreactor_v3/src/optics.py: the submodule package on the rig; for local simulation
    with an empty submodule dir, the file itself from the submodule or a sibling
    bioreactor_v3 checkout (it is pure Python, so loading it by path is safe)."""
    try:
        from bioreactor_v3.src import optics as mod
        return mod
    except ImportError:
        pass
    import importlib.util
    here = Path(__file__).resolve().parent
    for cand in (here / 'bioreactor_v3' / 'src' / 'optics.py',
                 here.parent.parent / 'bioreactor_v3' / 'src' / 'optics.py'):
        if cand.is_file():
            spec = importlib.util.spec_from_file_location('bioreactor_v3_optics', cand)
            mod = importlib.util.module_from_spec(spec)
            sys.modules['bioreactor_v3_optics'] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("bioreactor_v3/src/optics.py not found: run 'git submodule update --init' "
                       "or clone bioreactor_v3 next to bioreactor_website")


def _resolve_optics(config):
    return _load_optics_module().resolve_optical_config(config)


def _describe_optics(plan):
    return _load_optics_module().describe(plan)


def _stirrer_state():
    """Current stirrer duty for /api/state ({'duty','active'} or None)."""
    if not initialized_components.get('stirrer'):
        return None
    if simulation_mode:
        d = float(sim_state.get('stirrer_duty', 0.0))
    else:
        driver = getattr(bioreactor, 'stirrer_driver', None)
        d = float(getattr(driver, '_duty', 0.0)) if driver is not None else 0.0
    return {"duty": round(d, 1), "active": d > 0}


def _ring_dodge(active):
    """Dodge the ring light around an OD read: blank it (active=True) then restore its
    commanded colour (active=False). Off through the whole IR-on window so its light
    can't contaminate the read and it can't glitch visibly from IR-PWM SPI noise; the
    restore re-asserts the colour. dodge_off() keeps current_color intact, so /api/state
    still shows the commanded colour. Called by the OD sampler under HARDWARE_LOCK, so
    the two calls bracket one measurement atomically."""
    driver = getattr(bioreactor, 'ring_light_driver', None)
    if driver is None:
        return
    if active:
        driver.dodge_off()   # blank the strip, keep the commanded colour
    else:
        driver.refresh()     # restore the commanded colour (silent)


def _program_apply_ring(color):
    """Apply a program track's ring command: set the strip AND update the /api/state
    shadow so the readout/plot reflect the program-driven colour."""
    global ring_color
    r, g, b = int(color[0]), int(color[1]), int(color[2])
    if simulation_mode or bioreactor is None:
        sim_state['ring_r'], sim_state['ring_g'], sim_state['ring_b'] = r, g, b
    else:
        from bioreactor_v3.src.io import set_ring_light
        with HARDWARE_LOCK:
            set_ring_light(bioreactor, (r, g, b))
    ring_color = {'red': r, 'green': g, 'blue': b}


def _program_apply_stirrer(duty):
    """Apply a program track's stirrer command."""
    d = float(duty)
    if simulation_mode or bioreactor is None:
        sim_state['stirrer_duty'] = d
    else:
        from bioreactor_v3.src.io import set_stirrer_speed
        with HARDWARE_LOCK:
            set_stirrer_speed(bioreactor, d)


def _program_apply_relay(name, state):
    """Apply a program track's relay command. A safety-guarded relay's dose may be
    refused (rate limit / CO2) — log and carry on rather than crash the control tick."""
    try:
        co2_controller.manual(name, state)
    except RelaySafetyError as e:
        logger.warning("program relay %s -> %s blocked: %s", name, state, e)


def _get_config():
    """Lazy-load config for simulation mode sensor defaults."""
    from config import Config
    return Config()


class CO2StartRequest(BaseModel):
    target_ppm: float = Field(gt=0, lt=95000, allow_inf_nan=False, strict=True)
    duration_s: float = Field(default=3600, gt=0, le=604800, allow_inf_nan=False, strict=True)


@app.post("/api/co2/control")
@limiter.limit(RATE_LIMIT)
async def start_co2_control(request: Request, req: CO2StartRequest):
    try:
        if runner.active and runner.mode == 'program' and any(
                tr.device == 'relay:CO2' for tr in runner.program.tracks):
            raise RuntimeError('An active program owns the CO2 valve')
        co2_controller.start(req.target_ppm, duration_s=req.duration_s)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "success", **co2_controller.status()}


@app.post("/api/co2/stop")
@limiter.limit(RATE_LIMIT)
async def stop_co2_control(request: Request):
    co2_controller.stop()
    runner.note_override('relay:CO2')
    return {"status": "success", **co2_controller.status()}


@app.get("/api/co2/controller")
@limiter.limit(RATE_LIMIT)
async def co2_control_state(request: Request):
    return co2_controller.status()
