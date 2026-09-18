# Bioreactor API Reference

REST interface to the bioreactor hardware, served by `main.py` on the Pi
(port 9000, bound to `0.0.0.0`).

## Base URL

There is no fixed public hostname. Pick whichever path you have:

```bash
BASE=http://mori:9000                 # over the tailnet (normal path — Pi stays private)
BASE=http://localhost:9000            # on the Pi itself, or a local simulation run
BASE=https://<name>.trycloudflare.com # only if a cloudflared tunnel is running
```

All examples below use `$BASE` and `$API_KEY`.

## Authentication

Every route — `/health` included — requires a bearer token when the Pi API is
started with `API_KEY` set:

```
Authorization: Bearer <API_KEY>
```

If `API_KEY` is unset the server logs a warning and skips auth entirely (dev
mode only — never do this on a rig reachable from anything but localhost).

Rate limit: `RATE_LIMIT` env var, default **100/minute**, keyed per client IP
(Cloudflare `CF-Connecting-IP` / `X-Forwarded-For` aware).

---

## System

### Health check
```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/health
```
Response: `{"status": "healthy", "hardware_mode": "real", "hardware_available": true, "initialized_components": {...}}`

### List capabilities
Which components came up, and the endpoint pattern for each. Components that
failed to initialize (or are `False` in `INIT_COMPONENTS`) are absent here and
return `503` on their routes.
```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/capabilities
```

### Aggregate state
One call returns everything the live monitor needs — poll this at ~1 Hz instead
of hitting each endpoint separately. Unavailable components report `null`.
```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/state
```
Response keys: `timestamp`, `temperature`, `ambient_temp`, `peltier_current`
(signed: negative while heating), `peltier` `{duty_cycle, direction, active}`,
**`run`** (the full run status, same object as `GET /api/run/status`), `co2`,
`o2`, `od`, `od_measurements`, `od_available`, `od_sampling`, `voltages`, `led`,
`ring`, `stirrer`, `pumps`, `relays`.

---

## Actuators

### LED

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/led/state

curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"power": 50}' $BASE/api/led/control
```
`power`: 0–100.

---

### Peltier (manual / open loop)

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/peltier_driver/state

curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50, "direction": "heat"}' $BASE/api/peltier_driver/control
```
`duty_cycle`: 0–100. `direction`: `heat` | `cool` | `forward` | `reverse`
(`forward` == `cool`, `reverse` == `heat`).

While a **schedule** or **PID** run is active this returns `409` — stop the run
first. During a **program** run it is allowed and becomes an override: the
program leaves the peltier alone until that track's next step reclaims it.

> Direction vocabulary differs between the two reads: `/api/peltier_driver/state`
> reports `forward`/`reverse`, `/api/state` reports `cool`/`heat`. Same underlying
> flag.

---

### Stirrer

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/stirrer/state

curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50}' $BASE/api/stirrer/control
```

---

### Ring light

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/ring_light/state

# whole ring
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"red": 30, "green": 30, "blue": 30}' $BASE/api/ring_light/control

# one pixel
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"red": 255, "green": 0, "blue": 0, "pixel_index": 0}' $BASE/api/ring_light/control
```
RGB 0–255; `pixel_index` omitted/null = all pixels.

---

### Pumps

Two interfaces: direct velocity control, and the timed media-exchange regime the
dashboard and program tracks use.

**Direct velocity** (mL/s, negative = reverse):
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"pump_name": "inflow", "velocity": 1.5}' $BASE/api/pumps/control
```

> `GET /api/pumps/state` is a **stub in real mode** — it returns
> `{"pump_name": "all", "velocity": 0.0, "active": <pumps initialized>}` rather
> than reading the hardware. For the actual dosing regime use the `pumps` field of
> `GET /api/state` (that one is `pump_controller.status()`).

**Timed dosing** — every `duration` seconds, run outflow for `duration × duty`
and inflow for `0.95 × duration × duty`, so each cycle nets a small removal:
```bash
# repeat until stopped
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duration": 600, "duty_cycle": 10, "flow_rate": 1.0}' $BASE/api/pumps/run

# a single cycle, then stop
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"duration": 600, "duty_cycle": 10}' $BASE/api/pumps/dose

# both pumps off
curl -X POST -H "Authorization: Bearer $API_KEY" $BASE/api/pumps/stop
```
`duration` > 0 (seconds), `duty_cycle` 0–100 (0 stops), `flow_rate` optional
(mL/s; omit to keep the current value). All three count as a manual override of a
program's `pump` track until its next step.

---

### Relays

Relays are addressed by the names in `config.RELAYS`. `open` = de-energized
(the boot state), `closed` = energized.

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/relays/state

# open | closed | toggle
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"relay_name": "relay_1", "command": "closed"}' $BASE/api/relays/control

# command now, then toggle after `duration` seconds (one-shot timed pulse)
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"relay_name": "relay_1", "command": "closed", "duration": 30}' $BASE/api/relays/timed
```
Response: `{"status": "success", "states": {"relay_1": "closed", ...}, "pending": {"relay_1": 28.4}, "guards": {...}}`

