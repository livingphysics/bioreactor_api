# Bioreactor API

A minimal FastAPI server that exposes bioreactor hardware as REST endpoints.
Hardware drivers live in the [`bioreactor_v3`](../bioreactor_v3) submodule;
this repo is just the HTTP wrapper.

## Setup

```bash
git clone <this-repo>
cd bioreactor_website
git submodule update --init

# config.py is gitignored (per-rig); create it from the template and edit it
cp bioreactor-api/config.example.py bioreactor-api/config.py

# the submodule's src/__init__.py imports .config, so point it at the same file
ln -sfn ../../config.py bioreactor-api/bioreactor_v3/src/config.py

cd bioreactor-api
pip install -r requirements.txt

```

## Run

```bash
# simulation mode — no hardware needed, returns mock data
HARDWARE_MODE=simulation uvicorn main:app --port 9000 --reload

# real hardware mode — requires a Raspberry Pi with GPIO/I2C
HARDWARE_MODE=real uvicorn main:app --port 9000
```

Interactive API docs: <http://localhost:9000/docs>

## Examples

```bash
# what's available
curl http://localhost:9000/health
curl http://localhost:9000/api/capabilities

# turn on the LED at 75%
curl -X POST http://localhost:9000/api/led/control \
  -H "Content-Type: application/json" \
  -d '{"power": 75}'

# heat with peltier at 50% duty cycle
curl -X POST http://localhost:9000/api/peltier_driver/control \
  -H "Content-Type: application/json" \
  -d '{"duty_cycle": 50, "direction": "heat"}'

# read the vial temperature
curl http://localhost:9000/api/temp_sensor/state
```

## Configuration

Edit `bioreactor-api/config.py` (your rig's copy, gitignored) to enable/disable
components via the `INIT_COMPONENTS` dict. Only enabled components get
endpoints; disabled ones return `503`.
