import unittest
from datetime import datetime
from marketdesk.marks import display_mark

class MarksTests(unittest.TestCase):
 def quote(self,stamp='2026-10-15T15:29:00+05:30'):
  return dict(price=100,trade_time=datetime.fromisoformat(stamp).timestamp()*1000)
 def candle(self,**changes):
  return dict(ts='2026-10-15T00:00:00+05:30',close=102,source='upstox_daily_export',observed_at='2026-10-15T16:00:00+05:30')|changes
 def test_after_close_replaces_mark_but_preserves_last_trade(self):
  p=display_mark(self.quote(),self.candle(),datetime.fromisoformat('2026-10-15T17:00:00+05:30'))
  self.assertEqual(p['mark'],102);self.assertEqual(p['last_trade_mark'],100)
  self.assertEqual(p['mark_source'],'official_daily_close')
 def test_intraday_future_and_unverified_bars_never_replace_quote(self):
  for clock,changes in [('2026-10-15T15:00:00+05:30',{}),('2026-10-15T17:00:00+05:30',{'source':'yahoo'}),('2026-10-15T17:00:00+05:30',{'observed_at':'2026-10-15T14:00:00+05:30'})]:
   self.assertEqual(display_mark(self.quote(),self.candle(**changes),datetime.fromisoformat(clock))['mark'],100)
 def test_next_session_quote_wins_over_prior_close(self):
  p=display_mark(self.quote('2026-10-16T09:20:00+05:30'),self.candle(),datetime.fromisoformat('2026-10-16T09:21:00+05:30'))
  self.assertEqual(p['mark_source'],'last_trade');self.assertEqual(p['official_close'],102)
 def test_missing_quote_uses_verified_completed_close(self):
  p=display_mark(None,self.candle(),datetime.fromisoformat('2026-10-16T09:21:00+05:30'))
  self.assertEqual(p['mark'],102);self.assertIsNone(p['last_trade_mark'])
