import csv,json,tempfile,unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import Mock,patch
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.importer import import_exports
from marketdesk.candle_fallback import accept
from marketdesk.automatic_repair import repair_extra
from marketdesk.maintenance import Maintenance
import test_working_universe as scope_tests

class AlternateDataTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name);self.db=self.root/'db';init(self.db)
  with connect(self.db) as db:db.execute("INSERT INTO instruments VALUES('M&M','NSE_EQ|INE101A01026','INE101A01026','M&M','Auto')")
 def test_sanitized_export_retains_real_symbol(self):
  folder=self.root/'imports';j=folder/'RAW_CANDLES/json';c=folder/'RAW_CANDLES/csv';j.mkdir(parents=True);c.mkdir(parents=True)
  (folder/'VALID_STOCK_INPUT.csv').write_text('symbol,instrument_key\nM&M,NSE_EQ|INE101A01026\n')
  (j/'M_M.json').write_text(json.dumps({'symbol':'M&M','instrument_key':'NSE_EQ|INE101A01026','requested_at_utc':'2026-10-01T00:00:00+00:00'}))
  (c/'M_M.csv').write_text('timestamp,open,high,low,close,volume\n2026-10-01T00:00:00+05:30,100,101,99,100,1000\n')
  with patch('marketdesk.importer.ROOT',self.root):import_exports(folder,self.db,['M&M'])
  with connect(self.db) as db:self.assertEqual([r[0] for r in db.execute('SELECT symbol FROM candles')],['M&M'])
 def test_alternate_identity_and_prices_are_never_silently_trusted(self):
  rows=[['2026-10-01T00:00:00+05:30',100,101,99,100,1000]]
  meta={'symbol':'M&M.NS','currency':'INR','exchangeName':'NSI'}
  with self.assertRaises(ValueError):accept(self.db,'M&M','INE101A01026',{**meta,'symbol':'OTHER.NS'},rows)
  self.assertEqual(accept(self.db,'M&M','INE101A01026',meta,rows),1)
  with connect(self.db) as db:self.assertEqual(db.execute("SELECT count(*) FROM issues WHERE kind='candle'").fetchone()[0],1)
  with connect(self.db) as db:db.execute("UPDATE candles SET source='upstox_daily_export'")
  with self.assertRaisesRegex(ValueError,'disagree'):accept(self.db,'M&M','INE101A01026',meta,[['2026-10-01T00:00:00+05:30',200,201,199,200,1000]])
  with connect(self.db) as db:self.assertEqual(db.execute('SELECT close FROM candles').fetchone()[0],100)
 def test_unknown_annual_context_does_not_invent_growth(self):
  r=repair_extra('M&M','INE101A01026',{})
  self.assertNotIn('summary',r);self.assertEqual(r['automatic_repair']['attempts'][0]['status'],'unresolved')
 def test_growth_falls_back_to_verified_company_report(self):
  d={'latest':{'period':'Mar 2026','statement_type':'consolidated'}}
  with patch('marketdesk.growth_repair.recover',side_effect=ValueError('unavailable')),patch('marketdesk.report_growth.recover_report_growth',return_value={'status':'verified'}) as fallback,patch('marketdesk.report_growth.apply_growth',return_value={**d,'summary':{'net_profit_growth_yoy':10}}):
   r=repair_extra('M&M','INE101A01026',d)
   fallback.assert_called_once();self.assertEqual(r['summary']['net_profit_growth_yoy'],10);self.assertNotIn('summary',d)

class MaintenanceTests(unittest.TestCase):
 def setUp(self):
  self.fixture=scope_tests.WorkingUniverseTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups);self.path=self.fixture.path;self.refresh=Mock();self.m=Maintenance(self.path,self.refresh);self.clock=datetime(2026,10,15,8,tzinfo=timezone.utc)
 def test_loop_survives_when_recording_the_error_also_fails(self):
  import sqlite3
  self.m.tick=Mock(side_effect=sqlite3.OperationalError('unable to open database file'))
  self.m.stop_event.wait=lambda seconds:self.m.stop_event.set()
  with patch('marketdesk.maintenance.connect',side_effect=sqlite3.OperationalError('unable to open database file')):self.m.run()
  self.m.tick.assert_called_once()
 def test_running_job_not_duplicated_and_interrupted_job_resumes(self):
  with connect(self.path) as db:set_state(db,'refresh',{'working_universe':'nifty500','status':'running','started_at':self.clock.isoformat(),'mode':'fundamental_repair'})
  self.m.tick(self.clock);self.refresh.start.assert_not_called()
  with connect(self.path) as db:
   r=get_state(db,'refresh');r['status']='interrupted';set_state(db,'refresh',r)
  self.m.tick(self.clock);self.refresh.start.assert_called_once_with([],'fundamental_repair')
 def test_new_day_schedules_candles_without_basket_enablement(self):
  self.m.tick(self.clock);self.refresh.start.assert_called_once_with([],'candles')

if __name__=='__main__':unittest.main()
