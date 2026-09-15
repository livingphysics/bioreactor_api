"""Timing accounting must include delayed OFF writes and survive write failures."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'bioreactor-api'))
from relay_controller import RelayController
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from co2_response import finish_pulse


class RelayTimingTests(unittest.TestCase):
    def test_delayed_off_is_counted_and_published_after_write(self):
        clock = [10.0]
        changes = []
        def write(name, energized):
            if not energized:
                clock[0] += 0.603  # emulate the delay found in production
        relay = RelayController()
        relay.configure(set_fn=write, get_fn=lambda:{'CO2':False}, names=['CO2'],
                        on_change=changes.append)
        with patch('relay_controller.time.monotonic', side_effect=lambda:clock[0]):
            relay._set('CO2',True)
            clock[0] += 0.25
            relay._set('CO2',False)
            self.assertAlmostEqual(relay.closed_seconds()['CO2'],0.853)
            self.assertAlmostEqual(changes[-1]['CO2'],0.853)
            self.assertAlmostEqual(relay.status()['closed_seconds']['CO2'],0.853)

    def test_failed_off_preserves_open_interval(self):
        clock = [10.0]
        def write(name, energized):
            if not energized:
                raise RuntimeError('GPIO failed')
        relay = RelayController()
        relay.configure(set_fn=write, get_fn=lambda:{'CO2':True}, names=['CO2'])
        with patch('relay_controller.time.monotonic', side_effect=lambda:clock[0]):
            relay._set('CO2',True)
            clock[0] = 10.25
            with self.assertRaises(RuntimeError):relay._set('CO2',False)
            clock[0] = 11
            self.assertEqual(relay.closed_seconds()['CO2'],1)

    def test_failed_on_is_not_counted(self):
        def write(name, energized):raise RuntimeError('GPIO failed')
        relay = RelayController()
        relay.configure(set_fn=write,get_fn=lambda:{'CO2':False},names=['CO2'])
        with self.assertRaises(RuntimeError):relay._set('CO2',True)
        self.assertEqual(relay.closed_seconds()['CO2'],0)

    def test_calibration_records_completed_duration(self):
        replies=iter([
            {'states':{'CO2':'closed'},'pending':{'CO2':0.1},'closed_seconds':{'CO2':1.1}},
            {'states':{'CO2':'open'},'pending':{},'closed_seconds':{'CO2':1.853}},
        ])
        with patch('co2_response.time.sleep'):
            self.assertAlmostEqual(finish_pulse(lambda _:next(replies),1),0.853)

    def test_calibration_rejects_missing_duration(self):
        state={'states':{'CO2':'open'},'pending':{},'closed_seconds':{'CO2':1}}
        with self.assertRaises(RuntimeError):finish_pulse(lambda _:state,1)


if __name__ == '__main__':unittest.main()
