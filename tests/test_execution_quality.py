import copy,json,tempfile,unittest,uuid
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.paper import init_paper,trade,portfolios
from marketdesk.execution import normalize,charges,match,fetch_book
from marketdesk.quality import inspect_history,audit,source_reasons,financial_reasons
from support import fixture_book,seed_quality

class ExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';init(self.path);init_paper(self.path)
  self.clock=datetime(2026,10,12,5,tzinfo=timezone.utc)
  with connect(self.path) as db:
   db.execute("INSERT INTO instruments VALUES('TEST','NSE_EQ|TEST','TEST','Test','Test')")
   db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',('TEST',100,99,int(self.clock.timestamp()*1000),self.clock.isoformat(),'test'))
   set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
  seed_quality(self.path,'TEST',self.clock)
  self.book=fixture_book(self.path,'TEST');self.book['depth']['sell']=[{'price':100.1,'quantity':100},{'price':100.2,'quantity':200},{'price':101,'quantity':10000}]
  self.mock=patch('marketdesk.paper.fetch_book',side_effect=lambda *a:copy.deepcopy(self.book));self.mock.start();self.addCleanup(self.mock.stop)
 def tearDown(self):self.tmp.cleanup()
 def order(self,qty=50,side='BUY',request=None,portfolio='trader'):
  return trade(self.path,{'portfolio':portfolio,'symbol':'TEST','side':side,'quantity':qty,'request_id':request or str(uuid.uuid4())},self.clock)
 def test_partial_vwap_cancels_remainder_and_no_reuse_after_restart(self):
  rid=str(uuid.uuid4());a=self.order(request=rid);e=json.loads(a['execution'])
  self.assertEqual(a['quantity'],30);self.assertEqual(a['requested_quantity'],50)
  self.assertAlmostEqual(a['price'],(10*100.1+20*100.2)/30)
  self.assertEqual(e['unfilled_quantity'],20);self.assertEqual(e['status'],'PARTIAL_CANCELLED')
  init_paper(self.path);self.assertEqual(self.order(request=rid),a)
  with self.assertRaisesRegex(ValueError,'unused depth'):self.order()
  with connect(self.path) as db:self.assertEqual(db.execute('SELECT SUM(quantity) FROM depth_used').fetchone()[0],30)
 def test_concurrent_identical_requests_fill_once(self):
  rid=str(uuid.uuid4())
  with ThreadPoolExecutor(2) as pool:rows=list(pool.map(lambda _:self.order(request=rid),range(2)))
  self.assertEqual(rows[0]['id'],rows[1]['id'])
  with connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],1)
 def test_no_depth_no_fabricated_fill(self):
  with patch('marketdesk.paper.fetch_book',side_effect=ValueError('Depth unavailable')):
   with self.assertRaisesRegex(ValueError,'Depth unavailable'):self.order()
  self.assertEqual(portfolios(self.path)[0]['cash'],1000000)
 def test_stale_future_and_receipt_ages_rejected(self):
  for field in ('timestamp','received_at'):
   for seconds in (-6,1):
    original=self.book[field];self.book[field]=(self.clock+timedelta(seconds=seconds)).isoformat()
    with self.assertRaises(ValueError):self.order()
    self.book[field]=original
  self.book['trade_time']-=31000
  with self.assertRaises(ValueError):self.order()
 def test_pause_or_market_change_during_fetch(self):
  def closed(*args):
   with connect(self.path) as db:set_state(db,'stream',{'status':'disconnected'})
   return self.book
  with patch('marketdesk.paper.fetch_book',side_effect=closed):
   with self.assertRaisesRegex(ValueError,'open NSE'):self.order()
  self.assertEqual(portfolios(self.path)[0]['trades'],[])
 def test_budget_failure_does_not_consume_depth(self):
  with self.assertRaisesRegex(ValueError,'cash'):self.order(qty=10000)
  self.assertEqual(self.order(qty=10)['quantity'],10)
 def test_cost_breakdown_and_dp_only_once_per_day(self):
  c=charges(100000,'BUY');self.assertEqual(c['brokerage'],20);self.assertEqual(c['stt'],100);self.assertEqual(c['stamp'],15);self.assertEqual(c['exchange'],3.07);self.assertEqual(c['sebi'],.1);self.assertEqual(c['gst'],4.15);self.assertEqual(c['total'],142.32)
  self.order(qty=20);a=self.order(qty=5,side='SELL');b=self.order(qty=5,side='SELL')
  self.assertEqual(json.loads(a['execution'])['charges']['dp'],20)
  self.assertEqual(json.loads(b['execution'])['charges']['dp'],0)
 def test_price_protection_never_walks_far_level(self):
  self.assertEqual(self.order(qty=500)['quantity'],30)
 def test_data_gate_blocks_manual_buy_allows_exit(self):
  self.order(qty=10)
  with connect(self.path) as db:db.execute("UPDATE quality SET report=?",(json.dumps({'price_reasons':['Missing session']}),))
  with self.assertRaisesRegex(ValueError,'Missing session'):self.order()
  self.assertEqual(self.order(qty=5,side='SELL')['quantity'],5)
 def test_investor_missing_fundamentals_cannot_manual_bypass(self):
  with self.assertRaisesRegex(ValueError,'Annual financial'):self.order(qty=1,portfolio='investor')
 def test_candle_change_after_audit_blocks_buy(self):
  with connect(self.path) as db:db.execute("UPDATE candles SET volume=0 WHERE ts=(SELECT MAX(ts) FROM candles)")
  with self.assertRaisesRegex(ValueError,'Zero volume'):self.order(qty=1)
 def test_corporate_action_previous_close_mismatch_blocks_exit(self):
  self.order(qty=10)
  with connect(self.path) as db:db.execute('UPDATE quotes SET previous_close=50')
  with self.assertRaisesRegex(ValueError,'position review'):self.order(qty=1,side='SELL')
 def test_duplicate_launch_cannot_reset_running_app(self):
  from marketdesk.server import serve
  with patch('marketdesk.server.Server',side_effect=OSError('port occupied')),patch('marketdesk.server.App') as app:
   with self.assertRaises(OSError):serve(self.path)
   app.assert_not_called()
 def test_invalid_books(self):
  raw={'instrument_token':'NSE_EQ|TEST','symbol':'TEST','timestamp':self.clock.isoformat(),'last_trade_time':int(self.clock.timestamp()*1000),'last_price':100,'lower_circuit_limit':80,'upper_circuit_limit':120,'depth':{'buy':[{'price':99.9,'quantity':100}],'sell':[{'price':100.1,'quantity':100}]}}
  self.assertTrue(normalize(raw,'NSE_EQ|TEST','TEST',self.clock))
  for kind in ('cross','missing','nan','fraction','circuit','identity','wide','naive'):
   r=copy.deepcopy(raw)
   if kind=='cross':r['depth']['sell'][0]['price']=99
   if kind=='missing':r['depth']['buy']=[]
   if kind=='nan':r['depth']['sell'][0]['price']='nan'
   if kind=='fraction':r['depth']['sell'][0]['quantity']=.5
   if kind=='circuit':r['upper_circuit_limit']=100
   if kind=='identity':r['symbol']='OTHER'
   if kind=='wide':r['depth']['sell'][0]['price']=103
   if kind=='naive':r['timestamp']='2026-10-12T05:00:00'
   with self.assertRaises(ValueError,msg=kind):normalize(r,'NSE_EQ|TEST','TEST',self.clock)
 def test_rest_failure_is_read_only_and_sanitized(self):
  with patch('marketdesk.execution.load_token',return_value='private-token'),patch('marketdesk.execution.requests.get') as get:
   get.return_value.status_code=401
   with self.assertRaisesRegex(ValueError,'HTTP 401'):fetch_book(self.path,'TEST')
   self.assertEqual(get.call_args.args[0],'https://api.upstox.com/v3/market-quote/quotes')

