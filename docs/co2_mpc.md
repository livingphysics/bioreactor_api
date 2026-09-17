# CO₂ MPC integration

The model, pulse optimizer, standalone runner and full setup
instructions live in [the driver documentation](../bioreactor-api/bioreactor_v3/docs/co2_mpc.md).
Update the submodule with `git submodule update --init --recursive` when deploying
this branch. Preserve the rig's `config.py` and its submodule symlink.

Add `CO2_MPC` to the rig config after fitting/validating a model. `None` (the template
default) leaves control unavailable while normal sensing and manual guarded relay
operation continue. The internal target is in ppm: 50,000 = 5%. The UI and percent-based request use 5 for 5%, not 0.05. The API refuses settings
that exceed its CO₂ relay guards or 95,000 ppm.

## Gases page and JSON commands

The dashboard's **CO₂ / O₂** page has a **CO₂ setpoint** panel. Enter the target
in percent and a duration in minutes, then select **Start CO₂ control**. While
active, **Update CO₂ setpoint** preserves the observer and pending gas history.
The panel shows remaining time, waiting/recovery state and faults. **Stop CO₂**
closes the valve without stopping temperature control. A program-owned controller
must be stopped before taking direct control. Closing the browser does not stop
the on-Pi controller.

The panel reads the current rig's limits; it does not assume all rigs are calibrated.
On bioreactor01 the tested provisional profile permits setpoints up to **2%** and
timed runs up to **240 minutes**. With `trial.allow_indefinite: true`, enter
**0** minutes to run until stopped. Positive-duration updates cannot extend a
timed trial's deadline; an explicitly permitted zero-duration update removes it. The existing minimum pulse, measurement recovery, ownership and restart
settling guards remain active. The commissioning script's separate 27,500 ppm
supervisor is not launched by the UI; direct/program control uses the shared
worker's configured 30,000 ppm cutoff and 29,500 ppm planning budget. Keep the
physical setup consistent with calibration (20 ml buffer and 30% stirring).

POST this JSON to `/api/co2/control` on the authenticated Pi API or dashboard proxy:

```json
{"target_percent": 2, "duration_s": 3600}
```

`target_percent` must be finite, positive and below 9.5, and also satisfy the
rig's stricter configured limits. Supply exactly one of `target_percent` or the
backwards-compatible `target_ppm`. Duration defaults to 3,600 seconds; **0 means indefinite** when the profile permits
it. For example, `{"target_percent": 2, "duration_s": 0}`. Unknown
fields, both targets, strings and booleans are rejected. `POST /api/co2/stop`
stops control; `GET /api/co2/controller` and `/api/state.co2_control` expose its
status, percent target and configured limits.

For the Program panel or `POST /api/run/program`, use
[examples/co2_2_percent.json](../examples/co2_2_percent.json):

```json
{
  "name": "CO2 at 2 percent",
  "duration": "1h",
  "tracks": [{"name": "CO2", "steps": [{"co2": {"percent": 2}}]}]
}
```

`{"co2": false}` stops a program's CO₂ controller. Existing numeric
`{"co2": 20000}` remains ppm and behaves identically. Percent objects normalize
to ppm in program previews and the shared worker. Bounded profiles require a
finite program duration within the configured maximum unless indefinite operation
is explicitly enabled; each CO₂ step receives
only the time remaining to the original program deadline. Program completion,
stop or abort stops its CO₂ worker. Use `/api/run/program/preview` to check JSON
without actuating hardware. The 5% example requires a separately permitted profile;
it is not enabled by the current bioreactor01 calibration.

For an explicitly supervised commissioning trial, a provisional profile may keep
`validated: false` and specify `"trial": {"enabled": true, "target_max_ppm": 10000,
"max_duration_s": 7200}`. Use the direct control endpoint with a bounded duration;
finite program durations are supported. Indefinite starts require the explicit
`trial.allow_indefinite: true` option (validated profiles already permit them). Existing concentration,
freshness, pulse and restart-settling guards still apply. Positive-duration requests cannot
extend a timed trial deadline; an allowed zero-duration request removes it. Status reports `trial_mode`, `model_validated`,
`remaining_s` and `restart_wait_s`. The observer uses fresh CO₂ feedback every
cycle while retaining the delayed effects of previous injections; kinetics are not automatically refitted; optional uncertainty learning updates pulse gain within its fixed safety bound. See the driver documentation before commissioning.

Brief missing, invalid or stale measurements pause dosing while the worker remains
active. Configure `missing_timeout_s` (default 30 seconds since the last valid
acquisition) and `recovery_samples` (default 2 distinct fresh readings) in the
profile's `settings`. No pulse is proposed during recovery; dose history, the
observer and the original deadline are retained. `/api/co2/controller` exposes
`measurement_paused`, `last_valid_age_s`, `recovery_samples`, and
`last.measurement_state`. A longer outage latches a fault and stops control;
concentration limits and other faults remain immediate. Initial starts require a
fresh reading. External supervisors should allow the same brief gaps instead of
stopping on a single null sensor value.

## Tuning nominal pulse gain

The nominal `model.gain_ppm_per_s` predicts typical pulse response. The product
`model.gain_ppm_per_s * settings.gain_safety_factor` bounds uncertain injected gas
in the planner. When reducing nominal gain after calibration, preserve the
previous conservative upper gain unless independent evidence supports changing it:

```python
upper_gain = old_gain * old_gain_safety_factor
new_gain_safety_factor = upper_gain / new_gain
```

For example, the bioreactor01 commissioning profile changed nominal gain from
35,749.5165 to 20,000 ppm/s and the factor from 1.5 to 2.6812137358. Its upper
gain remains 53,624.2747 ppm/s. Delay, mixing, leakage, pulse timing and measurement
recovery settings are separate and should be held fixed when evaluating a gain
change. Compare independent recorded pulses and closed-loop simulations; a better
nominal fit does not establish a worst-case response bound or validate the rig.
Preserve the provisional profile's bounded-trial restrictions. Apply a profile
change only with control stopped, back up `config.py`, and restart the API to load
it. The restart does not start CO₂ control.

## Variable pulse responses

The shared driver now supports an optional `CO2_MPC['uncertainty']` profile object.
It plans against nine gain/kinetic scenarios and learns conditional pulse gains
from sufficiently complete fresh response windows. API, standalone and program
control all use the same implementation. Existing profiles retain their behavior
when this object is omitted. See the driver's
[configuration and learning details](../bioreactor-api/bioreactor_v3/docs/co2_mpc.md#optional-response-uncertainty-and-online-gain-learning).

`GET /api/co2/controller` now exposes `response_uncertainty`, including the current
nominal gain, operating range, fixed safety gain, update count and last fit quality.
The percent-based endpoint and program forms use this same worker.
Learning cannot reduce the fixed safety bound or change a deadline; API ownership,
sensor-gap recovery and valve shutdown remain in the shared worker.

## Indefinite status and stopping

`limits.allow_indefinite` tells the dashboard whether zero is accepted. While an
indefinite run is active, status includes `indefinite: true` and `remaining_s: null`.
The controller retains measurement recovery and all concentration/pulse guards.
Stop CO₂, a latched fault, API shutdown or a reboot stops it. It does not restart
automatically. Switching between timed and indefinite operation retains pending
doses and the observer. A positive duration sets a timer on an indefinite run.
