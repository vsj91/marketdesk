import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.paper import init_paper
from marketdesk.buy_universe import KEY,SOURCE
from marketdesk.working_universe import research_symbols,symbol_filter,scope_report
from marketdesk.analytics import listing
from marketdesk.quality import audit
from marketdesk.refresh import Refresh
from marketdesk.fundamental_repair import targets
from marketdesk.server import App
from marketdesk.replay import load_snapshot
from marketdesk.universe_exits import process,KEY as EXIT_KEY,exit_guard

class WorkingUniverseTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'db';init(self.path);init_paper(self.path)
  self.members={'INCLUDED':'INE000000001',**{'M'+str(i):'INE'+str(i+2).zfill(9) for i in range(499)}}
  with connect(self.path) as db:
   for s,isin in [('INCLUDED','INE000000001'),('OUTSIDE','INE000009999'),('HELD','INE000009998')]:
    db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(s,'NSE_EQ|'+isin,isin,s,'Test'))
    db.execute('INSERT INTO fundamentals VALUES(?,?,?)',(s,json.dumps({'status':'PARTIAL','missing_fields':['equity']}),datetime.now(timezone.utc).isoformat()))
    db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',(s,'2026-10-14T00:00:00+05:30',100,101,99,100,1000,'fixture','2026-10-14T00:00:00+05:30'))
   db.execute("INSERT INTO positions VALUES('trader','HELD',1,100)")
   set_state(db,'working_universe','nifty500');set_state(db,KEY,{'members':self.members,'source':SOURCE,'fetched_at':datetime.now(timezone.utc).isoformat()})
 def test_research_and_health_never_count_archived_or_held(self):
  self.assertEqual([r['symbol'] for r in listing(self.path)],['INCLUDED'])
  self.assertEqual(audit(self.path)['stocks'],1)
  h=App(self.path,read_only=True).health()
  self.assertEqual(h['counts']['instruments'],1);self.assertEqual(h['counts']['candles'],1);self.assertEqual(h['partial_fundamentals'],1);self.assertEqual(h['missing_fields']['equity'],1)
  self.assertEqual([r['symbol'] for r in targets(self.path)],['INCLUDED'])
  self.assertEqual(load_snapshot(self.path)['symbols'],['INCLUDED'])
  with connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM instruments').fetchone()[0],3)
 def test_missing_membership_fails_closed_but_pending_hold_keeps_quotes(self):
  with connect(self.path) as db:
   db.execute('DELETE FROM state WHERE key=?',(KEY,))
   self.assertEqual(research_symbols(db),set())
   self.assertEqual([r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db,include_holdings=True))],['HELD'])
  self.assertEqual(listing(self.path),[])
 def test_identity_mismatch_excluded(self):
  with connect(self.path) as db:
   db.execute("UPDATE instruments SET isin='INE000004444' WHERE symbol='INCLUDED'")
   self.assertEqual(research_symbols(db),set())
 def test_refresh_jobs_intersect_scope_and_exclude_outside_fundamentals(self):
  with patch('marketdesk.refresh.threading.Thread') as thread,patch('marketdesk.refresh.load_token',return_value='token'):
   job=Refresh(self.path);job.start([],'fundamentals')
   self.assertEqual([r['symbol'] for r in thread.call_args.kwargs['args'][0]],['INCLUDED']);job.lock.release()
   job.start([],'candles');self.assertEqual({r['symbol'] for r in thread.call_args.kwargs['args'][0]},{'INCLUDED','HELD'});job.lock.release()
   with self.assertRaisesRegex(ValueError,'No matching'):job.start(['OUTSIDE'],'fundamentals')
   self.assertFalse(job.lock.locked())
 def test_repair_report_drops_archived_rows_and_totals(self):
  audit(self.path)
  with connect(self.path) as db:r=scope_report(db,{'total':2333,'results':[{'symbol':'OUTSIDE'},{'symbol':'INCLUDED'}],'quality_before':{'stocks':2333}})
  self.assertEqual(r['total'],1);self.assertEqual(r['quality']['stocks'],1);self.assertEqual(r['quality_before'],{})

 def test_repair_report_classifies_every_blocked_stock(self):
  audit(self.path)
  with connect(self.path) as db:r=scope_report(db,{'working_universe':'nifty500','results':[]})
  self.assertEqual(len(r['results']),1)
  self.assertTrue(r['results'][0]['blocker_category'].startswith('Too new, eligible from '))
  self.assertIn('Fewer than 80 completed daily candles',r['results'][0]['blocker_reason'])
 def test_repair_report_allows_null_report_from_running_refresh(self):
  audit(self.path)
  with connect(self.path) as db:r=scope_report(db,{'results':[{'symbol':'INCLUDED','annual_report':None}]})
  self.assertEqual(r['annual_reports_verified'],0)
  self.assertEqual(r['quality']['stocks'],1)
  self.assertTrue(r['results'][0]['blocker_category'].startswith('Too new, eligible from '))
 def test_exit_retry_is_only_for_requested_holding_and_preserves_failure(self):
  with connect(self.path) as db:set_state(db,EXIT_KEY,[{'portfolio':'trader','symbol':'HELD','max_quantity':1}])
  with patch('marketdesk.universe_exits.refresh_held_quotes'),patch('marketdesk.universe_exits.trade',side_effect=ValueError('stale')) as trade:
   process(self.path);self.assertEqual(trade.call_args.args[1]['symbol'],'HELD')
  with connect(self.path) as db:
   self.assertEqual(len(get_state(db,EXIT_KEY)),1);self.assertEqual(db.execute("SELECT quantity FROM positions WHERE symbol='HELD'").fetchone()[0],1)
   db.execute("UPDATE positions SET quantity=0 WHERE symbol='HELD'")
  with patch('marketdesk.universe_exits.refresh_held_quotes'),patch('marketdesk.universe_exits.trade') as trade:
   process(self.path);trade.assert_not_called()
  with connect(self.path) as db:self.assertEqual(get_state(db,EXIT_KEY),[])

if __name__=='__main__':unittest.main()