Relays listed in `config.RELAY_SAFETY` are dose-guarded (auto-revert, rate limit,
CO₂ ceiling); a command the guard refuses returns `409`. An unknown relay name is
`404`; an invalid command is `422`.

---

## Sensors

| Endpoint | Response field |
|---|---|
| `GET /api/temp_sensor/state` | `temperature` (°C) |
| `GET /api/ambient_temp/state` | `temperature` (°C) |
| `GET /api/peltier_current/state` | `current` (A, unsigned — `/api/state` signs it) |
| `GET /api/co2_sensor/state` | `co2_ppm`, `acquired_at` (Unix seconds), `sample_age_s` (from the background gas sampler cache; timestamps null before acquisition) |
| `GET /api/o2_sensor/state` | `o2_percent` (from the background gas sampler cache) |

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/temp_sensor/state
```
Response: `{"status": "success", "temperature": 23.5, "unit": "celsius"}`

---

### Optical density (OD measurements)

The four canonical measurements `OD_45` / `OD_ref` / `OD_90` / `OD_135` are
configured in `config.py` (`OD_MEASUREMENTS`), each fed by a named voltage source
(`VOLTAGE_SOURCES`, an ADS1115 channel or an eyespy board). Values come from the
IR-gated background sampler; `null` = not measured yet / sampling off. See
`bioreactor_v3/docs/optics.md` for the configuration model.

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/od/state
```
Response: `{"status": "success", "od": {"OD_ref": 0.81, "OD_90": 1.08, "OD_135": 0.50}, "measurements": {"OD_ref": "pd_ref", ...}, "available": true, "sampling": {"enabled": true, "led_power": 10, "kinds": ["adc", "eyespy"], ...}, "unit": "volts"}`

`503` if no OD measurements are configured. The same `od`, `od_measurements`,
`od_available` and `od_sampling` fields appear in `GET /api/state`.

**IR-gated sampling control** — on/off plus the LED power used for each gated
reading (the IR LED only lights briefly per reading, never steady-on):
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"enabled": true, "led_power": 10}' $BASE/api/od/sampling
```
Both fields optional, but at least one required (`400` otherwise). Counts as a
manual override of a program's `od` track until its next step.

---

### Voltage sources

Every configured source by name, with its kind, the OD measurements it feeds, the
last IR-gated reading (`gated`) and an instantaneous un-gated reading taken now
(`live`).

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/voltages
curl -H "Authorization: Bearer $API_KEY" $BASE/api/voltage/pd_135
```
Response (single): `{"status": "success", "name": "pd_135", "kind": "adc", "component": "optical_density", "od": ["OD_135"], "gated": 0.504, "live": 0.51, "unit": "volts"}`

`404` for an unknown name, `503` if the source's hardware component is down.

**Deprecated aliases**, kept for old scripts: `GET /api/optical_density/state`
and `GET /api/eyespy_adc/state` return the un-gated voltages of the ADS1115 /
eyespy sources as a positional list plus `names`. Prefer `/api/od/state` or
`/api/voltages`.

---

## Run control (schedule / PID / program)

One control loop on the Pi, at 1 Hz, in one of three modes. It runs **next to the
hardware** so a dropped network link can never strand the heater — the safety
supervision is local regardless of the tunnel or the droplet.

**Safety cutoffs** (all abort the run and cut peltier power):
- bath temperature reads NaN for 15 consecutive samples
- bath temperature leaves the **[2 °C, 60 °C]** window
- free disk falls below `DATA_MIN_FREE_MB` while a schedule/program owns its automatic CSV
- 15 consecutive control-loop exceptions

Only one run at a time — starting a second returns `409`. A run is refused with
`507` if a schedule/program needs to open a CSV and free disk is already below
the floor after pruning. PID control does not require a CSV or free-disk check.

### Run a schedule (open loop)

CSV body of `duty,direction,hold_s` rows — same format as `heater_gui` /
`hardware_testing/peltier_schedule_example.csv`. `#` comments and a header row
are allowed. Duty is capped per direction (heat ≤ `PELTIER_MAX_DUTY_HEAT`,
cool ≤ `PELTIER_MAX_DUTY_COOL`).

```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: text/csv" \
  --data-binary $'duty,direction,hold_s\n50,cool,120\n0,heat,30\n70,heat,60\n' \
  $BASE/api/run/schedule
```

