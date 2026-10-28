"""The retired remote tracking routes cannot poll or actuate any reactor."""
import unittest
from unittest.mock import patch
import test_co2
import main
from fastapi.testclient import TestClient

class RemovedTrackingTests(unittest.TestCase):
 def test_routes_state_and_openapi_retire_tracking(self):
  with TestClient(main.app) as client,patch('urllib.request.urlopen',side_effect=AssertionError('remote access forbidden')):
   headers={'Authorization':'Bearer local-test-key'}
   for method,path in [('get','/api/co2/tracking'),('post','/api/co2/tracking/start'),('post','/api/co2/tracking/stop'),('get','/api/co2/tracking/log')]:
    with self.subTest(path=path):self.assertEqual(getattr(client,method)(path,headers=headers).status_code,404)
   state=client.get('/api/state',headers=headers).json()
   self.assertNotIn('co2_tracking',state)
   self.assertNotIn('master_co2',main._actuator_signals())
   self.assertNotIn('co2_reference',main._actuator_signals())
   self.assertFalse(hasattr(main.co2_controller,'tracking'))
   self.assertFalse(any('/co2/tracking' in p for p in main.app.openapi()['paths']))
   self.assertEqual(client.get('/api/co2/controller',headers=headers).status_code,200)
   self.assertEqual(client.post('/api/co2/control',headers=headers,json={'target_percent':-1}).status_code,422)

if __name__=='__main__':unittest.main()
