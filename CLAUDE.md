# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## What This Is

The **on-rig control layer**: a FastAPI server that runs on the bioreactor's
Raspberry Pi, exposes the hardware as REST endpoints, and runs the 1 Hz control
loop, the safety cutoffs, the background sensor samplers and the history archive.

Hardware drivers live in the `bioreactor_v3` git submodule — this repo never
talks to a bus directly, it calls `bioreactor_v3/src/io.py`.

Everything that must keep working when the network drops lives here, on the Pi.
The remote dashboard (`bioreactor_server`, on a droplet) is a pure proxy — it
monitors and issues commands, and can be rebooted mid-run without consequence.

> **Repo name:** GitHub renamed this repo to **`bioreactor_api`** (2026-09-10).
> The old `bioreactor_website` name still redirects, and the local checkout
> directory and the `origin` remote may still use it.

## File Layout

```
.
├── .gitmodules                  # submodule config (url: ../bioreactor_v3)
├── CLAUDE.md                    # this file
├── README.md                    # user-facing quick start
└── bioreactor-api/
    ├── bioreactor_v3/           # submodule — the hardware drivers
    │   └── src/{bioreactor,components,io,optics,utils,config_default}.py
    ├── API.md                   # REST reference (routes, bodies, error codes)
    ├── main.py                  # FastAPI app: endpoints, lifespan/startup wiring
    ├── control.py               # RunController — the 1 Hz control loop + HARDWARE_LOCK
    ├── program.py               # multi-track program parser + timeline expander
    ├── history.py               # rolling 24h buffer + 365-day daily JSONL archive
    ├── od_sampler.py            # IR-gated optical-density sampler thread
    ├── gas_sampler.py           # CO2 / O2 sampler thread
    ├── pump_controller.py       # timed media-exchange dosing thread
    ├── relay_controller.py      # relay open/closed/toggle + timed toggles + dose guards
    ├── camera.py                # rpicam-still JPEG snapshots
    ├── auth.py                  # bearer-token auth + per-IP rate limiting
    ├── config.example.py        # tracked template — copy to config.py per rig
    ├── config.py                # GITIGNORED, per-rig: INIT_COMPONENTS, pins, tuning
    └── requirements.txt         # fastapi, uvicorn, slowapi, ticlib
```

`main.py` is endpoints and startup wiring only. The behaviour lives in the peer
modules — when something is wrong with a *run*, read `control.py`, not `main.py`.

## Threading Model — read this before touching hardware access

Up to six threads touch the I2C/GPIO buses concurrently: the five background
samplers/controllers (`od_sampler`, `gas_sampler`, `pump_controller`,
`relay_controller`, `history`), the `RunController` loop during a run, and any
FastAPI request thread.

**Every hardware read or write must be inside `control.HARDWARE_LOCK`** — a
module-level `threading.RLock` (re-entrant, so one tick can nest peltier + sensor
calls). `main.py` acquires it around its own reads/writes; the samplers take it
via the `hw_lock=` they are configured with.

Two conventions that exist for good reasons — don't "simplify" them away:

- **Slow reads release the lock while waiting.** An Atlas EZO gas read needs a
  ~1.5 s processing wait; `gas_sampler` writes `"R"` under the lock, sleeps
  *without* it, then reads the result under the lock again. Holding it across the
  wait starves every other bus user.
- **The control tick never does a slow read.** It pulls OD/CO2/O2 from the
  samplers' caches (`od_latest_fn`, `gas_latest_fn`), fetched *outside*
  `HARDWARE_LOCK` because those getters take the samplers' own locks — taking
  them in the other order is a lock-ordering inversion.

## Run Control

One loop, three modes, all in `control.RunController` at `SAMPLE_PERIOD_S = 1.0`:

| Mode | Started by | Behaviour |
|---|---|---|
| `schedule` | `POST /api/run/schedule` | open loop; steps through `duty,direction,hold_s` CSV rows |
| `pid` | `POST /api/run/pid` | closed loop; holds one setpoint via `temperature_pid_controller` |
| `program` | `POST /api/run/program` | parallel per-device tracks (`ring`/`temp`/`heater`/`stirrer`/`pump`/`relay`/`od`) |

Only one run at a time (`409` otherwise). Each run writes its own CSV under
`bioreactor_v3/src/bioreactor_data/`; old API-generated run files are pruned
before each run, and a run is refused (`507`) if free disk is below the floor.

**The PID math is not in this repo.** `temperature_pid_controller` lives in
`bioreactor_v3/src/utils.py`; `control.py` only decides *when* to call it and
with what temperature. PID state (`_temp_integral`, `_temp_last_error`,
`_temp_last_time`, `_temp_last_derivative`) is stored as attributes on the
`Bioreactor` instance and persists across calls — `start_pid` / `start_program`
delete them so each run starts with a clean integrator.

