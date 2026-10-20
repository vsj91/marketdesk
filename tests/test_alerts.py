import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from marketdesk.alerts import Alerts, market_hours, mac_notify
from marketdesk.autotrading import IST, init_auto, event
from marketdesk.storage import init, connect, set_state, get_state

OPEN = datetime(2026, 10, 12, 10, 0, tzinfo=IST)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.db = Path(self.t.name)/'db'
        init(self.db)
        init_auto(self.db)
        self.sent = []

    def alerts(self, clock=OPEN, feed=None):
        return Alerts(self.db, feed, notify=lambda title, message: self.sent.append(title), clock=clock)

    def state(self, **values):
        with connect(self.db) as db:
            for key, value in values.items():
                set_state(db, key, value)

    def healthy(self, clock):
        self.state(stream={'status': 'connected'}, auto_status={'trader': {'checked_at': clock.isoformat()}}, refresh={'status': 'complete'})

    def test_market_hours_weekdays_only(self):
        self.assertTrue(market_hours(OPEN))
        self.assertFalse(market_hours(OPEN.replace(hour=9, minute=14)))
        self.assertFalse(market_hours(OPEN.replace(hour=15, minute=30)))
        self.assertFalse(market_hours(OPEN+timedelta(days=6)))

    def test_new_engine_pause_notifies_once_and_old_ones_are_ignored(self):
        with connect(self.db) as db:
            event(db, 'trader', 'error', 'Old pause.')
        alerts = self.alerts()
        self.healthy(OPEN)
        alerts.tick(OPEN)
        self.assertEqual(self.sent, [])
        with connect(self.db) as db:
            event(db, 'trader', 'error', 'Engine paused.')
            event(db, 'investor', 'error', 'Engine paused.')
        alerts.tick(OPEN)
        alerts.tick(OPEN)
        self.assertEqual(self.sent, ['Paper trading paused'])
        self.assertIn('investor, trader', alerts.snapshot()['recent'][0]['message'])

    def test_feed_down_alerts_after_grace_then_recovers_once(self):
        alerts = self.alerts()
        self.healthy(OPEN)
        self.state(stream={'status': 'reconnecting'})
        alerts.tick(OPEN)
        alerts.tick(OPEN+timedelta(minutes=2))
        self.assertEqual(self.sent, [])
        alerts.tick(OPEN+timedelta(minutes=3))
        alerts.tick(OPEN+timedelta(minutes=4))
        self.assertEqual(self.sent, ['Live prices down'])
        self.healthy(OPEN+timedelta(minutes=5))
        alerts.tick(OPEN+timedelta(minutes=5))
        self.assertEqual(self.sent, ['Live prices down', 'Live prices back'])

    def test_closed_market_feed_is_not_an_alert(self):
        evening = OPEN.replace(hour=20)
        alerts = self.alerts(evening)
        self.healthy(evening)
        self.state(stream={'status': 'stopped'}, auto_status={'trader': {'checked_at': (evening+timedelta(minutes=10)).isoformat()}})
        alerts.tick(evening+timedelta(minutes=10))
        self.assertEqual(self.sent, [])

    @patch('marketdesk.alerts.load_token', return_value='token')
    def test_stopped_feed_restarts_itself_at_most_every_five_minutes(self, _):
        feed = Mock()
        alerts = self.alerts(feed=feed)
        self.healthy(OPEN)
        self.state(stream={'status': 'stopped'})
        alerts.tick(OPEN)
        feed.start.assert_not_called()
        alerts.tick(OPEN+timedelta(minutes=1))
        alerts.tick(OPEN+timedelta(minutes=2))
        self.assertEqual(feed.start.call_count, 1)
        alerts.tick(OPEN+timedelta(minutes=6))
        self.assertEqual(feed.start.call_count, 2)

    @patch('marketdesk.alerts.load_token', return_value='token')
    def test_rejected_token_alerts_and_is_never_retried(self, _):
        feed = Mock()
        alerts = self.alerts(feed=feed)
        self.healthy(OPEN)
        self.state(stream={'status': 'auth_failed'})
        for minutes in (0, 5, 10):
            self.state(auto_status={'trader': {'checked_at': (OPEN+timedelta(minutes=minutes)).isoformat()}})
            alerts.tick(OPEN+timedelta(minutes=minutes))
        feed.start.assert_not_called()
        self.assertEqual(self.sent, ['Upstox token rejected'])

    def test_silent_engine_alerts_after_startup_grace(self):
        alerts = self.alerts()
        self.healthy(OPEN)
        alerts.tick(OPEN+timedelta(minutes=4))
        self.assertEqual(self.sent, [])
        alerts.tick(OPEN+timedelta(minutes=6))
        self.assertEqual(self.sent, ['Trading engine not responding'])
        self.healthy(OPEN+timedelta(minutes=7))
        alerts.tick(OPEN+timedelta(minutes=7))
        self.assertEqual(self.sent[-1], 'Trading engine running')

    def test_failed_refresh_alerts_once(self):
        alerts = self.alerts()
        self.healthy(OPEN)
        self.state(refresh={'status': 'failed', 'stage': 'Candle refresh stopped'})
        alerts.tick(OPEN)
        alerts.tick(OPEN)
        self.assertEqual(self.sent, ['Data refresh failed'])

    def test_repeated_restarts_alert_and_old_starts_expire(self):
        self.alerts(OPEN)
        self.alerts(OPEN+timedelta(minutes=1))
        self.assertEqual(self.sent, [])
        self.alerts(OPEN+timedelta(minutes=2))
        self.assertEqual(self.sent, ['MarketDesk keeps restarting'])
        self.alerts(OPEN+timedelta(hours=1))
        with connect(self.db) as db:
            self.assertEqual(len(get_state(db, 'service_starts')), 1)

    @patch('marketdesk.alerts.sys.platform', 'darwin')
    @patch('marketdesk.alerts.subprocess.run')
    def test_notification_text_is_passed_as_arguments(self, run):
        mac_notify('Title', 'Say "hi" & quit')
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ['Title', 'Say "hi" & quit'])
        self.assertNotIn('hi', command[2])


if __name__ == '__main__':
    unittest.main()
