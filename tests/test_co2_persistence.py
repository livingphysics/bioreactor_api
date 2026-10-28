import copy,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_co2  # installs simulation config before API imports
from co2_controller import CO2API
from relay_controller import RelayController
from bioreactor_v3.src.co2_mpc import GasModel,MPCSettings,ResponseUncertainty,make_controller

PROFILE=json.loads(Path('/Users/davidjordan/Documents/GitHub/bioreactor_website/experiments/bioreactor01-2026-09-18/nonlinear-revision/candidate-profile.json').read_text())

class PersistenceAPITests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.now=100.;self.handles=[]
  self.cfg=SimpleNamespace(CO2_MPC=copy.deepcopy(PROFILE),CO2_MPC_STATE_PATH=str(Path(self.tmp.name)/'state.json'),RELAYS={'CO2':17},CO2_SENSOR_TYPE='sensair_k33',CO2_SENSOR_I2C_BUS=3,CO2_SENSOR_I2C_ADDRESS=104,RELAY_SAFETY={'CO2':{'max_duration_s':1,'min_interval_s':60,'co2_max_ppm':95000}})
  original=Path.read_text
  self.patch=patch.object(Path,'read_text',lambda p,*a,**kw:'boot-A' if str(p)=='/proc/sys/kernel/random/boot_id' else original(p,*a,**kw));self.patch.start()
 def tearDown(self):
  for c in self.handles:
   c.stop();c.worker.close_history();c.relays.stop()
  self.patch.stop();self.tmp.cleanup()
 def api(self):
  states={'CO2':False};relays=RelayController();relays.configure(set_fn=lambda n,v:states.__setitem__(n,v),get_fn=lambda:states,names=['CO2'],guards=self.cfg.RELAY_SAFETY,co2_fn=lambda:30000)
  c=CO2API(self.cfg,SimpleNamespace(sample=lambda n:(30000,self.now)),relays,{'co2_sensor':True,'relays':True});c.worker.clock=lambda:self.now;c.enable_history();self.handles.append(c);return c
 def release(self,c):c.stop();c.worker.close_history();c.relays.stop();self.handles.remove(c)
 def test_clean_restart_restores_doses_without_starting_or_full_wait(self):
  c=self.api();self.now+=1610
  e=make_controller(GasModel(**PROFILE['model']),MPCSettings(**PROFILE['settings']),ResponseUncertainty(**PROFILE['uncertainty']))
  e.observe(30000,self.now,self.now);c.worker.history.begin(e);start=self.now;self.now+=.125;e.committed(start,.125);c.worker.engine=e;c.worker.history.complete(e)
  self.release(c);self.now+=10;c=self.api()
  self.assertFalse(c.status()['active']);self.assertEqual(c.status()['restart_wait_s'],0)
  with patch.object(c.worker,'_run',return_value=None):c.start(50000,duration_s=3600)
  c.worker._thread.join();self.assertEqual(c.worker.engine.doses,[(start,.125)])
  self.assertEqual(c.worker.engine.propose(self.now),0)
 def test_manual_dose_and_stop_invalidate_history_and_cancel_timer(self):
  c=self.api();self.now+=1610
  c.manual('CO2','closed',.05)
  self.assertEqual(c.worker.history.state['pending'],'external')
  self.assertGreater(c.status()['restart_wait_s'],0)
  c.stop();self.assertFalse(c.relays.status()['pending'])
  self.assertEqual(c.relays.states()['CO2'],'open')
  self.assertGreater(c.status()['restart_wait_s'],0)
  wait=c.status()['restart_wait_s'];self.release(c);self.now+=100;c=self.api()
  self.assertAlmostEqual(c.status()['restart_wait_s'],wait-100)
  with self.assertRaisesRegex(RuntimeError,'settle'):c.start(50000,duration_s=3600)
 def test_timed_toggle_cannot_schedule_reopening_after_stop(self):
  c=self.api();self.now+=1610;c.manual('CO2','closed',1)
  with self.assertRaisesRegex(ValueError,'reopen'):c.manual('CO2','toggle',.1)
 def test_unconfigured_api_invalidates_previous_state(self):
  c=self.api();self.release(c);self.cfg.CO2_MPC=None;c=self.api()
  self.assertIsNone(c.worker.history)
  self.assertIn('invalid',json.loads(Path(self.cfg.CO2_MPC_STATE_PATH).read_text()))
 def test_storage_failure_blocks_manual_on(self):
  c=self.api();self.now+=1610
  with patch('bioreactor_v3.src.co2_history.os.replace',side_effect=OSError('disk unavailable')):
   with self.assertRaises(RuntimeError):c.manual('CO2','closed',.05)
  self.assertEqual(c.relays.states()['CO2'],'open')