class QualityTests(unittest.TestCase):
 def setUp(self):
  self.clock=datetime(2026,10,12,5,tzinfo=timezone.utc)
  day=self.clock.date()-timedelta(days=1);days=[]
  while len(days)<201:
   if day.weekday()<5:days.append(day.isoformat())
   day-=timedelta(days=1)
  self.days=sorted(days)
  self.rows=[{'ts':d+'T00:00:00+05:30','open':100,'high':101,'low':99,'close':100,'volume':100000} for d in self.days]
 def test_clean_history_and_missing_session_not_interpolated(self):
  self.assertEqual(inspect_history(self.rows,self.days,self.clock),[])
  del self.rows[100]
  self.assertTrue(any('gaps against' in r for r in inspect_history(self.rows,self.days,self.clock)))
 def test_split_jump_zero_volume_future_and_short_history(self):
  self.rows[-1].update(open=50,close=50,high=51,low=49,volume=0)
  reasons=inspect_history(self.rows,self.days,self.clock)
  self.assertTrue(any('discontinuity' in r for r in reasons));self.assertIn('Zero volume in recent 20 sessions',reasons)
  self.rows[-1]['ts']='2026-10-12T00:00:00+05:30'
  self.assertIn('Current or future daily candle',inspect_history(self.rows,self.days,self.clock))
  self.assertIn('Fewer than 200 completed daily candles',inspect_history(self.rows[-20:],self.days,self.clock))
 def test_duplicate_invalid_ohlc_and_stale(self):
  self.rows[-1]['ts']=self.rows[-2]['ts'];self.rows[-1]['high']=10
  result=inspect_history(self.rows,self.days,self.clock)
  self.assertIn('Duplicate daily sessions',result);self.assertIn('Invalid OHLCV values',result)
  self.assertIn('Daily history is stale',inspect_history(self.rows[:-10],self.days,self.clock))
 def test_old_evidence_not_refreshed_by_import_timestamp(self):
  times={k:self.clock.isoformat() for k in ('income_statement','balance_sheet','cash_flow')}
  self.assertEqual(source_reasons({},times,self.clock),[])
  times['balance_sheet']=(self.clock-timedelta(days=31)).isoformat()
  self.assertTrue(source_reasons({},times,self.clock))
  f={'selected_source':'nse_audited_filing','fallbacks':{'nse':{'observed_at':(self.clock-timedelta(days=31)).isoformat()}}}
  self.assertTrue(source_reasons(f,times,self.clock))
 def test_audit_persists_exclusion_evidence(self):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'db';init(path)
   with connect(path) as db:
    for symbol in ('GOOD','GAP','OTHER'):
     db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,'NSE_EQ|'+symbol,symbol,symbol,None))
     for i,r in enumerate(self.rows):
      if symbol=='GAP' and i==100:continue
      db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',(symbol,r['ts'],r['open'],r['high'],r['low'],r['close'],r['volume'],'test',self.clock.isoformat()))
   summary=audit(path,self.clock);self.assertEqual(summary['trader_ready'],2);self.assertEqual(summary['investor_ready'],0)
   with connect(path) as db:
    report=json.loads(db.execute("SELECT report FROM quality WHERE symbol='GAP'").fetchone()[0]);self.assertFalse(report['trader_ready']);self.assertTrue(report['price_reasons'])

if __name__=='__main__':unittest.main()
