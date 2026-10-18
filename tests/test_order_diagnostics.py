import json,unittest
from unittest.mock import patch
from test_claude_trader import ClaudeTests
from marketdesk.storage import connect
from support import fixture_book

class DiagnosticsTests(unittest.TestCase):
 def setUp(self):
  self.fixture=ClaudeTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
 def test_rejection_preserves_sizing_and_does_not_book_risk(self):
  f=self.fixture;f.enable_fee_cap()
  def shallow(path,symbol):
   book=fixture_book(path,symbol);book['depth']['sell'][0]['quantity']=100;return book
  with patch('marketdesk.paper.fetch_book',side_effect=shallow):
   with self.assertRaises(ValueError):f.order(q=500)
  with connect(f.path) as db:
   r=db.execute('SELECT * FROM order_attempts').fetchone();d=json.loads(r['details'])
   self.assertEqual(r['status'],'blocked');self.assertEqual(d['requested_quantity'],500)
   self.assertEqual(d['matched_quantity'],10);self.assertEqual(d['filled_quantity'],0)
   self.assertEqual(d['risk_used_inr'],0);self.assertGreater(d['proposed_risk_inr'],0)
   self.assertEqual(d['blocked_by'],'fee_floor');self.assertIn('depth',d['binding_constraints'])
   self.assertTrue(r['strategy_version_id'].startswith('claude-v2/'))
   self.assertEqual(db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],0)
 def test_fill_captures_observed_risk_and_version(self):
  f=self.fixture;t=f.order(q=50);d=json.loads(t['execution'])['audit']
  self.assertEqual(d['filled_quantity'],50);self.assertEqual(d['stop_distance'],4)
  self.assertGreater(d['risk_used_inr'],200);self.assertEqual(d['equity_before'],1000000)
  self.assertEqual(d['strategy_version_id'],t['strategy_version_id'])
  with connect(f.path) as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM order_attempts').fetchone()[0],1)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_event_evidence').fetchone()[0],1)
