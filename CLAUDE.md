# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## What This Is

A minimal FastAPI server that exposes bioreactor hardware as REST endpoints.
Hardware drivers live in the `bioreactor_v3` git submodule — this repo only
provides the HTTP wrapper.

**Step 1 of a step-by-step rebuild.** Intentionally small: 3 files + a
submodule. No Docker, no queue, no web UI, no database.

## File Layout

```
.
├── .gitmodules                # submodule config
├── CLAUDE.md                  # this file
├── README.md                  # user-facing quick start
└── bioreactor-api/
    ├── bioreactor_v3/         # submodule (url: ../bioreactor_v3)
    │   └── src/
    │       ├── bioreactor.py  # Bioreactor class, reads INIT_COMPONENTS
    │       ├── components.py  # COMPONENT_REGISTRY + init_* functions
    │       ├── io.py          # set_led, get_temperature, read_co2, etc.
    │       └── config_default.py
    ├── config.example.py      # tracked template — copy to config.py per rig
    ├── config.py              # GITIGNORED, per-rig: INIT_COMPONENTS + pins
    ├── main.py                # FastAPI app + all endpoints
    └── requirements.txt       # fastapi, uvicorn
```

## How Endpoints Work

Every hardware component follows the same pattern:

- **Actuators**: `POST /api/<name>/control` + `GET /api/<name>/state`
- **Sensors**:   `GET  /api/<name>/state`

Only components with `INIT_COMPONENTS[name] = True` in `config.py` get endpoints.
Disabled components return `503`. `GET /api/capabilities` lists what's live.

Endpoints call functions from `bioreactor_v3/src/io.py` directly — e.g.
`POST /api/led/control` → `io.set_led(bioreactor, power)`.

## Dev Commands

```bash
cd bioreactor-api
pip install -r requirements.txt

# simulation mode (default) — no hardware needed
HARDWARE_MODE=simulation uvicorn main:app --port 9000 --reload

# real hardware mode (on a Pi with GPIO/I2C)
HARDWARE_MODE=real uvicorn main:app --port 9000

# interactive docs
open http://localhost:9000/docs
```

## Adding a New Hardware Component

1. **If the driver doesn't exist yet**: add it to the `bioreactor_v3`
   submodule (`components.py` for init, `io.py` for read/write).
2. **Enable it here**: set `INIT_COMPONENTS['new_thing'] = True` in
   `bioreactor-api/config.py`, plus any pin/address config it needs.
3. **Add endpoints in `main.py`**: a Pydantic model + `@app.post` + `@app.get`,
   following the same pattern as LED / peltier / stirrer.

Do **not** duplicate hardware drivers in `bioreactor-api/`. The submodule is
the source of truth.

## Simulation vs Real Mode

- `HARDWARE_MODE=simulation` — default. No `Bioreactor` instance. Actuator
  endpoints track state in a dict, sensor endpoints return plausible random
  values. Lets you develop/test the API without a Pi.
- `HARDWARE_MODE=real` — instantiates `Bioreactor(Config())` on startup.
  Individual components can still fail to initialize (missing sensor, wrong
  pin, etc.) — `_initialized` dict tracks which succeeded; failed ones get
  no endpoints.

## Submodule Notes

`bioreactor_v3` is a sibling repo: `.gitmodules` url is `../bioreactor_v3`.
After cloning this repo, run `git submodule update --init`.
