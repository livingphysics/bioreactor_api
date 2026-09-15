"""Record one bounded API valve pulse and its response. No automatic repeat doses.

API_KEY is read from the environment. CSV goes to --output; progress to stdout.
The API's timed relay endpoint owns valve closure even if this client disconnects.
"""
import argparse
import csv
import json
import math
import os
import time
import urllib.request


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='http://127.0.0.1:9000')
    p.add_argument('--pulse', type=float, required=True)
    p.add_argument('--baseline', type=float, default=60)
    p.add_argument('--observe', type=float, default=1200)
    p.add_argument('--limit', type=float, required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    if not all(math.isfinite(v) and v > 0 for v in (a.pulse, a.baseline, a.observe, a.limit)):
        p.error('durations and limit must be finite and positive')
    def request(path, body=None):
        req = urllib.request.Request(a.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + os.environ['API_KEY'],
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    state = request('/api/state')
    if state['run']['active']:
        raise RuntimeError('Stop the experiment before an identification pulse')
    relay = request('/api/relays/state')
    guard = relay.get('guards', {}).get('CO2', {})
    if not guard or a.pulse > guard['max_duration_s']:
        raise RuntimeError('Pulse exceeds or lacks API timed-dose guard')
    if relay['states']['CO2'] != 'open' or relay.get('pending', {}).get('CO2'):
        raise RuntimeError('CO2 valve is already active')
    t0 = time.monotonic()
    fired = False
    valid_baseline = 0
    with open(a.output, 'x', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['t_s', 'co2_ppm', 'dose_s'])
        writer.writeheader()
        try:
            while time.monotonic() - t0 <= a.baseline + a.observe:
                tick = time.monotonic()
                value = request('/api/co2_sensor/state')['co2_ppm']
                if value is None or not math.isfinite(value) or value < 0 or value >= a.limit:
                    raise RuntimeError(f'Missing/invalid CO2 or limit reached: {value}')
                dose = 0
                if tick - t0 < a.baseline:
                    valid_baseline += 1
                elif not fired:
                    if valid_baseline < 6:
                        raise RuntimeError('Not enough baseline readings')
                    # Never retry an uncertain POST: it may already have actuated.
                    fired = True
                    request('/api/relays/timed', {'relay_name': 'CO2',
                            'command': 'closed', 'duration': a.pulse})
                    dose = a.pulse
                row = {'t_s': round(tick-t0, 3), 'co2_ppm': value, 'dose_s': dose}
                writer.writerow(row)
                f.flush()
                print(json.dumps(row), flush=True)
                time.sleep(max(0, 5 - (time.monotonic() - tick)))
        finally:
            if fired:
                request('/api/relays/control', {'relay_name': 'CO2', 'command': 'open'})


if __name__ == '__main__':
    main()
