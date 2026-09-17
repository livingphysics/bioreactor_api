"""
TEMPLATE -- copy to bioreactor-api/config.py and edit for your rig:

    cp bioreactor-api/config.example.py bioreactor-api/config.py

config.py itself is gitignored because every value below is rig-specific.
Start from this file rather than bioreactor_v3/src/config_default.py: the
driver defaults do not carry the ~25 API-layer settings defined here (camera,
history, data retention, gas/OD sampler tuning, pump rates) and, most
importantly, RELAY_SAFETY. main.py reads that with
getattr(config, 'RELAY_SAFETY', {}), so omitting it silently leaves the gas
relays with NO dose guard -- no auto-revert, no rate limit, no CO2 ceiling.

Values here are bioreactor01's. Re-check every one marked CALIBRATE or VERIFY
before driving hardware -- in particular PUMPS serials/steps_per_ml and
PELTIER_DIR_INVERTED, both of which are wiring-specific and have been wrong
on this rig before.

Hardware configuration for the bioreactor API.

Edit INIT_COMPONENTS to enable/disable hardware components.
Only enabled components get API endpoints.
"""
import os
import sys
from pathlib import Path
from typing import Optional, Union

# Add bioreactor_v3 to path
BIOREACTOR_V3_PATH = Path(__file__).parent / 'bioreactor_v3' / 'src'
sys.path.insert(0, str(BIOREACTOR_V3_PATH))

# Default CO2 ceiling for the relay dose guard (see RELAY_SAFETY below). The guard
# refuses a dose when CO2 is near the top of what the sensor can read, so the number
# tracks the sensor: the Atlas EZO-CO2 reads to 10000 ppm, the Senseair K33 to
# ~100000. Prefix-matched the same way io.read_co2 dispatches on CO2_SENSOR_TYPE.
CO2_MAX_PPM_BY_SENSOR = {'sensair': 95000, 'atlas': 7500}


def _co2_max_ppm_for(sensor_type: str) -> int:
    """Dose-guard CO2 ceiling for `sensor_type`, overridable with the
    RELAY_CO2_MAX_PPM env var. An unrecognised sensor falls back to the tighter
    Atlas ceiling rather than the permissive one."""
    override = os.getenv('RELAY_CO2_MAX_PPM')
    if override:
        return int(float(override))
    for prefix, ppm in CO2_MAX_PPM_BY_SENSOR.items():
        if sensor_type.lower().startswith(prefix):
            return ppm
    return CO2_MAX_PPM_BY_SENSOR['atlas']

