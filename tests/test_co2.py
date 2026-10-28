"""Hardware-free tests: python -m unittest discover -s tests -v."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch, ANY

API=Path(__file__).resolve().parents[1]/'bioreactor-api'
sys.path.insert(0,str(API))
os.environ['HARDWARE_MODE']='simulation'
os.environ['API_KEY']='local-test-key'
os.environ['RATE_LIMIT']='10000/minute'
spec=importlib.util.spec_from_file_location('config',API/'config.example.py')
config=importlib.util.module_from_spec(spec)
sys.modules['config']=config
spec.loader.exec_module(config)
config.Config.HISTORY_ENABLED=False
from program import parse_program,ProgramError,expand_tracks
from control import RunController
from gas_sampler import GasSampler


def program(value=50000):
    return {'duration':'1h','tracks':[{'name':'CO2','steps':[{'co2':value}]}]}


class ProgramTests(unittest.TestCase):
    def test_target_preview(self):
        p=parse_program(program())
        self.assertEqual(p.tracks[0].device,'relay:CO2')
        self.assertEqual(expand_tracks(p)['tracks'][0]['segments'][0]['value'],50000)

    def test_conflicting_relay_track(self):
        p=program()
        p['tracks'].append({'steps':[{'relay':{'name':'CO2','state':'closed'}}]})
        with self.assertRaises(ProgramError):parse_program(p)

    def test_percent_object_normalizes_to_ppm(self):
        p=parse_program(program({'percent':2}))
        self.assertEqual(p.tracks[0].steps[0].value,20000)
        self.assertEqual(expand_tracks(p)['tracks'][0]['segments'][0]['value'],20000)
        for v in ({'percent':True},{'percent':0},{'percent':9.5},
                  {'percent':'2'},{'percent':2,'ppm':20000},{}, {'percent':float('nan')}):
            with self.subTest(value=v), self.assertRaises(ProgramError):
                parse_program(program(v))

    def test_invalid_targets(self):
        for v in [True,-1,0,95000,float('nan'),float('inf'),'5%']:
            with self.assertRaises(ProgramError):parse_program(program(v))
        self.assertIs(parse_program(program(False)).tracks[0].steps[0].value,False)

    def runner(self,apply,stop):
        c=RunController()
        c.configure(bio=None,sim=True,sim_state={},io_module=None,pid_func=None,
                    measure_func=None,data_dir=None,max_heat=70,max_cool=100,
                    co2_apply_fn=apply,co2_stop_fn=stop)
        # Advance the real parser/runner deterministically without a background thread.
        def begin():
            c.active=True;c.run_t0=time.time();c.program_end=c.run_t0+3600
        c._begin=begin
        return c

    def test_stop_and_completion_close(self):
        for completion in [False,True]:
            apply,stop=Mock(),Mock();c=self.runner(apply,stop)
            c.start_program(parse_program(program()));c._tick()
            apply.assert_called_once_with(50000, duration_s=ANY)
            self.assertGreater(apply.call_args.kwargs['duration_s'],3590)
            self.assertLessEqual(apply.call_args.kwargs['duration_s'],3600)
            if completion:
                c.program_end=time.time()-1;c._tick()
            else:c.stop()
            self.assertFalse(c.active);stop.assert_called()

    def test_start_failure_aborts_immediately(self):
        stop=Mock();c=self.runner(Mock(side_effect=ValueError('not calibrated')),stop)
        c.start_program(parse_program(program()));c._tick()
        self.assertTrue(c.aborted);self.assertFalse(c.active);stop.assert_called()

    def test_expired_program_does_not_start_an_injection(self):
        apply,stop=Mock(),Mock();c=self.runner(apply,stop)
        c.start_program(parse_program(program()));c.program_end=time.time()-1;c._tick()
        apply.assert_not_called();stop.assert_called();self.assertFalse(c.active)

    def test_controller_fault_aborts_program(self):
        stop=Mock();c=self.runner(Mock(),stop)
        c._co2_status_fn=lambda:{'owner':'program','fault':'CO2 sample stale'}
        c.start_program(parse_program(program()));c._tick()
        self.assertTrue(c.aborted);self.assertIn('stale',c.abort_reason);stop.assert_called()

    def test_freshness_is_per_sensor(self):
        g=GasSampler();g._latest={'co2':1000,'o2':20};g._acquired={'co2':10,'o2':50}
        self.assertEqual(g.sample('co2'),(1000,10))
        g._acquired_epoch={'co2':1000,'o2':1040}
        with patch('gas_sampler.time.monotonic',return_value=55):
            self.assertEqual(g.sample_info('co2'),{'value':1000,'acquired_at':1000,'age_s':45})


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import main
        cls.main=main
        cls.client=TestClient(main.app)
        cls.client.__enter__()
        cls.headers={'Authorization':'Bearer local-test-key'}

    @classmethod
    def tearDownClass(cls):cls.client.__exit__(None,None,None)

    def tearDown(self):
        self.main.runner.stop()
        self.main.co2_controller.stop()
        self.main.co2_controller.worker.profile=None

    def profile(self):
        from dataclasses import asdict
        from bioreactor_v3.src.co2_mpc import GasModel,MPCSettings
        return {'validated':True,'model':asdict(GasModel(18000,90,60,0.00015)),
                'settings':asdict(MPCSettings())}

    def test_auth_and_unconfigured_refusal(self):
        self.assertEqual(self.client.post('/api/co2/control',json={'target_ppm':50000}).status_code,401)
        r=self.client.post('/api/co2/control',headers=self.headers,json={'target_ppm':50000})
        self.assertEqual(r.status_code,400,r.text)

    def test_sensor_endpoint_exports_acquisition_metadata(self):
        with patch.object(self.main.gas_sampler,'sample_info',return_value={
                'value':1500,'acquired_at':1700000000,'age_s':2.5}) as sample:
            r=self.client.get('/api/co2_sensor/state',headers=self.headers)
        self.assertEqual(r.status_code,200,r.text)
        sample.assert_called_once_with('co2')
        self.assertEqual(r.json(),{'status':'success','co2_ppm':1500,
                                   'acquired_at':1700000000,'sample_age_s':2.5})

    def test_start_status_manual_exclusion_stop(self):
        w=self.main.co2_controller.worker
        w.profile=self.profile();w._last_stop=None
        w.read_sample=lambda:(49900,time.monotonic())
        r=self.client.post('/api/co2/control',headers=self.headers,json={'target_ppm':50000})
        self.assertEqual(r.status_code,200,r.text);self.assertTrue(r.json()['active'])
        r=self.client.post('/api/relays/timed',headers=self.headers,
            json={'relay_name':'CO2','command':'closed','duration':0.25})
        self.assertEqual(r.status_code,409,r.text)
        r=self.client.post('/api/co2/stop',headers=self.headers)
        self.assertEqual(r.status_code,200,r.text);self.assertFalse(r.json()['active'])
        self.assertEqual(self.main.relay_controller.states()['CO2'],'open')

    def test_stale_start_and_guard_mismatch(self):
        w=self.main.co2_controller.worker;w.profile=self.profile();w._last_stop=None
        w.read_sample=lambda:(1000,time.monotonic()-100)
        r=self.client.post('/api/co2/control',headers=self.headers,json={'target_ppm':50000})
        self.assertEqual(r.status_code,400,r.text)
        w.profile['settings']['max_pulse_s']=2
        r=self.client.post('/api/co2/control',headers=self.headers,json={'target_ppm':50000})
        self.assertEqual(r.status_code,400,r.text)

    def test_uncertainty_profile_and_status_use_shared_worker(self):
        w=self.main.co2_controller.worker;w.profile=self.profile();w._last_stop=None
        w.profile['uncertainty']={'gain_min_ppm_per_s':10000,'gain_max_ppm_per_s':25000}
        w.read_sample=lambda:(49900,time.monotonic())
        with patch('bioreactor_v3.src.co2_control.threading.Thread'):
            r=self.client.post('/api/co2/control',headers=self.headers,
                               json={'target_ppm':50000,'duration_s':120})
        self.assertEqual(r.status_code,200,r.text)
        state=self.client.get('/api/co2/controller',headers=self.headers).json()
        self.assertTrue(state['response_uncertainty']['enabled'])
        self.assertEqual(state['response_uncertainty']['safety_gain_ppm_per_s'],27000)
        self.assertEqual(state['response_uncertainty']['gain_max_ppm_per_s'],25000)
        w.stop();w._thread=None;w._last_stop=None
        w.profile['uncertainty']['gain_max_ppm_per_s']=30000
        r=self.client.post('/api/co2/control',headers=self.headers,json={'target_ppm':50000})
        self.assertEqual(r.status_code,400,r.text)

    def test_measurement_pause_keeps_api_ownership_and_deadline(self):
        w=self.main.co2_controller.worker
        w.profile=self.profile();w._last_stop=None
        now=[100.0];sample=[49900,100.0];old_clock=w.clock
        w.clock=lambda:now[0];w.read_sample=lambda:tuple(sample)
        try:
            with patch('bioreactor_v3.src.co2_control.threading', wraps=__import__('threading')) as threads:
                threads.Thread=Mock()
                threads.Thread.return_value.is_alive.return_value=False
                r=self.client.post('/api/co2/control',headers=self.headers,
                    json={'target_ppm':50000,'duration_s':120})
            self.assertEqual(r.status_code,200,r.text)
            deadline=w.deadline
            now[0]=105;sample[:]=[None,105]
            self.assertEqual(w._plan_sample(),0)
            r=self.client.get('/api/co2/controller',headers=self.headers).json()
            self.assertTrue(r['active']);self.assertTrue(r['measurement_paused'])
            self.assertIsNone(r['fault']);self.assertEqual(r['owner'],'api')
            r=self.client.post('/api/relays/timed',headers=self.headers,
                json={'relay_name':'CO2','command':'closed','duration':0.25})
            self.assertEqual(r.status_code,409,r.text)
            with patch.object(w.engine,'propose',return_value=0) as propose:
                now[0]=110;sample[:]=[49900,110];w._plan_sample()
                propose.assert_not_called()
                now[0]=115;sample[:]=[49900,115];w._plan_sample()
                propose.assert_called_once()
            self.assertFalse(w.status()['measurement_paused'])
            self.assertEqual(w.deadline,deadline)
        finally:
            w.stop();w.clock=old_clock

    def test_bounded_provisional_trial(self):
        w=self.main.co2_controller.worker
        w.profile=self.profile();w._last_stop=None
        w.profile.update(validated=False, trial={'enabled':True,'target_max_ppm':10000,
                                                'max_duration_s':60})
        w.read_sample=lambda:(10000,time.monotonic())
        for payload in [{'target_ppm':10001,'duration_s':60},
                        {'target_ppm':10000,'duration_s':61}]:
            r=self.client.post('/api/co2/control',headers=self.headers,json=payload)
            self.assertEqual(r.status_code,400,r.text)
        with self.assertRaises(ValueError):self.main.co2_controller.program(10000)
        r=self.client.post('/api/co2/control',headers=self.headers,
                           json={'target_ppm':10000,'duration_s':60})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(r.json()['trial_mode'])
        self.assertFalse(r.json()['model_validated'])
        self.assertLessEqual(r.json()['remaining_s'],60)

    def test_program_preview_and_unconfigured_start(self):
        r=self.client.post('/api/run/program/preview',headers=self.headers,json=program())
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.post('/api/run/program',headers=self.headers,json=program())
        self.assertEqual(r.status_code,400,r.text)

    def test_percent_endpoint_and_validation(self):
        c=self.main.co2_controller
        with patch.object(c,'start') as start:
            r=self.client.post('/api/co2/control',headers=self.headers,
                               json={'target_percent':2,'duration_s':3600})
            self.assertEqual(r.status_code,200,r.text)
            start.assert_called_once_with(20000,duration_s=3600)
            start.reset_mock()
            for body in ({}, {'target_percent':True}, {'target_percent':'2'},
                         {'target_percent':0}, {'target_percent':9.5},
                         {'target_percent':2,'target_ppm':20000},
                         {'target_percent':2,'typo':1}):
                with self.subTest(body=body):
                    r=self.client.post('/api/co2/control',headers=self.headers,json=body)
                    self.assertEqual(r.status_code,422,r.text)
            start.assert_not_called()

    def test_percent_trial_limits_and_program_duration(self):
        c=self.main.co2_controller;w=c.worker
        w.profile=self.profile();w._last_stop=None
        w.profile.update(validated=False,trial={'enabled':True,'target_max_ppm':20000,'max_duration_s':3600})
        state=c.status()
        self.assertEqual(state['limits']['trial_target_max_percent'],2)
        self.assertEqual(state['limits']['max_duration_s'],3600)
        for body in ({'target_percent':2.01}, {'target_percent':2,'duration_s':3601}):
            r=self.client.post('/api/co2/control',headers=self.headers,json=body)
            self.assertEqual(r.status_code,400,r.text)
        with patch.object(w,'start') as start:
            c.program(20000,duration_s=300)
            start.assert_called_once_with(20000,'program',300)
        with patch.object(self.main.runner,'start_program') as run:
            for duration in (None,'2h'):
                payload=program({'percent':2})
                if duration is None:del payload['duration']
                else:payload['duration']=duration
                r=self.client.post('/api/run/program',headers=self.headers,json=payload)
                self.assertEqual(r.status_code,400,r.text)
            run.assert_not_called()
            r=self.client.post('/api/run/program',headers=self.headers,json=program({'percent':2}))
            self.assertEqual(r.status_code,200,r.text)
            run.assert_called_once()

    def test_timed_off_cannot_schedule_a_future_reopen(self):
        r=self.client.post('/api/relays/timed',headers=self.headers,
            json={'relay_name':'CO2','command':'open','duration':1})
        self.assertEqual(r.status_code,422,r.text)
        self.assertFalse(self.main.relay_controller.status()['pending'].get('CO2'))


if __name__=='__main__':unittest.main()