**Safety supervision runs before actuation, every tick.** The loop reads the
temperature once, runs the checks, and only then lets the PID drive the peltier
from that *same* reading — never a second independent read the supervisor didn't
see. Aborts (peltier off) on: NaN temperature for 15 consecutive samples, bath
outside [2, 60] °C, free disk below the floor, or 15 consecutive tick exceptions.
Any unhandled tick exception cuts peltier power immediately.

While a schedule/PID run is active, manual `POST /api/peltier_driver/control`
returns `409`. During a *program* run it is allowed and registers as an override
(`runner.note_override`), which suspends that device's track until its next step.

## Configuration

`config.py` is **gitignored and per-rig**; `config.example.py` is the tracked
template. It is a **standalone `class Config`** — it does *not* inherit from
`bioreactor_v3/src/config_default.py`.

That has a sharp edge: any setting the template omits falls back to whatever
hardcoded default each call site passes to `getattr(config, NAME, <default>)`,
and those fallbacks don't always match `config_default.py`. Currently 15 settings
are in `config_default.py` but not in the template. If you add a driver setting,
add it to **both**, or verify the call-site fallback is what you want on a rig.

`INIT_COMPONENTS` decides what exists: only enabled components get endpoints, and
disabled or failed-to-initialize ones return `503`. `GET /api/capabilities` lists
what is actually live.

## Simulation vs Real Mode

- **`HARDWARE_MODE=simulation`** (the default) — no `Bioreactor` instance.
  Actuator endpoints track state in `sim_state`, sensor endpoints return plausible
  random values, the run loop advances schedules/programs in software. Note the
  PID does **not** run in simulation (there is no hardware to drive), and no run
  CSV is written. `config.py` must still exist — it is imported for
  `INIT_COMPONENTS` either way.
- **`HARDWARE_MODE=real`** — instantiates `Bioreactor(Config())` on startup.
  Failure here is **deliberately fatal**: the server refuses to start rather than
  silently degrade to simulated readings, because a run driven by fake sensor data
  is worse than no run. Individual components may still fail; `_initialized`
  records those and their endpoints return `503`.

## Dev Commands

```bash
git submodule update --init                      # the submodule is NOT checked out by default
cp bioreactor-api/config.example.py bioreactor-api/config.py
ln -sfn ../../config.py bioreactor-api/bioreactor_v3/src/config.py

cd bioreactor-api
pip install -r requirements.txt                  # API deps
pip install -r bioreactor_v3/requirements.txt    # driver deps (numpy, adafruit, lgpio, ...)

# simulation (default) — no hardware needed
HARDWARE_MODE=simulation uvicorn main:app --port 9000 --reload

# real hardware (on the Pi)
API_KEY=<secret> HARDWARE_MODE=real uvicorn main:app --host 0.0.0.0 --port 9000

open http://localhost:9000/docs                  # live schema — authoritative
```

On the rig the Pi runs this as the `bioreactor-api.service` systemd unit.

## Adding a New Hardware Component

1. **If the driver doesn't exist**: add it to the `bioreactor_v3` submodule —
   `components.py` for init, `io.py` for read/write. Commit there, then bump the
   submodule pointer here.
2. **Enable it**: set `INIT_COMPONENTS['new_thing'] = True` in `config.py` plus
   any pin/address settings, and add the same to `config.example.py`.
3. **Add endpoints in `main.py`**: a Pydantic model + `@app.post` + `@app.get`,
   following the LED / peltier / stirrer pattern. Wrap every hardware call in
   `with HARDWARE_LOCK:` and guard with `require_component('new_thing')`.
4. **If it needs background sampling or timing**, put that in its own module with
   its own thread (like `gas_sampler.py`), configured from `lifespan` — not in a
   request handler.
5. **Update `API.md`.**

Do **not** duplicate hardware drivers in `bioreactor-api/`. The submodule is the
source of truth.

## Submodule Notes

`bioreactor_v3` is a sibling repo (`.gitmodules` url is `../bioreactor_v3`),
pinned by SHA. After cloning, `git submodule update --init`.

Because `src/__init__.py` in the submodule imports `.config`, the setup symlinks
the submodule's `src/config.py` at this repo's `config.py`, so both layers read
one file.

**Simulation works with an empty submodule dir.** Every module here imports only
the standard library, and the one piece of the submodule simulation touches
(`src/optics.py`, pure stdlib) is loaded by path via `_load_optics_module()` —
which falls back to a **sibling `bioreactor_v3` checkout** when the submodule dir
is empty. So don't add a submodule-init requirement to the simulation path; that
fallback is deliberate.

**Deploy ordering matters.** The dashboard tolerates both old and new Pi API
shapes, so deploy **bioreactor_server first**, then this repo together with its
submodule bump. The reverse order breaks the dashboard's OD tab.

## Testing

There is currently **no test suite in this repo** — the only tests in the three
repos are `bioreactor_v3/test_optics.py`. Schedule parsing (`parse_schedule`),
program parsing (`program.py`), retention (`prune_run_files`) and the abort paths
in `control.py` are all pure logic and straightforward to test; adding coverage
there is worthwhile before changing them, since this code drives a heater
unattended.
