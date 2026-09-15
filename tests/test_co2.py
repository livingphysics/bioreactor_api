"""Hardware-free tests: python -m unittest discover -s tests -v."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock

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
            apply.assert_called_once_with(50000)
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

    def test_freshness_is_per_sensor(self):
        g=GasSampler();g._latest={'co2':1000,'o2':20};g._acquired={'co2':10,'o2':50}
        self.assertEqual(g.sample('co2'),(1000,10))


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

    def test_program_preview_and_unconfigured_start(self):
        r=self.client.post('/api/run/program/preview',headers=self.headers,json=program())
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.post('/api/run/program',headers=self.headers,json=program())
        self.assertEqual(r.status_code,400,r.text)

    def test_timed_off_cannot_schedule_a_future_reopen(self):
        r=self.client.post('/api/relays/timed',headers=self.headers,
            json={'relay_name':'CO2','command':'open','duration':1})
        self.assertEqual(r.status_code,422,r.text)
        self.assertFalse(self.main.relay_controller.status()['pending'].get('CO2'))


if __name__=='__main__':unittest.main()
