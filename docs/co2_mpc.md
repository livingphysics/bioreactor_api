# CO₂ MPC integration

The model, pulse optimizer, standalone runner, calibration fitting and full setup
instructions live in [the driver documentation](../bioreactor-api/bioreactor_v3/docs/co2_mpc.md).
Update the submodule with `git submodule update --init --recursive` when deploying
this branch. Preserve the rig's `config.py` and its submodule symlink.

Add `CO2_MPC` to the rig config after fitting/validating a model. `None` (the template
default) leaves control unavailable while normal sensing and manual guarded relay
operation continue. The target is in ppm: 50,000 = 5%. The API refuses settings
that exceed its CO₂ relay guards or 95,000 ppm.

Use [examples/co2_5_percent.json](../examples/co2_5_percent.json) in the frontend's
Program panel, or POST `{"target_ppm":50000,"duration_s":3600}` to `/api/co2/control`.
The controller runs on the Pi after clients disconnect. `/api/co2/stop` stops it;
`/api/co2/controller` and `/api/state.co2_control` expose its status and forecast.
The matching `bioreactor_server` branch adds proxy endpoints, CO₂ target/status
labels and a correct ppm-to-percent display in program previews.

## One-pulse identification

The measurement tool requires relay status to expose `closed_seconds` computed
after completed GPIO writes. `dose_s` records the difference in those counters;
`requested_dose_s` records the command. Older APIs are rejected before dosing.
A timing mismatch greater than max(20 ms, 10%) stops the measurement after saving
the pulse row. These counters estimate electrical energized time, not mechanical
valve opening. Independently verify short-pulse timing before calibrating flow.

Set `API_KEY` in the environment and run (on the Pi, or supply `--url`):

```sh
python tools/co2_response.py --pulse 0.25 --baseline 60 --observe 1200 \
  --limit 95000 --output response.csv
```

The script records a single pulse, then keeps the valve off. It refuses an active
program, a pending CO₂ dose, or a pulse above the existing guard. It stops on bad
readings or the concentration limit and does not retry uncertain actuation requests.
On the pre-MPC API, gas endpoints do not expose acquisition timestamps; collect
against a healthy running sampler and inspect continuity before fitting. The new
MPC itself uses per-sensor monotonic acquisition timestamps and rejects stale data.

Fit with the driver tool, using NumPy:

```sh
cd bioreactor-api/bioreactor_v3
python -m tools.fit_co2 /path/to/response.csv --output /path/to/profile.json
```

The fit always remains provisional (`validated:false`). Review independent pulse
responses, minimum reliable solenoid duration and valve-off decay before enabling
live control. Do not substitute the documentation's illustrative model parameters
for a calibration of the installed rig.

## Tests

Install API requirements plus `httpx` and `numpy` in an isolated environment, then
run `python -m unittest discover -s tests -v` from this repository root. The tests
use simulated hardware and do not need a real `config.py` or attached reactor.
