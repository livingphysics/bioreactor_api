"""Read a configured master, retain its trace, and optionally follow it with a delay.

Observe mode never writes a valve. Control is an explicit commissioning opt-in.
Source credentials and URLs are configuration, never accepted from request bodies.
"""
from bisect import bisect_left
from collections import deque
from datetime import datetime, timezone
import json
import os
import shutil
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from bioreactor_v3.src.co2_reference import ReferenceTrajectory, ReferenceUnavailable, number


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # never forward the bearer credential to a redirected host


class MasterClient:
    def __init__(self, source):
        self.url = source['base_url'].rstrip('/')
        parts = urlsplit(self.url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('source needs an http(s) base URL without credentials/query/fragment')
        self.token_env = source['token_env']
        self.opener = build_opener(NoRedirect())

    def get(self, path):
        token = os.environ.get(self.token_env)
        if not token:
            raise ValueError('source credential is not configured')
        request = Request(self.url+path, headers={'Authorization': 'Bearer '+token})
        try:
            with self.opener.open(request, timeout=3) as response:
                raw = response.read(8*1024*1024+1)
                if len(raw) > 8*1024*1024:
                    raise ValueError('source response exceeds size limit')
                return json.loads(raw)
        except ValueError:
            raise
        except Exception:
            # URL errors can contain internal addresses; expose only this message.
            raise ReferenceUnavailable('master API request failed') from None

    def sample(self):
        return self.get('/api/co2_sensor/state')

    def history(self, start, end, stop):
        # Six-hour chunks stay below the API's 6000-point archive downsampling
        # at its normal ten-second cadence. Preserve gaps in the original data.
        while start < end and not stop.is_set():
            following = min(start+6*3600, end)
            result = self.get('/api/history/range?'+urlencode({'start':int(start*1000),'end':int(following*1000)}))
            yield from result.get('points', [])
            start = following


class TrackingSession:
    def __init__(self, config, controller, gas_sampler, *, client_factory=MasterClient,
                 clock=time.monotonic, wall=time.time):
        self.config, self.controller, self.gas = config, controller, gas_sampler
        self.clock, self.wall, self.client_factory = clock, wall, client_factory
        self.sources = getattr(config, 'CO2_TRACKING_SOURCES', {})
        self.control_enabled = getattr(config, 'CO2_TRACKING_CONTROL_ENABLED', False) is True
        self.root = Path(getattr(config, 'CO2_TRACKING_DATA_DIR', Path(__file__).parent/'co2-tracking-data'))
        self.min_free_mb = getattr(config, 'CO2_TRACKING_MIN_FREE_MB', 256)
        if not number(self.min_free_mb) or self.min_free_mb < 0:
            raise ValueError('CO2_TRACKING_MIN_FREE_MB must be finite and nonnegative')
        self.period = 5.0
        self.stale_s = 30.0
        self.max_gap_s = 30.0
        self._lock = threading.RLock()
        self._transition = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.active = False
        self._points = deque(maxlen=40000)  # >48h at five seconds, bounded memory
        self._state = {}
        self._latest = None
        self._received = None
        self._source_age_at_receive = None
        self._verified = False
        self._fault = None
        self._path = None
        self._anchor = None
        self._attempted_control = False

    def start(self, source, delay_s=3660, mode='observe', duration_s=0):
        if source not in self.sources:
            raise ValueError('unknown configured master source')
        if mode not in ('observe', 'control'):
            raise ValueError('tracking mode must be observe or control')
        if not number(delay_s) or not 60 <= delay_s <= 86400:
            raise ValueError('tracking delay must be 60..86400 seconds')
        if not number(duration_s) or not 0 <= duration_s <= 604800:
            raise ValueError('tracking duration must be 0..604800 seconds')
        if mode == 'control':
            if not self.control_enabled:
                raise ValueError('tracking actuation needs CO2_TRACKING_CONTROL_ENABLED after commissioning')
            profile = self.controller.worker.profile or {}
            self.controller.validate(profile.get('settings', {}).get('target_ppm', 50000), duration_s or 0)
            if delay_s < profile['settings'].get('horizon_s', 1800)+60:
                raise ValueError('control delay must cover the MPC horizon plus 60 seconds')
            if self.controller.worker.status()['active']:
                raise RuntimeError('CO2 valve is already owned by a controller')
        client = self.client_factory(self.sources[source])
        with self._transition, self._lock:
            if self.active or (self._thread and self._thread.is_alive()):
                raise RuntimeError('a tracking session is already active or stopping')
            self.root.mkdir(parents=True, exist_ok=True)
            self._check_storage()
            self._path = self.root/('co2-tracking-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')+'.jsonl')
            # Fail before starting if storage is unavailable.
            with self._path.open('x') as file:
                file.write(json.dumps({'event':'start','source':source,'mode':mode,'delay_s':delay_s,'time_s':self.wall()})+'\n')
            self._points.clear()
            self._latest = self._received = self._fault = None
            self._source_age_at_receive = None
            self._verified = self._attempted_control = False
            self._anchor = self.wall(), self.clock()
            self._state = {'source':source,'mode':mode,'delay_s':delay_s,'phase':'loading_history',
                           'reference_ppm':None,'error_ppm':None,'freshness_verified':False}
            self._deadline = self.clock()+duration_s if duration_s else None
            self._stop = threading.Event()
            self.active = True
            self._thread = threading.Thread(target=self._run, args=(client,self._stop), daemon=True,name='co2-tracking')
            self._thread.start()
        return self.status()

    def _add(self, stamp, value, verified):
        if not number(stamp) or not number(value) or not 0 < value < 100000:
            return
        # Called by the sole ingest thread; cached/out-of-order points cannot
        # count as new evidence. History is loaded in timestamp order.
        with self._lock:
            if self._points and stamp <= self._points[-1][0]:
                return
            self._points.append((stamp, value, bool(verified)))

    def _provider(self, now):
        with self._lock:
            if self._stop.is_set() or not self.active:
                raise ReferenceUnavailable('tracking stopped')
            if (not self._verified or self._received is None
                    or self._source_age_at_receive is None
                    or now-self._received+self._source_age_at_receive > self.stale_s):
                raise ReferenceUnavailable('master measurement freshness is unverified or stale')
            epoch, mono = self._anchor
            if abs((self.wall()-epoch)-(now-mono)) > 2:
                raise ReferenceUnavailable('clock changed; restart tracking to synchronize')
            delay = self._state['delay_s']
            horizon = self.controller.worker.profile['settings'].get('horizon_s',1800)
            points = tuple(self._points)
        start = epoch+now-mono-delay-30
        end = start+horizon+90
        stamps = [p[0] for p in points]
        first = max(0,bisect_left(stamps,start)-1)
        last = min(len(points),bisect_left(stamps,end)+1)
        window = points[first:last]
        if not window or not all(p[2] for p in window):
            raise ReferenceUnavailable('waiting for a complete verified master preview')
        profile = self.controller.worker.profile
        maximum = profile['settings'].get('max_ppm',95000)-profile['settings'].get('margin_ppm',5000)
        if not profile.get('validated'):
            maximum = min(maximum,profile['trial']['target_max_ppm'])
        if any(p[1] >= maximum for p in window):
            raise ReferenceUnavailable('master reference exceeds follower limits')
        return ReferenceTrajectory(tuple((mono+p[0]-epoch+delay,p[1]) for p in window),self.max_gap_s)

    def _check_storage(self):
        if shutil.disk_usage(self.root).free < self.min_free_mb*1024*1024:
            raise OSError('tracking stopped: free disk space below configured reserve')

    def _tick(self, client, log):
        self._check_storage()
        sample = client.sample()
        now, epoch = self.clock(), self.wall()
        value, acquired, age = (sample.get(k) for k in ('co2_ppm','acquired_at','sample_age_s'))
        if not number(value) or not 0 < value < 100000:
            raise ReferenceUnavailable('master measurement is missing or invalid')
        verified = (number(acquired) and number(age) and 0 <= age <= self.stale_s
                    and -2 <= epoch-acquired <= self.stale_s
                    and abs(epoch-acquired-age) <= 2)
        # Legacy APIs are useful for observation/replay but cannot authorize doses.
        if acquired is None:
            acquired = epoch
        elif not verified:
            raise ReferenceUnavailable('master acquisition timestamp is stale or inconsistent')
        self._add(acquired,value,verified)
        with self._lock:
            self._latest, self._received, self._verified = value, now, verified
            self._source_age_at_receive = max(age, epoch-acquired, 0) if verified else None
            points = tuple((t,v) for t,v,_ in self._points)
            delay, mode = self._state['delay_s'], self._state['mode']
            self._state.update(freshness_verified=verified, phase='observing' if mode=='observe' else 'warming')
        reference = None
        try:
            reference = ReferenceTrajectory(points,self.max_gap_s).value_at(epoch-delay)
        except ReferenceUnavailable:
            pass
        if mode == 'control':
            status = self.controller.worker.status()
            if self._attempted_control and (not status['active'] or status['owner']!='tracking'):
                raise RuntimeError(status.get('fault') or 'tracking controller stopped')
            if not self._attempted_control:
                try:
                    trajectory = self._provider(now)
                    trajectory.require_span(now, now+self.controller.worker.profile['settings'].get('horizon_s',1800)+30)
                    target = trajectory.value_at(now)
                except ReferenceUnavailable as e:
                    with self._lock:self._state['phase'] = str(e)
                else:
                    with self._transition:
                        if not self._stop.is_set():
                            remaining = max(.001,self._deadline-now) if self._deadline else 0
                            self.controller.start(target,owner='tracking',duration_s=remaining,reference_provider=self._provider)
                            self._attempted_control = True
            status = self.controller.worker.status()
            if self._attempted_control:
                with self._lock:self._state['phase'] = status.get('last',{}).get('measurement_state','tracking')
        else:
            status = self.controller.worker.status()
        follower, stamp = self.gas.sample('co2')
        if not number(follower) or not number(stamp) or not 0 <= now-stamp <= 20:
            follower = None
        error = follower-reference if follower is not None and reference is not None else None
        with self._lock:
            self._state.update(reference_ppm=reference,error_ppm=error,master_ppm=value)
        row={'time_s':epoch,'master_acquired_at':acquired,'master_ppm':value,
             'reference_ppm':reference,'follower_ppm':follower,'error_ppm':error,
             'o2_percent':self.gas.latest().get('o2'),'freshness_verified':verified,
             'delay_s':delay,'mode':mode,'controller':status}
        log.write(json.dumps(row,allow_nan=False)+'\n');log.flush()

    def _run(self, client, stop):
        try:
            # Source samples persist for audit/replay in the session log. Backfill
            # remains explicitly unverified; a fresh verified preview must fill
            # before actuation. No control state is automatically resumed.
            with self._path.open('a') as log:
                try:
                    for point in client.history(self.wall()-self._state['delay_s']-120,self.wall(),stop):
                        self._add(point.get('t',0)/1000,point.get('co2'),False)
                except Exception:
                    with self._lock:self._state['phase']='history unavailable; collecting live'
                while not stop.is_set() and (self._deadline is None or self.clock()<self._deadline):
                    started = self.clock()
                    try:
                        self._tick(client,log)
                    except ReferenceUnavailable as e:
                        with self._lock:
                            self._verified = False
                            self._state.update(phase=str(e), freshness_verified=False,
                                               master_ppm=None, reference_ppm=None, error_ppm=None)
                        log.write(json.dumps({'time_s':self.wall(),'event':'source_gap','reason':str(e),
                                              'reference_ppm':None,'follower_ppm':None})+'\n');log.flush()
                    stop.wait(max(0,self.period-(self.clock()-started)))
        except Exception as e:
            with self._lock:self._fault = str(e)
        finally:
            if self._state.get('mode')=='control':self.controller.stop(owner='tracking')
            with self._lock:self.active=False

    def stop(self):
        self._stop.set()
        with self._transition:
            if self._state.get('mode')=='control':self.controller.stop(owner='tracking')
        thread=self._thread
        if thread and thread is not threading.current_thread():thread.join(timeout=4)
        with self._lock:self.active=False
        return self.status()

    def status(self):
        with self._lock:
            return {'configured':bool(self.sources),'sources':list(self.sources),
                    'control_enabled':self.control_enabled,'active':self.active,'fault':self._fault,
                    'log_file':self._path.name if self._path else None,
                    'source_age_s':(None if self._received is None or self._source_age_at_receive is None
                                    else max(0,self.clock()-self._received+self._source_age_at_receive)),
                    'poll_age_s':None if self._received is None else max(0,self.clock()-self._received),
                    **self._state}

    def log_path(self):
        with self._lock:return self._path