class Config:
    """
    Hardware configuration.

    Override default settings here for your specific hardware.
    Only include settings that differ from defaults.
    """

    # CO2 MPC is opt-in. Supply a validated, rig-specific model after response tests.
    # See docs/co2_mpc.md in bioreactor_v3; no controller starts automatically.
    # Optional profile["uncertainty"] enables scenario planning and bounded gain learning.
    # Omit it for legacy planning; see the commissioning example in the same docs.
    # Optional provisional profile setting: trial.allow_indefinite=True permits
    # duration 0 (until stopped), retaining all concentration/pulse/sensor limits.
    CO2_MPC = None

    # ========================================================================
    # Component Initialization Control
    # ========================================================================
    # Set to True to initialize, False to skip.
    # Only initialized components get API endpoints.
    INIT_COMPONENTS = {
        'i2c': True,
        'temp_sensor': True,
        'peltier_driver': True,
        'stirrer': True,
        'led': True,
        'ring_light': True,
        'optical_density': True,
        'eyespy_adc': True,
        'co2_sensor': True,
        'o2_sensor': True,
        'ambient_temp': True,
        'peltier_current': True,
        'pumps': True,
        'relays': True,
    }

    # Peltier Driver (Raspberry Pi 5 GPIO via lgpio)
    PELTIER_PWM_PIN: int = 21
    PELTIER_DIR_PIN: int = 20
    PELTIER_PWM_FREQ: int = 1000
    # DIR-pin polarity. True inverts it, for rigs wired opposite the driver
    # convention. bioreactor01's peltier follows the driver convention, so this is
    # False here; it was True (set 2026-07-03 from a rig where 'heat' at 70% cooled
    # the bath ~1.9 °C in 75s), which made heat/cool backwards on this Pi.
    # Verify empirically after changing: a wrong value makes the PID drive the wrong
    # way, so a call for heat cools and the loop runs away from its setpoint.
    PELTIER_DIR_INVERTED: bool = False

    # Data retention for API-generated run CSVs in bioreactor_v3/src/bioreactor_data/.
    # Old run files are pruned (oldest first) on startup and before each run so the
    # SD card can't fill up; runs are refused if free space drops below the floor.
    # Only touches files this API creates (*_peltier_schedule.csv / *_pid_run.csv).
    DATA_RETENTION_MAX_MB: int = 1000   # cap total size of API run files
    DATA_RETENTION_KEEP: int = 10       # always keep at least this many newest runs
    DATA_MIN_FREE_MB: int = 500         # refuse to start a run if free disk below this

    # Pi camera (rpicam-still snapshots via GET /api/camera/snapshot).
    # rotation/hflip/vflip/zoom are defaults; requests can override via query params.
    CAMERA_ENABLED: bool = True
    CAMERA_WIDTH: int = 1280
    CAMERA_HEIGHT: int = 720
    CAMERA_ROTATION: int = 0     # 0 or 180
    CAMERA_HFLIP: bool = False
    CAMERA_VFLIP: bool = False
    CAMERA_ZOOM: float = 1.0     # 1.0 = full frame; 2.0 = 2x centered digital zoom
    CAMERA_QUALITY: int = 90

    # Rolling sensor-history buffer (for the monitor's long-range plot).
    # Samples temp/ambient/current/OD continuously (independent of runs). Served at
    # GET /api/history from an in-memory HISTORY_WINDOW_H window; every sample is also
    # appended to a daily archive file history/YYYY-MM-DD.jsonl and kept for
    # HISTORY_RETENTION_DAYS (files older than that are pruned). Append-only, so a
    # year of data costs ~0.5 GB and only a few MB/day of SD writes.
    HISTORY_ENABLED: bool = True
    HISTORY_INTERVAL_S: int = 10        # sample period
    HISTORY_WINDOW_H: int = 24          # hours kept in memory + served to the frontend
    HISTORY_RETENTION_DAYS: int = 365   # days of daily archive files kept on disk

    # Stirrer (PWM only)
    STIRRER_PWM_PIN: int = 12
    STIRRER_PWM_FREQ: int = 1000
    STIRRER_DEFAULT_DUTY: float = 30.0

    # LED (PWM control)
    LED_PWM_PIN: int = 25
    LED_PWM_FREQ: int = 500

    # Ring Light (Neopixel via pi5neo SPI)
    RING_LIGHT_SPI_DEVICE: str = '/dev/spidev0.0'
    RING_LIGHT_COUNT: int = 32
    RING_LIGHT_SPI_SPEED: int = 800

    # ------------------------------------------------------------------------
    # Optical inputs: VOLTAGE SOURCES + OD MEASUREMENTS
    # (bioreactor_v3/src/optics.py, bioreactor_v3/docs/optics.md)
    # ------------------------------------------------------------------------
    # Every photodiode/ADC input, under a name of YOUR choosing. kind 'adc' = one ADS1115
    # channel A0-A3 (needs INIT_COMPONENTS['optical_density']); kind 'eyespy' = one ADS1114
    # eyespy board (needs INIT_COMPONENTS['eyespy_adc']). Each source: GET /api/voltage/<name>,
    # listed by GET /api/voltages, and its own run-CSV column '<name>_V' unless an OD
    # measurement consumes it. Shorthands: 'adc:A0', 'eyespy:0x49'.
    VOLTAGE_SOURCES: dict = {
        'pd_135':  {'kind': 'adc', 'channel': 'A0'},
        'pd_ref':  {'kind': 'adc', 'channel': 'A1'},
        'pd_90':   {'kind': 'adc', 'channel': 'A2'},
        'eyespy1': {'kind': 'eyespy', 'i2c_address': 0x49, 'i2c_bus': 1, 'gain': 1.0},
        'eyespy2': {'kind': 'eyespy', 'i2c_address': 0x4a, 'i2c_bus': 1, 'gain': 1.0},
    }

    # The four canonical OD measurements: enabled or not, and which source feeds each
    # (either kind). Enabled ones are IR-gated, logged as OD_<x>_V, served by
    # GET /api/od/state and plotted by the dashboard (the ONLY optical series it shows).
    # Shorthands: 'pd_135' (enabled, that source), False, True (enabled, source 'pd_<x>').
    OD_MEASUREMENTS: dict = {
        'OD_45':  {'enabled': False},
        'OD_ref': {'enabled': True, 'source': 'pd_ref'},
        'OD_90':  {'enabled': True, 'source': 'pd_90'},
        'OD_135': {'enabled': True, 'source': 'pd_135'},
    }

    # OD column the run-CSV EKF tracks: an OD measurement ('OD_135'), its suffix, or a source.
    EKF_OD_CHANNEL: str = 'OD_135'

    # IR-gated OD sampling (od_sampler.py). Every reading pulses the IR LED:
    #   LED on (OD_LED_POWER) -> settle (OD_SETTLE_S) -> read -> pause (OD_POST_READ_S) -> LED off.
    # Runs continuously (24/7) so the 24h OD history fills even when nobody's watching.
    # When both source kinds are present (ADS1115 channels, eyespy boards) they
    # interleave (one kind per pulse), so each kind samples at half the pulse rate. The whole gated measurement is
    # held under HARDWARE_LOCK (so nothing toggles the LED mid-read), which means the
    # lock is occupied for ~settle+read+post each pulse. The 1 Hz heater safety loop
    # shares that lock, so keep the settle short to bound lock occupancy: an IR LED +
    # ADS1115 stabilise in <20 ms, so 0.25 s is ample and keeps occupancy well under
    # the pulse period. led_power/enabled are also settable live via POST /api/od/sampling.
    OD_SAMPLE_ENABLED: bool = True
    OD_LED_POWER: float = 10.0       # IR LED % during each gated reading (frontend: 1–20%)
    OD_SETTLE_S: float = 0.25        # settle after LED-on before reading (bounds lock occupancy)
    OD_POST_READ_S: float = 0.05     # brief pause after reading before LED-off
    OD_PULSE_PERIOD_S: float = 5.0   # LED-pulse period. Pulses every 5s; with both sources
                                     # interleaved each channel samples every 10s (0.1 Hz),
                                     # aligning with the 10s history archive so every point
                                     # gets a fresh OD reading. ~5x fewer IR pulses than 1 Hz,
                                     # which also cuts the ring-light SPI-noise glitching.

    # LEGACY optical form (honoured when VOLTAGE_SOURCES / OD_MEASUREMENTS are absent, with
    # the old column names OD_<chan>_V and Eyespy_<board>_raw/_V preserved):
    #   OD_ADC_CHANNELS: dict[str, str] = {'135': 'A0', 'Ref': 'A1', '90': 'A2'}
    #   EYESPY_ADC: dict = {'eyespy1': {'i2c_address': 0x49, 'i2c_bus': 1, 'gain': 1.0},
    #                       'eyespy2': {'i2c_address': 0x4a, 'i2c_bus': 1, 'gain': 1.0}}

    # CO2 Sensor (Senseair K33, reads ppm). Speaks its own 4-byte ReadRAM frames,
    # not the Atlas EZO protocol — it does NOT answer a plain I2C receive-byte, so
    # it won't show up in a default `i2cdetect -y -r 1` scan even when healthy.
    CO2_SENSOR_TYPE: str = 'sensair_k33'
    CO2_SENSOR_I2C_ADDRESS: Optional[int] = 0x68   # verified on the bus 2026-08-27
    # Dedicated K33 bus: SDA GPIO23 (pin 16), SCL GPIO24 (pin 18).
    # Requires the i2c-gpio boot overlay; see bioreactor_v3/docs/senseair_k33.md.
    # Atlas CO2 rigs should use bus 1 instead.
    CO2_SENSOR_I2C_BUS: int = 3

    # O2 Sensor (Atlas Scientific EZO-O2, reads %)
    O2_SENSOR_I2C_ADDRESS: Optional[int] = 0x6C    # verified on the bus
    O2_SENSOR_I2C_BUS: int = 1

    # Atlas gas sampler (gas_sampler.py): a background thread reads CO2 + O2 every
    # GAS_SAMPLE_PERIOD_S and caches the latest for /api/state and the history buffer.
    # Each Atlas "R" read needs a ~GAS_READ_DELAY_MS processing wait; the I2C lock is
    # released during that wait (write -> sleep -> read) so the slow sensors don't
    # starve the bus. Reads are slow, so poll gently — the gases change slowly anyway.
    GAS_SAMPLE_PERIOD_S: float = 5.0
    GAS_READ_DELAY_MS: int = 1500

    # Ambient Temperature Sensor (NXP PCT2075, I2C) — reads in °C
    AMBIENT_TEMP_I2C_ADDRESS: int = 0x37
    AMBIENT_TEMP_I2C_BUS: int = 1

    # Peltier Current Sensor (TI INA228 current monitor, I2C) — reads in Amps.
    # Current is derived from the shunt voltage: I = V_shunt / INA228_SHUNT_OHMS,
    # so INA228_SHUNT_OHMS must be CALIBRATED to your board's shunt resistor.
    PELTIER_CURRENT_I2C_ADDRESS: int = 0x40
    PELTIER_CURRENT_I2C_BUS: int = 1
    INA228_SHUNT_OHMS: float = 0.015  # Adafruit INA228 breakout default; calibrate per board

    # Pumps (ticUSB protocol)
    PUMPS: dict[str, dict[str, Union[str, int, float]]] = {
        'inflow': {
            # Swapped in 2026-08-27; steps_per_ml below was calibrated for the
            # previous pair (00473510/00473504) — recalibrate for these units.
            'serial': '00473508',
            'step_mode': 2,
            'current_limit': 32,
            'direction': 'forward',
            'steps_per_ml': 41493129.7,
        },
        'outflow': {
            'serial': '00473498',
            'step_mode': 2,
            'current_limit': 32,
            'direction': 'forward',
            'steps_per_ml': 41493129.7,
        },
    }

    # Timed-dose pump runs (POST /api/pumps/run + program 'pump' tracks).
    # A "run" cycles every `interval` seconds: outflow ON for interval*duty, inflow
    # ON for PUMP_INFLOW_TIME_RATIO*interval*duty (duty is 0-1 internally), both at
    # PUMP_RUN_ML_PER_SEC. CALIBRATE PUMP_RUN_ML_PER_SEC to your pumps (with
    # steps_per_ml above); it sets how much volume each ON-second moves.
    PUMP_RUN_ML_PER_SEC: float = 0.0035    # default flow rate; overridable per run
    PUMP_INFLOW_TIME_RATIO: float = 0.95   # inflow runs 0.95x the outflow ON-time

    # Relays (GPIO via lgpio). Named relays -> BCM pins. The API addresses relays by
    # name; "closed" = energized (driver ON), "open" = de-energized (OFF, the boot
    # state). RELAY_ACTIVE_LOW handles module polarity (True = pin LOW energizes).
    RELAYS: dict[str, int] = {
        'CO2': 5,
        'N2': 6,
        'relay_3': 13,
        'relay_4': 19,
    }
    RELAY_ACTIVE_LOW: bool = True

    # Per-relay safety guard. A "dose" = closing (energizing) the relay. A guarded
    # relay's close auto-reverts to open after max_duration_s and is rate-limited to one
    # dose per min_interval_s. If co2_max_ppm is set, a dose is also refused when the
    # current CO2 reading is above it (or unreadable); omit it for no CO2 gate. Enforced
    # in relay_controller.py (Pi), so it applies to both the manual API and program tracks.
    RELAY_SAFETY: dict = {
        'CO2': {
            'max_duration_s': 1.0,     # a dose (closed) auto-reverts to open after this
            'min_interval_s': 60.0,    # at most one dose per this window
            # Refuse a dose if CO2 is above this (or unreadable). Derived from
            # CO2_SENSOR_TYPE above: 7500 for the Atlas, 95000 for the K33.
            'co2_max_ppm': _co2_max_ppm_for(CO2_SENSOR_TYPE),
        },
        'N2': {
            'max_duration_s': 1.0,     # same duration + frequency limits as CO2...
            'min_interval_s': 60.0,    # ...but no co2_max_ppm -> no CO2-level gate
        },
    }