### Run a PID setpoint

```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"setpoint": 37.0, "kp": 12.0, "ki": 0.015, "kd": 0.0}' \
  $BASE/api/run/pid
```
Gains default to `kp=12.0, ki=0.015, kd=0.0`. The integrator is cleared at the
start of every run. Requires both `peltier_driver` and `temp_sensor`.
Starting or stopping PID does **not** start or stop manual CSV recording.

### Run a multi-device program

A JSON document of parallel per-device tracks (`ring` / `temp` / `heater` /
`stirrer` / `pump` / `relay` / `od`), durations as bare seconds or `s`/`m`/`h`/`d`.
See the module docstring in `program.py` for the full grammar.

```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  --data-binary @program.json $BASE/api/run/program
```

Validate + get the expanded timeline without running anything (for a Gantt
preview). Returns `200` with `{"valid": false, "error": "..."}` on a bad program
so an editor can show it inline:
```bash
curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  --data-binary @program.json $BASE/api/run/program/preview
```

When a program opens its own run CSV, its uploaded JSON is saved beside it
(`*_program.json`) for reproducibility. If manual recording is already active,
the program uses that CSV; retain the uploaded program JSON separately.

### Status and stop

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/run/status

curl -X POST -H "Authorization: Bearer $API_KEY" $BASE/api/run/stop
```
Status: `{"active": true, "mode": "schedule", "completed": false, "aborted": false, "abort_reason": null, "data_file": "20260910_142530_peltier_schedule.csv", "elapsed_s": 184.0, "step": 2, "total_steps": 3, "step_remaining_s": 41.2, "last": {"temperature": 24.2, "ambient_temp": 24.4, "peltier_current": -0.36, "peltier_duty": 50.0, "direction": "heat"}}`

Mode-specific extras: `schedule` adds `step` / `total_steps` / `current_step` /
`remaining_steps` / `step_remaining_s`; `pid` adds `setpoint` / `gains`;
`program` adds `program_name` / `setpoint` / `overrides` / `remaining_s` /
`tracks[]`. `stop` is safe to call when idle and always leaves the peltier off.

---

## History

A background sampler logs the monitor signals plus actuator/control state
continuously — independent of runs — into a 24 h in-memory ring buffer backed by
a 365-day append-only daily archive on the Pi (`history/YYYY-MM-DD.jsonl`).

```bash
# rolling window; ?since=<ms> for cheap incremental polling
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/history?since=1757500000000"

# arbitrary range, read from the on-disk archive (downsampled server-side)
curl -H "Authorization: Bearer $API_KEY" "$BASE/api/history/range?start=<ms>&end=<ms>"
```
Both return `{"status": "success", "interval_s": 10, "od_measurements": {...}, "od_available": true, "points": [...]}` (`interval_s` = `HISTORY_INTERVAL_S`, default 10). `/api/history` also returns `archive_earliest_ms`.

---

## Data files

### Independent CSV recording

The dashboard's **Start CSV** button (left of **Download latest CSV**) starts
recording on the Pi. It changes to **Stop CSV** while recording. This works with
CO₂ control, temperature PID, or neither controller running. Browser disconnects
and stopping a controller do not stop manually started recording. Recording stops
on an explicit recording-stop request, an API shutdown/restart, or a storage error.
A recording error is exposed in status and does not stop independent controllers.
Each Start creates a new `*_recording.csv`; repeated Start requests are idempotent.

```bash
curl -X POST -H "Authorization: Bearer $API_KEY" $BASE/api/data/recording/start
curl -H "Authorization: Bearer $API_KEY" $BASE/api/data/recording
curl -X POST -H "Authorization: Bearer $API_KEY" $BASE/api/data/recording/stop
```

Status is also returned as `recording` in `/api/state`: `active`, `owner`
(`manual` or `run`), `data_file`, `elapsed_s`, `error`, and `simulation`.
Simulation exposes the same controls but does not create files. Start returns
`507` if storage is insufficient. View-only dashboard users cannot start/stop.

Temperature PID and direct CO₂ control do not automatically record CSV.
Schedules/programs retain automatic recording: their file closes when the run
ends, unless manual recording was already active, in which case that same manual
file continues. Stop CSV can also stop a schedule/program's recording without
stopping the run. An open recording is never pruned.

### Contents and sampling

Rows are recorded nominally once per second (slower if hardware reads take longer).
During a temperature/program run, recording shares its measurement tick; while
idle, a dedicated recorder samples without running temperature supervision or
actuation. Gas and OD values come from background sampler caches and may repeat
between sensor updates. Failed/missing readings can be NaN or blank.

Columns follow the rig's configured labels and initialized components:

- Local timestamp `time` and seconds since recording began, `elapsed_time`.
- Bath and ambient temperature (°C), CO₂ (ppm), O₂ (%).
- Configured OD channels and unmapped voltage sources (including EyeSpy/ADC).
- Peltier current (A), duty (%) and direction flag (`0` heat, `1` cool).
- Ring-light red, green and blue values.
- Relay states and cumulative `relay_<name>_closed_s`, including CO₂ pulses.
- Cumulative `pump_<name>_time_s` for each configured pump.
- When available, EKF OD, growth-rate and doubling-time estimates and their
  uncertainties (`ekf_*`); these are derived estimates, not extra sensor readings.

Cumulative relay/pump counters are since API startup, not since recording began;
use differences between rows for doses during a recording. CSV does not include
controller setpoints, MPC predictions/correction state, or stirrer duty. Continuous
dashboard history remains a separate archive and is unaffected by the CSV button.

### Downloads and retention

Run CSVs live under `bioreactor_v3/src/bioreactor_data/` and are listed
recursively, newest first.

```bash
curl -H "Authorization: Bearer $API_KEY" $BASE/api/data/list
curl -H "Authorization: Bearer $API_KEY" -OJ $BASE/api/data/latest
```

API-generated run files (`*_peltier_schedule.csv`, `*_pid_run.csv`,
`*_program.csv`, `*_recording.csv`) are pruned oldest-first on startup and before
opening a recording to stay
under `DATA_RETENTION_MAX_MB`, always keeping at least `DATA_RETENTION_KEEP` of
the newest. Historical and committed data is never touched.

---

## Camera

```bash
curl -H "Authorization: Bearer $API_KEY" -o snap.jpg \
  "$BASE/api/camera/snapshot?rotation=180&zoom=1.5"
