# Follow another reactor's CO₂ trace

The follower runs the session locally on its Pi. It polls the configured master
API every five seconds and targets `master(t − delay_s)`. The dashboard is only
an interface; closing it does not stop recording or control. This feature does
not change the master's experiment or actuators.

## First milestone and commissioning

Start in **Record comparison** mode. This records the master, follower and delayed
reference without taking ownership of the valve or starting an MPC worker. A
separate CO₂ controller can run while observing. Stopping observation leaves that
controller alone.

Actuation defaults to disabled. On 2026-10-09, 72 hours of the algae master's
history ranged from **498 to 1,053 ppm**. The follower's current high-concentration
profile represents **7,250 nominal ppm** of injection per minimum 0.125-second
pulse, before transport, mixing and loss. A 24-hour replay selected zero pulses
and gave approximately 479 ppm RMSE. This profile cannot establish useful tracking
at the master's observed range. That result is exploratory simulation, not a new
hardware calibration.

Before enabling control:

1. Compare the two CO₂ sensors over the observed range, including their offsets.
2. Commission smaller gas doses (for example with reduced flow or diluted gas),
   retaining a reproducible regulator setting and minimum reliable valve pulse.
3. Fit and validate transport, mixing, low-range loss and dose uncertainty; retune
   the concentration deadband and average correction for this range.
4. Replay recorded light/dark cycles and assess bias, MAE, RMSE, overshoot and rising
   versus falling error at the declared delay. Do not fit an extra time shift.
5. Check whether passive decline is fast enough. N₂ flushing requires its own
   calibrated actuator and O₂ limits; this version never operates N₂.
6. Run a supervised bounded tracking test before allowing long sessions.

The current master API omits measurement acquisition timestamps. Observation can
use it, but actuation is inhibited. Upgrade the master to a version exposing
`acquired_at` and `sample_age_s` **between experiments**; do not restart the master
just to record a trace. History archive timestamps alone do not prove sensor
freshness and never authorize a dose.

## Configure the follower

Use the driver's `codex/co2-tracking` revision pinned by the API submodule. On the
follower, add these settings to its gitignored `bioreactor-api/config.py`:

```python
CO2_TRACKING_SOURCES = {
    'algae': {
        'base_url': 'http://mori:9000',
        'token_env': 'CO2_MASTER_API_KEY',
    },
}
CO2_TRACKING_CONTROL_ENABLED = False
# Optional override; the default is beside config.py:
# CO2_TRACKING_DATA_DIR = '/persistent/local/path/co2-tracking-data'
```

These are attributes inside `class Config`. Put `CO2_MASTER_API_KEY` in the
follower API service's environment with the master's API bearer token. Keep it
out of Git, browser requests and logs. Use the master's private Pi API over the
tailnet, rather than its public dashboard/login page. No new Python dependency
is needed beyond the existing API/driver requirements. Keep the existing shared
config symlink and persistent CO₂ dose journal.

Only configured source IDs can be selected through the API. Source URLs and
credentials cannot be supplied by a client. Redirects are rejected so a bearer
credential cannot be forwarded to another host.

## API and dashboard

The gases page contains **Follow master CO₂**, with a source selector, delay in
minutes, Record comparison / Control follower modes, Start, Stop and comparison
log download. Master and delayed reference are additional gas graph traces in
ppm. View-only dashboard sessions cannot start or stop a session.

Authenticated API examples:

```json
POST /api/co2/tracking/start
{"source":"algae","mode":"observe","delay_s":3660,"duration_s":0}
```

`duration_s: 0` means until stopped; positive durations are limited to seven days.
Delays are 60 seconds to 24 hours. Only one session may be active. Read status at
`GET /api/co2/tracking` or `co2_tracking` in `GET /api/state`; stop using
`POST /api/co2/tracking/stop`. Download the current/latest session with
`GET /api/co2/tracking/log`.

After commissioning, set `CO2_TRACKING_CONTROL_ENABLED = True` and restart the
follower API. An explicit `mode: "control"` request is still required. The delay
must cover the MPC horizon plus 60 seconds: **61 minutes** for the current
3,600-second horizon. This is an initial preview requirement, not an optimized
tracking delay.

Control warms up while collecting a complete verified reference preview. Backfill
is available for observation immediately, but control waits for fresh samples to
replace it. It uses the existing local CO₂ sensor, MPC, concentration/pulse guards
and durable dose history. The planner scores the future reference at each time;
average correction integrates follower-minus-reference error through changing
references. No model or safety threshold is changed by tracking.

Tracking control reserves the CO₂ valve during warmup and operation. Scalar
setpoint requests and CO₂ program/manual doses conflict with it. Stop tracking
first to switch modes. Explicit manual CO₂ OFF, CO₂ Stop and Standby stop the
tracking session. Other devices and the master's experiment continue normally.

## Freshness, outages and logs

Verified control samples require a finite acquisition timestamp, age no greater
than 30 seconds and UTC/age agreement within two seconds. Keep both Pi clocks
synchronized. A fixed UTC/monotonic anchor aligns the delayed reference; a clock
jump invalidates it until the session is restarted. Interpolation rejects gaps
over 30 seconds and never extrapolates beyond the known preview.

Missing/stale master readings pause new doses while the local observer continues
tracking gas already injected. Average correction freezes its offset and clears
its window. Control resumes only with a complete verified preview. Local sensor
outage limits and concentration cutoffs still apply. A worker fault or failed log
write stops the session; sessions never resume automatically after API restart.
Recording/control also stops when free storage falls below
`CO2_TRACKING_MIN_FREE_MB` (default 256 MiB).

JSONL logs persist on the follower and are independent of the manual CSV recorder.
Each sample includes UTC time, master acquisition time and ppm, delayed reference,
follower ppm, signed error, O₂, freshness flag, delay, mode and controller state.
Source gaps are explicit events. History also archives `master_co2` and
`co2_reference` for dashboard reloads. Logs have no automatic retention policy in
this version; archive/remove old sessions as needed. Only the current/latest
session in this API process is offered by the download endpoint; older files
remain in the data directory.

For offline replay and the shared standalone worker/provider interface, see
[driver tracking documentation](../bioreactor-api/bioreactor_v3/docs/co2_tracking.md).
This first implementation is ready for observation and further commissioning;
it is not validated for autonomous low-range gas control.


## Commissioning shorter manual pulses

Manual/program relay doses retain their historical 0.05-second floor unless
`RELAY_SAFETY['CO2']['min_duration_s']` explicitly sets a different positive
minimum. Requests are clamped between this minimum and `max_duration_s`; inspect
these values in `/api/relays/state` before a test. The MPC has its own
`CO2_MPC['settings']['min_pulse_s']`; changing the manual floor does not change
that profile or establish mechanical valve resolution. Record completed electrical
ON-time and the gas response separately. A short electrical pulse may deliver no
gas, variable gas, or a nonlinear amount. Commission it before use in tracking.
