import copy,json,math,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.claude_trader import ClaudeTrader,STRATEGY,features,eligible,quantity,fits,limits,RULE
from marketdesk.autotrading import AutoTrading,IST,RULES
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.paper import init_paper,trade
from support import seed_quality,fixture_book

class ClaudeTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'paper.db'
  init(self.path);init_paper(self.path);self.auto=AutoTrading(self.path);self.c=ClaudeTrader(self.path)
  self.clock=datetime(2026,10,13,9,16,tzinfo=IST)
  self.signal=dict(as_of='2026-10-12',close=102,atr=2,dma50=99,dma200=90,high20=101,volume_ratio=1.5,turnover20=20_000_000,rank=.2,quality=True)
  self.c.benchmark=dict(as_of='2026-10-12',allowed=True)
  with connect(self.path) as db:
   set_state(db,'auto_config',{'trader':{'enabled':True,'strategy':STRATEGY},'investor':{'enabled':False,'unchanged':123}})
   set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
  self.add_stock('TEST');self.patch=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.patch.start();self.addCleanup(self.patch.stop)
 def add_stock(self,symbol):
  with connect(self.path) as db:
   db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,'NSE_EQ|'+symbol,symbol,symbol,'Test'))
   db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,102,100,int(self.clock.timestamp()*1000),self.clock.isoformat(),'test'))
  seed_quality(self.path,symbol,self.clock)
 def order(self,side='BUY',q=50,symbol='TEST'):
  import uuid
  return trade(self.path,dict(portfolio='trader',symbol=symbol,side=side,quantity=q,request_id=str(uuid.uuid4())),self.clock,auto_guard=self.c.guard(self.signal,self.clock))
 def price(self,price):
  with connect(self.path) as db:db.execute('UPDATE quotes SET price=?,trade_time=?,received_at=? WHERE symbol=?',(price,int(self.clock.timestamp()*1000),self.clock.isoformat(),'TEST'))
 def test_closed_signal_not_current_quote_breakout(self):
  self.signal['close']=100
  self.assertFalse(eligible(self.signal,103,self.clock,self.c.benchmark))
  self.signal['close']=102
  self.assertTrue(eligible(self.signal,100,self.clock,self.c.benchmark))
  self.assertFalse(eligible(self.signal,119,self.clock,self.c.benchmark))
 def test_benchmark_fail_closed(self):
  for bm in [None,dict(as_of='2026-10-09',allowed=True),dict(as_of='2026-10-12',allowed=False)]:
   self.c.benchmark=bm
   with self.assertRaisesRegex(ValueError,'market filter'):self.order()
 def test_signal_current_day_rejected(self):
  self.signal['as_of']='2026-10-13'
  self.assertFalse(eligible(self.signal,102,self.clock,dict(as_of='2026-10-13',allowed=True)))
 def test_missed_opening_no_late_chase(self):
  self.clock=self.clock.replace(hour=10);self.price(102);seed_quality(self.path,'TEST',self.clock)
  with self.assertRaisesRegex(ValueError,'opening depth'):self.order()
 def test_fill_metadata_restart_no_target_and_stop(self):
  fill=self.order();entry=fill['price'];self.c=ClaudeTrader(self.path)
  with connect(self.path) as db:
   s=db.execute('SELECT * FROM claude_positions').fetchone()
   self.assertAlmostEqual(s['stop'],entry-4)
   self.assertIsNone(self.c.exit_reason(db,'TEST',entry*1.10))
  self.price(entry-5);sold=self.order('SELL');self.assertEqual(sold['side'],'SELL')
  with connect(self.path) as db:self.assertIsNone(db.execute('SELECT * FROM claude_positions').fetchone())
  self.price(102);self.c.benchmark=dict(as_of='2026-10-12',allowed=True)
  with self.assertRaisesRegex(ValueError,'re-entry'):self.order()
 def test_trailing_completed_day_only_ratchet_and_ma_exit(self):
  self.order();s=copy.deepcopy(self.signal);s.update(as_of='2026-10-13',close=120,atr=2)
  self.c.histories={'TEST':[s]}
  with connect(self.path) as db:
   self.c.update_positions(db,self.clock)
   self.assertLess(db.execute('SELECT stop FROM claude_positions').fetchone()[0],100)
  tomorrow=self.clock+timedelta(days=1)
  with connect(self.path) as db:
   self.c.update_positions(db,tomorrow);stop=db.execute('SELECT stop FROM claude_positions').fetchone()[0]
   self.assertEqual(stop,114)
   s.update(as_of='2026-10-14',close=110,atr=10,dma50=115)
   self.c.update_positions(db,tomorrow+timedelta(days=1))
   self.assertEqual(db.execute('SELECT stop FROM claude_positions').fetchone()[0],114)
   self.assertIn('completed close',self.c.exit_reason(db,'TEST',116))
 def test_partial_exit_keeps_stop_state(self):
  self.order();self.price(95)
  def tiny(path,symbol):
   b=fixture_book(path,symbol);b['depth']['buy'][0]['quantity']=100;return b
  with patch('marketdesk.paper.fetch_book',side_effect=tiny):fill=self.order('SELL')
  self.assertEqual(fill['quantity'],10)
  with connect(self.path) as db:
   self.assertIsNotNone(db.execute('SELECT * FROM claude_positions').fetchone())
   self.assertEqual(db.execute("SELECT quantity FROM positions WHERE portfolio='trader'").fetchone()[0],40)
 def test_risk_fee_heat_and_fill_recheck(self):
  q=quantity(102,2,1_000_000,0,100000)
  self.assertTrue(fits(q,102,2,1_000_000,0,100000,102))
  self.assertFalse(fits(q+1,102,2,1_000_000,0,100000,102))
  self.assertEqual(quantity(102,2,1_000_000,30_000,100000),0)
  self.signal['atr']=20
  with self.assertRaisesRegex(ValueError,'risk, heat'):self.order(q=900)
  with connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM claude_positions').fetchone()[0],0)
 def test_legacy_migration_keeps_ledger_and_stop_floor(self):
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('trader','TEST',10,1020)")
   before=[tuple(r) for r in db.execute('SELECT * FROM positions')]
   self.c.histories={'TEST':[self.signal]};self.c.update_positions(db,self.clock)
   self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM positions')])
   s=db.execute('SELECT * FROM claude_positions').fetchone();self.assertTrue(s['legacy']);self.assertAlmostEqual(s['stop'],102*.97)
 def test_missing_legacy_atr_keeps_stop_and_blocks_buys(self):
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('trader','TEST',10,1020)")
   self.assertIn('Legacy',self.c.exit_reason(db,'TEST',95))
   with self.assertRaisesRegex(ValueError,'ATR history'):limits(db,db.execute("SELECT * FROM portfolios WHERE name='trader'").fetchone(),self.clock)
 def test_config_preserves_strategy_and_investor(self):
  before=self.auto.snapshot();self.auto.configure('trader',False);self.auto.configure('trader',True);after=self.auto.snapshot()
  self.assertEqual(after['config']['trader']['strategy'],STRATEGY)
  self.assertEqual(before['config']['investor'],after['config']['investor'])
  self.assertEqual(after['rules']['investor'],RULES['investor']);self.assertEqual(after['rules']['trader'],RULE)
 def test_no_old_daily_loss_circuit(self):
  with connect(self.path) as db:db.execute("INSERT INTO auto_days VALUES('trader','2026-10-13',1000000,1)")
  self.assertEqual(self.order()['side'],'BUY')
 def test_pause_guard_and_stale_quote(self):
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['trader']['enabled']=False;set_state(db,'auto_config',cfg)
  with self.assertRaisesRegex(ValueError,'paused'):self.order()
  with connect(self.path) as db:
   cfg['trader']['enabled']=True;set_state(db,'auto_config',cfg)
   db.execute('UPDATE quotes SET trade_time=trade_time-60000')
  with self.assertRaisesRegex(ValueError,'30 seconds'):self.order()
 def test_three_buys_and_five_holdings(self):
  self.order()
  for symbol in ('SECOND','THIRD'):self.add_stock(symbol);self.order(symbol=symbol)
  self.add_stock('FOURTH')
  with self.assertRaisesRegex(ValueError,'Three new'):self.order(symbol='FOURTH')
 def test_ranked_scan_routes_only_trader_to_claude(self):
  import time
  self.c.histories={'TEST':[self.signal]}
  for symbol,rank in [('LOW',.1),('TOP',.8),('MID',.5)]:
   self.add_stock(symbol);sig=copy.deepcopy(self.signal);sig['rank']=rank;self.c.histories[symbol]=[sig]
  self.c.loaded=time.monotonic();self.c.loaded_day='2026-10-13';self.auto.claude=self.c
  with patch.object(self.c,'refresh_benchmark'):
   self.auto.cycle(self.clock,signals={})
  with connect(self.path) as db:
   self.assertEqual([r[0] for r in db.execute('SELECT symbol FROM trades ORDER BY rowid')],['TOP','MID','TEST'])
   self.assertFalse(get_state(db,'auto_config')['investor']['enabled'])
 def test_max_five_positions_rechecked(self):
  with connect(self.path) as db:
   for i in range(5):
    symbol='HELD'+str(i)
    db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,symbol,symbol,symbol,'test'))
    db.execute('INSERT INTO positions VALUES(?,?,?,?)',('trader',symbol,1,100))
  with self.assertRaisesRegex(ValueError,'Five-position'):self.order()
 def test_missing_benchmark_does_not_block_protective_exit(self):
  import time
  self.order();self.price(95);self.c.benchmark=None;self.c.histories={}
  self.c.loaded=time.monotonic();self.c.loaded_day='2026-10-13'
  with patch.object(self.c,'refresh_benchmark'):status=self.c.scan(self.clock)
  self.assertEqual(status['state'],'data_wait');self.assertEqual(status['fills_this_scan'],1)
 def test_loader_excludes_unfinished_candle(self):
  with connect(self.path) as db:
   db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',('TEST','2026-10-13T00:00:00+05:30',100,500,99,400,999999,'fixture',self.clock.isoformat()))
  self.c.load(self.clock)
  self.assertEqual(self.c.histories['TEST'][-1]['as_of'],'2026-10-12')
 def test_trader_error_does_not_pause_investor(self):
  self.auto.claude=self.c
  with connect(self.path) as db:cfg=get_state(db,'auto_config')
  with patch.object(self.c,'scan',side_effect=ValueError('test')):
   result=self.auto.scan_selected('trader',{},self.clock,False,cfg)
  self.assertEqual(result['state'],'error')
  with connect(self.path) as db:self.assertEqual(get_state(db,'auto_config')['investor'],cfg['investor'])

class IndicatorTests(unittest.TestCase):
 def test_wilder_and_prior_windows(self):
  rows=[]
  for i in range(220):
   c=100+i*.1;rows.append(dict(ts=(datetime(2025,1,1)+timedelta(days=i)).isoformat(),open=c,high=c+2,low=c-1,close=c,volume=100000+i*100))
  f=features(rows)[-1]
  self.assertAlmostEqual(f['atr'],3)
  self.assertAlmostEqual(f['dma50'],sum(r['close'] for r in rows[-51:-1])/50)
  self.assertAlmostEqual(f['high20'],max(r['high'] for r in rows[-21:-1]))
  self.assertAlmostEqual(f['volume_ratio'],(sum(r['volume'] for r in rows[-11:-1])/10)/(sum(r['volume'] for r in rows[-41:-11])/30))
  unchanged=copy.deepcopy(f);rows[-1]['volume']=10000000
  self.assertEqual(features(rows)[-1]['volume_ratio'],unchanged['volume_ratio'])
  rows[-1]['high']+=14
  self.assertAlmostEqual(features(rows)[-1]['atr'],4)

if __name__=='__main__':unittest.main()