```
Optional query params override the configured defaults: `rotation` (0 or 180),
`hflip`, `vflip`, `zoom` (≥ 1.0, centered digital zoom). Returns `image/jpeg`.
`503` if the camera is disabled or unavailable.

---

## Error codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Malformed body — bad schedule/program CSV or JSON, empty OD sampling request, bad `rotation` |
| 401 | Missing or malformed authorization header |
| 403 | Invalid API key |
| 404 | No data file / unknown voltage source / unknown relay name |
| 409 | Manual peltier control blocked (schedule or PID run active) · run already active · relay safety guard refused the command |
| 422 | Body failed validation (out-of-range field, invalid relay command) |
| 429 | Rate limit exceeded (default 100 req/min) |
| 503 | Component not available (disabled in `INIT_COMPONENTS`, or failed to initialize) |
| 507 | Not enough free disk to start a run |

---

## Interactive docs

FastAPI serves a live, always-accurate schema — useful when this file and the
code disagree:

```
$BASE/docs
```


### CO₂ feedback controller

- `POST /api/co2/control`: `{"target_percent": 2, "duration_s": 3600}` starts or
  updates the on-Pi MPC. Use exactly one of `target_percent` or legacy `target_ppm`.
  Percent means percent by volume: **2 = 20,000 ppm**. Numeric finite values only;
  target must be positive and below 9.5%, subject to stricter rig limits. Duration
  defaults to one hour. Positive durations are capped at seven days or the lower
  configured trial maximum; zero requests indefinite operation when permitted. Positive-duration trial updates never extend a timed deadline; an explicitly
  permitted zero-duration update removes it.
- `POST /api/co2/stop`: immediate stop and valve OFF; releases/suspends a program's
  CO₂ track until its next step, without stopping other devices.
- `GET /api/co2/controller`: `active`, `owner`, `fault`, `target_ppm`, `target_percent`,
  `remaining_s`, `restart_wait_s`, measurement recovery, uncertainty and forecasts.
  `limits` gives `trial_target_max_percent` (inclusive when present),
  `target_below_percent` (exclusive) and `max_duration_s`.
- Program step: `{"co2": {"percent": 2}}`; legacy `{"co2": 20000}` remains ppm.
  `{"co2": false}` stops control. Provisional profiles require finite program duration unless
  `trial.allow_indefinite` is explicitly enabled.

Invalid request shapes return 422; invalid profile targets/durations return 400;
competing valve ownership or restart settling return 409. Authentication and relay
safety guards apply. See [CO₂ setup and examples](../docs/co2_mpc.md).

CO₂ control accepts `duration_s: 0` for indefinite operation, for example
`{"target_percent": 2, "duration_s": 0}`. Check `limits.allow_indefinite` in controller
status: validated profiles permit this; provisional profiles require the explicit
`CO2_MPC['trial']['allow_indefinite'] = True` setting. Active indefinite runs report
`indefinite: true` and `remaining_s: null`. Stop and fault behavior are unchanged.
