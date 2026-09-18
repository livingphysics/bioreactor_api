# Bioreactor API

The control layer that runs on the bioreactor's Raspberry Pi. A FastAPI server
that exposes the hardware as REST endpoints, and runs the 1 Hz control loop,
safety cutoffs, background sensor samplers and history archive locally — so a
dropped network link can never strand the heater.

Hardware drivers live in the [`bioreactor_v3`](../bioreactor_v3) submodule; the
remote dashboard lives in [`bioreactor_server`](../bioreactor_server) and is a
pure proxy onto this API.

- **[`bioreactor-api/API.md`](bioreactor-api/API.md)** — REST reference: routes, bodies, error codes.
- **[`CLAUDE.md`](CLAUDE.md)** — architecture, threading model, run control, how to extend.

> GitHub renamed this repo to **`bioreactor_api`** (2026-09-10). The old
> `bioreactor_website` name still redirects.

## Setup

```bash
git clone <this-repo>
cd bioreactor_website
git submodule update --init            # drivers are NOT checked out by default
                                       # (required for real mode; see below for simulation)

# config.py is gitignored (per-rig); create it from the template and edit it
cp bioreactor-api/config.example.py bioreactor-api/config.py

# the submodule's src/__init__.py imports .config, so point it at the same file
ln -sfn ../../config.py bioreactor-api/bioreactor_v3/src/config.py

cd bioreactor-api
pip install -r requirements.txt                  # API: fastapi, uvicorn, slowapi, ticlib
pip install -r bioreactor_v3/requirements.txt    # drivers: numpy, adafruit-*, lgpio, smbus2, ...
```

**For `HARDWARE_MODE=real` you need both files** — the API layer imports the
drivers, which pull in numpy and the hardware libraries.

**Senseair K33:** the template uses `CO2_SENSOR_I2C_BUS = 3`, a dedicated bus
on GPIO23/24. Follow the [K33 wiring and boot setup](bioreactor-api/bioreactor_v3/docs/senseair_k33.md)
before starting the API. The sensor uses separate main power, common ground,
and 3.3 V I²C signals; leave its DVCC output disconnected from Pi 3.3 V.
Atlas CO₂ rigs should set the type/address for Atlas and use bus 1.
Existing `config.py` files are not updated by pulling code: change the K33 bus
explicitly, and keep the submodule symlink pointing to that same config.

**For simulation you only need `requirements.txt`.** Every API module imports
nothing but the standard library, the drivers are never imported, and the one
piece of `bioreactor_v3` that is used (`src/optics.py`, pure stdlib) is loaded by
path — from the submodule, or from a **sibling `bioreactor_v3` checkout** if the
submodule dir is empty. So a laptop with the submodule uninitialized still runs
simulation fine, as long as `bioreactor_v3` sits next to this repo.

## Run

```bash
# simulation mode (default) — no hardware needed, returns mock data
HARDWARE_MODE=simulation uvicorn main:app --port 9000 --reload

# real hardware — requires a Raspberry Pi with GPIO/I2C
API_KEY=<secret> HARDWARE_MODE=real uvicorn main:app --host 0.0.0.0 --port 9000
```

`config.py` must exist in both modes (it is imported for `INIT_COMPONENTS`).

**`API_KEY` sets the bearer token.** Leave it unset and authentication is skipped
entirely — dev only, never on a rig reachable from anything but localhost. Bind
`--host 0.0.0.0` so the tailnet (and a cloudflared tunnel, if used) can reach it.

In `HARDWARE_MODE=real`, a failure to bring up the rig is **fatal by design** —
the server refuses to start rather than silently serve simulated readings.
Individual components may still fail; those return `503` and the rest works.

Interactive API docs (live schema, always accurate): <http://localhost:9000/docs>

On the rig, the Pi runs this as the `bioreactor-api.service` systemd unit.

## Examples

```bash
BASE=http://localhost:9000
AUTH="Authorization: Bearer $API_KEY"

# what's available
curl -H "$AUTH" $BASE/health
curl -H "$AUTH" $BASE/api/capabilities

# everything the monitor needs, in one call
curl -H "$AUTH" $BASE/api/state

# turn on the LED at 75%
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"power": 75}' $BASE/api/led/control

# heat with peltier at 50% duty cycle
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50, "direction": "heat"}' $BASE/api/peltier_driver/control

# read the vial temperature
curl -H "$AUTH" $BASE/api/temp_sensor/state

# hold 37 °C with the PID loop (does not start CSV), then stop it
curl -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"setpoint": 37.0}' $BASE/api/run/pid
curl -H "$AUTH" $BASE/api/run/status
curl -X POST -H "$AUTH" $BASE/api/run/stop
```

See [`bioreactor-api/API.md`](bioreactor-api/API.md) for the full surface —
schedules, multi-device programs, OD sampling, pumps, relays, history and camera.

## CSV recording

Use **Start CSV** next to **Download latest CSV** in the dashboard. Recording is
independent of temperature PID and CO₂ control; **Stop CSV** only stops recording.
The recorder runs on the Pi and continues if the browser disconnects. API restarts
stop recording. Schedules/programs still record automatically unless a manual
recording is already active. See [CSV endpoints, columns and sampling](bioreactor-api/API.md#independent-csv-recording).

## Configuration

Edit `bioreactor-api/config.py` (your rig's copy, gitignored) to enable/disable
components via the `INIT_COMPONENTS` dict. Only enabled components get endpoints;
disabled ones return `503`.

Start from `config.example.py`, not from the submodule's `config_default.py`: the
template carries ~25 API-layer settings the drivers don't know about (camera,
history, data retention, gas/OD sampler tuning, pump rates) and — importantly —
`RELAY_SAFETY`, without which the gas relays have no dose guard.

Note that `config.example.py` defines a **standalone** `class Config` rather than
inheriting from `config_default.py`, so a setting it omits silently falls back to
whatever default the driver's call site passes. If you add a driver setting, add
it to both files.
