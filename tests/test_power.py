import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from marketdesk.autotrading import IST
from marketdesk.power import SleepGuard, remaining_seconds


class PowerTests(unittest.TestCase):
    def test_window_boundaries_and_weekend(self):
        for hour, minute, expected in [(8,59,0),(9,0,24000),(15,39,60),(15,40,0)]:
            clock=datetime(2026,10,12,hour,minute,tzinfo=IST)
            self.assertEqual(remaining_seconds(clock.astimezone(timezone.utc)),expected)
        self.assertEqual(remaining_seconds(datetime(2026,10,11,10,tzinfo=IST)),0)

    @patch('marketdesk.power.sys.platform','darwin')
    @patch('marketdesk.power.subprocess.Popen')
    def test_one_idle_assertion_parent_bound_released_outside_window(self, spawn):
        process=Mock(pid=123);process.poll.return_value=None;spawn.return_value=process
        guard=SleepGuard();clock=datetime(2026,10,12,10,tzinfo=IST)
        guard.tick(clock);guard.tick(clock)
        spawn.assert_called_once()
        command=spawn.call_args.args[0]
        self.assertEqual(command[:4],['/usr/bin/caffeinate','-i','-t','20400'])
        self.assertIn('-w',command);self.assertNotIn('-d',command)
        guard.tick(datetime(2026,10,12,16,tzinfo=IST))
        process.terminate.assert_called_once();process.wait.assert_called_once()
        self.assertEqual(guard.snapshot()['status'],'outside_hours')

    @patch('marketdesk.power.sys.platform','darwin')
    @patch('marketdesk.power.subprocess.Popen')
    def test_exited_helper_restarts(self, spawn):
        process=Mock(pid=123);process.poll.return_value=1;spawn.return_value=process
        guard=SleepGuard();clock=datetime(2026,10,12,10,tzinfo=IST)
        guard.tick(clock);guard.tick(clock)
        self.assertEqual(spawn.call_count,2)
