import copy,json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
from marketdesk.autotrading import AutoTrading,entry_signal,IST
from marketdesk.paper import init_paper,trade,portfolios
from marketdesk.storage import init,connect,set_state,get_state
from support import seed_quality,fixture_book

class AutoTradingTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite3';init(self.path);init_paper(self.path)
  self.clock=datetime(2026,10,12,10,0,tzinfo=IST).astimezone(timezone.utc)
  self.engine=AutoTrading(self.path)
  self.signal={'as_of':'2026-10-09T00:00:00+05:30','close':100,'dma50':99,'dma200':90,'high20':101,'volume_ratio':1.5,'turnover20':20_000_000,'fundamentals':{'status':'COMPLETE','latest':{'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','profit_before_tax':100,'finance_costs':10,'equity':1000,'total_borrowings':100,'total_lease_liabilities':10,'cash_and_cash_equivalents':50},'summary':{'revenue_period':'Mar 2026','net_profit_period':'Mar 2026','statement_type':'consolidated','units':'crore','frequency':'yearly','revenue_growth_yoy':10,'net_profit_growth_yoy':20}}}
  self.signals={}
  self.add_stock('TEST')
  self.depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.depth.start();self.addCleanup(self.depth.stop)
  with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
 def tearDown(self):self.temp.cleanup()
 def add_stock(self,symbol,price=102):
  self.signals[symbol]=copy.deepcopy(self.signal)
  with connect(self.path) as db:
   db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,'NSE_EQ|'+symbol,symbol,symbol,'Test'))
   db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,price,100,int(self.clock.timestamp()*1000),self.clock.isoformat(),'test'))
  seed_quality(self.path,symbol,self.clock,self.signal['fundamentals'])
 def set_price(self,symbol,price):
  with connect(self.path) as db:db.execute('UPDATE quotes SET price=?,trade_time=?,received_at=? WHERE symbol=?',(price,int(self.clock.timestamp()*1000),self.clock.isoformat(),symbol))
  seed_quality(self.path,symbol,self.clock,self.signal['fundamentals'])
 def run_cycle(self):
  for symbol in self.signals:seed_quality(self.path,symbol,self.clock,self.signals[symbol]['fundamentals'])
  self.engine.cycle(self.clock,self.signals)
 def trades(self,p='trader'):
  with connect(self.path) as db:return [dict(r) for r in db.execute('SELECT * FROM trades WHERE portfolio=? ORDER BY rowid',(p,))]
 def enable(self,p='trader'):self.engine.configure(p,True)
 def increase_investor_capacity(self):
  self.enable('investor')
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor'].update(max_positions=10,max_buys_per_day=5);set_state(db,'auto_config',cfg)
 def investor_order(self,symbol='TEST',qty=1):
  import uuid
  return trade(self.path,dict(portfolio='investor',symbol=symbol,side='BUY',quantity=qty,request_id=str(uuid.uuid4())),self.clock,auto_guard=self.engine.guard('investor',self.signals[symbol],self.clock))
 def test_expanded_investor_scan_buys_five_and_blocks_sixth(self):
  self.increase_investor_capacity()
  for n in range(6):self.add_stock('EXTRA'+str(n))
  self.run_cycle();self.run_cycle()
  self.assertEqual(len(self.trades('investor')),5)
  status=self.engine.snapshot()['status']['investor']
  self.assertEqual(status['max_positions'],10);self.assertEqual(status['max_buys_per_day'],5)
  self.assertEqual(status['buy_count'],5);self.assertIn('(5/5)',status['message'])
  with self.assertRaisesRegex(ValueError,'Daily new-position'):self.investor_order()
 def test_expanded_investor_tenth_holding_allowed_eleventh_blocked_stop_kept(self):
  self.increase_investor_capacity()
  for n in range(9):
   symbol='HELD'+str(n);self.add_stock(symbol)
   with connect(self.path) as db:db.execute("INSERT INTO positions VALUES('investor',?,1,102)",(symbol,))
  self.run_cycle();self.assertEqual(len(self.trades('investor')),1)
  with connect(self.path) as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM positions WHERE portfolio='investor' AND quantity>0").fetchone()[0],10)
  self.add_stock('ELEVENTH')
  with self.assertRaisesRegex(ValueError,'Position limit'):self.investor_order('ELEVENTH')
  self.set_price('TEST',90);self.run_cycle()
  sells=[t for t in self.trades('investor') if t['side']=='SELL']
  self.assertEqual(len(sells),1);self.assertIn('10% loss threshold',sells[0]['reason'])
 def test_expanded_investor_capacity_persists_other_rules_and_trader_unchanged(self):
  before=self.engine.snapshot();self.increase_investor_capacity()
  self.engine=AutoTrading(self.path);self.engine.configure('investor',False);self.enable('investor')
  after=self.engine.snapshot()
  self.assertEqual(after['config']['investor']['max_positions'],10)
  self.assertEqual(after['config']['investor']['max_buys_per_day'],5)
  self.assertEqual(after['rules']['investor'],{**before['rules']['investor'],'max_positions':10,'max_buys_per_day':5})
  self.assertEqual(after['config']['trader'],before['config']['trader'])
  self.assertEqual(after['rules']['trader'],before['rules']['trader'])
 def test_expanded_investor_limits_keep_budget_loss_halt_and_fundamental_checks(self):
  self.increase_investor_capacity()
  with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=1000)
  self.signals['TEST']['fundamentals']['status']='PARTIAL';self.run_cycle()
  self.assertEqual(self.trades('investor'),[])
  self.signals['TEST']=copy.deepcopy(self.signal)
  with connect(self.path) as db:db.execute("UPDATE portfolios SET cash=cash-25000 WHERE name='investor'")
  self.run_cycle();self.assertEqual(self.trades('investor'),[])
  with connect(self.path) as db:self.assertEqual(db.execute("SELECT halted FROM auto_days WHERE portfolio='investor'").fetchone()[0],1)
 def test_expanded_investor_stale_held_prices_still_block(self):
  self.increase_investor_capacity();self.add_stock('HELD')
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('investor','HELD',1,102)")
   db.execute("UPDATE quotes SET trade_time=trade_time-60000 WHERE symbol='HELD'")
  self.run_cycle();self.assertEqual(self.trades('investor'),[])
  self.assertIn('HELD',self.engine.snapshot()['status']['investor']['message'])
 def test_investor_capacity_rechecked_after_depth_fetch(self):
  self.increase_investor_capacity();self.investor_order();self.add_stock('SECOND')
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:
    cfg=get_state(db,'auto_config');cfg['investor']['max_buys_per_day']=1;set_state(db,'auto_config',cfg)
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'Daily new-position'):self.investor_order('SECOND')
  self.assertEqual(len(self.trades('investor')),1)
 def enable_investor_stale_valuation(self):
  self.increase_investor_capacity();self.add_stock('JNKINDIA')
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['allow_stale_held_valuation']=True;set_state(db,'auto_config',cfg)
   db.execute("INSERT INTO positions VALUES('investor','JNKINDIA',1,102)")
   db.execute("UPDATE quotes SET trade_time=trade_time-95000 WHERE symbol='JNKINDIA'")
 def test_investor_stale_valuation_allows_other_buy_and_records_mark(self):
  self.enable_investor_stale_valuation();before=self.engine.snapshot()
  self.engine=AutoTrading(self.path);self.run_cycle()
  rows=self.trades('investor');self.assertEqual([r['symbol'] for r in rows],['TEST'])
  self.assertIn('stale holding valuation: JNKINDIA at 102',rows[0]['reason'])
  after=self.engine.snapshot();status=after['status']['investor']
  self.assertEqual(status['state'],'monitoring')
  self.assertEqual([m['symbol'] for m in status['valuation_only_holdings']],['JNKINDIA'])
  self.assertTrue(status['allow_stale_held_valuation'])
  self.assertEqual(before['config']['trader'],after['config']['trader'])
  self.assertEqual(before['rules']['trader'],after['rules']['trader'])
 def test_investor_stale_valuation_rejects_unusable_held_marks(self):
  from marketdesk.autotrading import investor_valuation
  self.enable_investor_stale_valuation()
  stamp=int(self.clock.timestamp()*1000)
  cases=[(102,stamp-86400000,self.clock.isoformat()),(102,stamp-95000,(self.clock-timedelta(days=1)).isoformat()),
   (102,stamp+1000,self.clock.isoformat()),(102,stamp-95000,(self.clock+timedelta(seconds=1)).isoformat()),
   (102,stamp-95000,'bad-date'),(102,stamp-95000,self.clock.replace(tzinfo=None).isoformat()),
   (0,stamp,self.clock.isoformat()),(-1,stamp,self.clock.isoformat()),(float('inf'),stamp,self.clock.isoformat())]
  with connect(self.path) as db:
   for price,traded,received in cases:
    with self.subTest(price=price,traded=traded,received=received):
     db.execute("UPDATE quotes SET price=?,trade_time=?,received_at=? WHERE symbol='JNKINDIA'",(price,traded,received))
     self.assertIsNone(investor_valuation(db,self.clock)[0])
   db.execute("DELETE FROM quotes WHERE symbol='JNKINDIA'")
   self.assertIsNone(investor_valuation(db,self.clock)[0])
  with self.assertRaisesRegex(ValueError,'no fresh price'):self.investor_order()
 def test_investor_stale_valuation_keeps_daily_loss_halt_latched(self):
  from marketdesk.autotrading import daily_risk
  self.enable_investor_stale_valuation()
  with connect(self.path) as db:
   self.assertTrue(daily_risk(db,'investor',self.clock)[0])
   db.execute("UPDATE portfolios SET cash=cash-25000 WHERE name='investor'")
   self.assertFalse(daily_risk(db,'investor',self.clock)[0])
   db.execute("UPDATE portfolios SET cash=cash+25000 WHERE name='investor'")
  self.engine=AutoTrading(self.path)
  with self.assertRaisesRegex(ValueError,'Daily loss limit'):self.investor_order()
  self.run_cycle();self.assertEqual(self.trades('investor'),[])
 def test_investor_stale_valuation_does_not_allow_stale_buy_or_stop_fill(self):
  self.enable_investor_stale_valuation()
  with connect(self.path) as db:
   db.execute("UPDATE quotes SET trade_time=trade_time-95000 WHERE symbol='TEST'")
   db.execute("UPDATE quotes SET price=90 WHERE symbol='JNKINDIA'")
  self.run_cycle();self.assertEqual(self.trades('investor'),[])
  with self.assertRaises(ValueError):self.investor_order()
  self.set_price('JNKINDIA',90);self.run_cycle()
  rows=self.trades('investor');self.assertEqual([(r['symbol'],r['side']) for r in rows],[('JNKINDIA','SELL')])
  self.assertIn('10% loss threshold',rows[0]['reason'])
 def test_investor_stale_valuation_rechecked_at_fill(self):
  self.enable_investor_stale_valuation()
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:db.execute("UPDATE quotes SET trade_time=trade_time-86400000 WHERE symbol='JNKINDIA'")
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'no fresh price'):self.investor_order()
  self.assertEqual(self.trades('investor'),[])
 def test_investor_stale_valuation_keeps_caps_and_fundamentals(self):
  self.enable_investor_stale_valuation()
  with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=1000)
  self.signals['TEST']['fundamentals']['status']='PARTIAL';self.run_cycle()
  self.assertEqual(self.trades('investor'),[])
  self.signals['TEST']=copy.deepcopy(self.signal)
  for n in range(6):self.add_stock('NEW'+str(n))
  self.run_cycle();self.run_cycle();self.assertEqual(len(self.trades('investor')),5)
  with self.assertRaisesRegex(ValueError,'Daily new-position'):self.investor_order()
 def test_investor_stale_valuation_can_be_disabled_at_fill(self):
  self.enable_investor_stale_valuation()
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:
    cfg=get_state(db,'auto_config');cfg['investor']['allow_stale_held_valuation']=False;set_state(db,'auto_config',cfg)
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'no fresh price'):self.investor_order()
  self.assertEqual(self.trades('investor'),[])
 def test_both_portfolios_buy_separately_and_repeated_cycles_do_not_duplicate(self):
  self.enable();self.enable('investor');self.run_cycle();self.run_cycle()
  for p in ['trader','investor']:
   rows=self.trades(p);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source'],'automatic');self.assertTrue(rows[0]['reason'])
   self.assertLessEqual(rows[0]['quantity']*rows[0]['price']+rows[0]['fee'],100000)
  with connect(self.path) as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_events WHERE kind='fill'").fetchone()[0],2)
 def test_stop_exit_and_no_same_day_reentry_after_restart(self):
  self.enable();self.run_cycle();self.set_price('TEST',98);self.run_cycle()
  self.assertEqual([r['side'] for r in self.trades()],['BUY','SELL'])
  self.assertIn('loss threshold',self.trades()[-1]['reason'])
  self.engine=AutoTrading(self.path);self.set_price('TEST',103);self.run_cycle()
  self.assertEqual(len(self.trades()),2)
 def test_profit_target_and_investor_trend_exit(self):
  self.enable();self.enable('investor');self.run_cycle();self.set_price('TEST',110);self.run_cycle()
  self.assertEqual(self.trades()[-1]['side'],'SELL');self.assertIn('profit target',self.trades()[-1]['reason'])
  self.signals['TEST']['dma200']=105;self.set_price('TEST',104);self.run_cycle()
  self.assertEqual(self.trades('investor')[-1]['side'],'SELL');self.assertIn('200-day',self.trades('investor')[-1]['reason'])
 def test_incomplete_conflicting_or_mismatched_fundamentals_block_investor(self):
  self.enable('investor')
  cases=[]
  for edit in ['incomplete','conflict','period','units','basis','old','finance']:
   s=copy.deepcopy(self.signal);f=s['fundamentals']
   if edit=='incomplete':f['status']='PARTIAL'
   if edit=='conflict':f['source_conflicts']=[{'field':'profit_before_tax'}]
   if edit=='period':f['summary']['net_profit_period']='Mar 2025'
   if edit=='units':f['summary']['units']='lakh'
   if edit=='basis':f['summary']['statement_type']='standalone'
   if edit=='old':f['latest']['period']='Mar 2020'
   if edit=='finance':f['latest']['finance_costs']=None
   self.assertIsNone(entry_signal('investor',s,102,self.clock),edit)
  self.assertIsNotNone(entry_signal('investor',self.signal,102,self.clock))
 def test_closed_disconnected_stale_or_future_quote_no_fills(self):
  self.enable()
  for status,market in [('connected','CLOSING_END'),('disconnected','NORMAL_OPEN')]:
   with connect(self.path) as db:set_state(db,'stream',{'status':status,'market_status':{'NSE_EQ':market}})
   self.run_cycle();self.assertEqual(self.trades(),[])
  with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
  for offset in [-31,31]:
   with connect(self.path) as db:db.execute('UPDATE quotes SET trade_time=?',(int((self.clock.timestamp()+offset)*1000),))
   self.run_cycle();self.assertEqual(self.trades(),[])
 def test_daily_entry_limit(self):
  for n in range(5):self.add_stock('TEST'+str(n))
  self.enable();self.enable('investor');self.run_cycle();self.run_cycle()
  self.assertEqual(len(self.trades()),3);self.assertEqual(len(self.trades('investor')),2)
 def test_pause_is_rechecked_inside_order_transaction(self):
  self.enable();self.engine.configure('trader',False)
  with self.assertRaisesRegex(ValueError,'paused'):
   trade(self.path,{'portfolio':'trader','symbol':'TEST','side':'BUY','quantity':1,'request_id':'automatic-pause-test'},self.clock,auto_guard=self.engine.guard('trader',self.signal,self.clock))
  self.assertEqual(self.trades(),[])
 def test_manual_orders_require_pausing_enabled_portfolio(self):
  self.enable()
  with self.assertRaisesRegex(ValueError,'Pause automatic'):
   trade(self.path,{'portfolio':'trader','symbol':'TEST','side':'BUY','quantity':1,'request_id':'manual-order-test'},self.clock)
 def test_daily_loss_halt_persists_after_recovery_and_restart(self):
  self.add_stock('OTHER');self.enable();self.run_cycle()
  with connect(self.path) as db:db.execute("UPDATE portfolios SET cash=cash-25000 WHERE name='trader'")
  self.add_stock('NEW');self.run_cycle()
  with connect(self.path) as db:
   self.assertEqual(db.execute("SELECT halted FROM auto_days WHERE portfolio='trader'").fetchone()[0],1)
   db.execute("UPDATE portfolios SET cash=cash+25000 WHERE name='trader'")
  self.engine=AutoTrading(self.path);self.run_cycle()
  self.assertNotIn('NEW',[t['symbol'] for t in self.trades()])
 def test_position_count_limit_and_cash_cap(self):
  self.enable()
  for n in range(5):
   symbol='HELD'+str(n);self.add_stock(symbol)
   with connect(self.path) as db:db.execute('INSERT INTO positions VALUES(?,?,?,?)',('trader',symbol,1,102))
  self.run_cycle();self.assertEqual(self.trades(),[])
 def test_old_history_low_liquidity_and_large_gap_skip(self):
  for field,value in [('as_of','2026-09-01T00:00:00+05:30'),('turnover20',100),('close',10)]:
   s=copy.deepcopy(self.signal);s[field]=value;self.assertIsNone(entry_signal('trader',s,102,self.clock))
 def test_outside_entry_window_still_processes_exits(self):
  self.enable();self.run_cycle();self.clock=self.clock.replace(hour=10,minute=0) # 15:30 IST
  self.set_price('TEST',98);self.run_cycle();self.assertEqual(self.trades()[-1]['side'],'SELL')
  self.add_stock('NEW');self.run_cycle();self.assertNotIn('NEW',[t['symbol'] for t in self.trades()])

 def test_new_session_waits_for_previous_session_candles(self):
  self.enable();self.run_cycle()
  self.clock+=timedelta(days=1);self.set_price('TEST',102);self.add_stock('NEW');self.run_cycle()
  self.assertNotIn('NEW',[t['symbol'] for t in self.trades()])
  self.signals['NEW']['as_of']='2026-10-12T00:00:00+05:30';self.run_cycle()
  self.assertIn('NEW',[t['symbol'] for t in self.trades()])
 def test_daily_refresh_waits_for_other_job_and_runs_once(self):
  self.enable();self.run_cycle();refresh=Mock();self.engine.refresh=refresh
  self.clock+=timedelta(days=1)
  with connect(self.path) as db:set_state(db,'refresh',{'status':'running'})
  self.engine.refresh_daily_candles(self.clock);refresh.start.assert_not_called()
  with connect(self.path) as db:set_state(db,'refresh',{'status':'complete'})
  self.engine.refresh_daily_candles(self.clock);self.engine.refresh_daily_candles(self.clock)
  refresh.start.assert_called_once_with([],'candles')
 def test_stale_holdings_block_new_buys_but_fresh_stop_exit_runs(self):
  self.enable();self.run_cycle();self.add_stock('STALE');self.add_stock('NEW')
  with connect(self.path) as db:
   db.execute('INSERT INTO positions VALUES(?,?,?,?)',('trader','STALE',1,102))
   db.execute("UPDATE quotes SET trade_time=trade_time-60000 WHERE symbol='STALE'")
  self.set_price('TEST',98);self.run_cycle()
  self.assertEqual(self.trades()[-1]['side'],'SELL');self.assertNotIn('NEW',[t['symbol'] for t in self.trades()])

 def test_delayed_scan_rechecks_real_execution_time(self):
  self.enable()
  with patch('marketdesk.autotrading.datetime') as clock, patch('marketdesk.paper.datetime') as paper_clock:
   clock.now.return_value=self.clock+timedelta(seconds=31)
   paper_clock.now.return_value=self.clock+timedelta(seconds=31)
   self.assertEqual(self.engine.execute('trader','TEST','BUY',1,self.signal,self.clock,realtime=True),0)
  self.assertEqual(self.trades(),[])

 def test_unexpected_engine_error_pauses_enabled_portfolios(self):
  self.enable();self.enable('investor');self.engine.cycle=Mock(side_effect=RuntimeError('test failure'))
  self.engine.stop_event.wait=lambda seconds:self.engine.stop_event.set()
  self.engine.run()
  snapshot=self.engine.snapshot()
  self.assertTrue(all(not c['enabled'] for c in snapshot['config'].values()))
  self.assertEqual(sum(e['kind']=='error' for e in snapshot['events']),2)
 def enable_full_session(self,p='investor'):
  self.enable(p)
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg[p]['full_session_buying']=True;set_state(db,'auto_config',cfg)
 def test_investor_full_session_open_late_buys_limits_and_stop(self):
  self.enable_full_session()
  self.clock=datetime(2026,10,12,9,15,tzinfo=IST);self.set_price('TEST',102);self.run_cycle()
  self.assertEqual(len(self.trades('investor')),1)
  self.clock=self.clock.replace(hour=15,minute=29);self.set_price('TEST',102);self.add_stock('LATE');self.run_cycle()
  self.assertEqual(len(self.trades('investor')),2)
  self.add_stock('THIRD');self.run_cycle();self.assertEqual(len(self.trades('investor')),2)
  self.set_price('TEST',90);self.run_cycle()
  self.assertEqual(self.trades('investor')[-1]['side'],'SELL')
  self.assertIn('10% loss threshold',self.trades('investor')[-1]['reason'])
  self.set_price('TEST',102);self.run_cycle();self.assertEqual(len(self.trades('investor')),3)
 def test_investor_full_session_hours_and_execution_time_recheck(self):
  self.enable_full_session()
  for hour,minute in ((9,14),(15,30)):
   self.clock=datetime(2026,10,12,hour,minute,tzinfo=IST);self.set_price('TEST',102);self.run_cycle()
   self.assertEqual(self.trades('investor'),[])
   with self.assertRaisesRegex(ValueError,'09:15–15:30'):
    trade(self.path,dict(portfolio='investor',symbol='TEST',side='BUY',quantity=1,request_id='outside-session-order'),self.clock,auto_guard=self.engine.guard('investor',self.signal,self.clock))
  with connect(self.path) as db:
   with self.assertRaisesRegex(ValueError,'09:15–15:30'):
    self.engine.guard('investor',self.signal,self.clock.replace(minute=29,second=59))(db,{'side':'BUY'},{'price':102,'execution_time':self.clock.isoformat()},None,None)
 def test_investor_full_session_keeps_fundamentals_daily_loss_and_feed_checks(self):
  self.enable_full_session();self.clock=datetime(2026,10,12,15,15,tzinfo=IST);self.set_price('TEST',102)
  for state,market in (('connected','PRE_OPEN'),('connected','CLOSED'),('disconnected','NORMAL_OPEN')):
   with connect(self.path) as db:set_state(db,'stream',{'status':state,'market_status':{'NSE_EQ':market}})
   self.run_cycle();self.assertEqual(self.trades('investor'),[])
  with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
  self.signals['TEST']['fundamentals']['status']='PARTIAL';self.run_cycle();self.assertEqual(self.trades('investor'),[])
  self.signals['TEST']=copy.deepcopy(self.signal)
  with connect(self.path) as db:
   db.execute("UPDATE auto_days SET halted=1 WHERE portfolio='investor'")
  self.run_cycle();self.assertEqual(self.trades('investor'),[])
 def test_investor_full_session_persists_and_rules_keep_stops(self):
  before=self.engine.snapshot()['rules']['investor'];self.enable_full_session()
  self.engine=AutoTrading(self.path);self.engine.configure('investor',False);self.enable('investor')
  snap=self.engine.snapshot();self.assertTrue(snap['config']['investor']['full_session_buying'])
  self.assertIn('09:15–15:30',snap['rules']['investor']['entry_window'])
  for key,value in before.items():self.assertEqual(snap['rules']['investor'][key],value)
 def test_live_scan_uses_current_time_after_slow_quote_recovery(self):
  self.enable_full_session()
  with connect(self.path) as db:db.execute("INSERT INTO positions VALUES('investor','TEST',1,100)")
  self.add_stock('NEW')
  with patch('marketdesk.autotrading.datetime',wraps=datetime) as current,patch('marketdesk.autotrading.refresh_held_quotes') as refresh,patch.object(self.engine,'execute',return_value=1):
   current.now.return_value=self.clock
   result=self.engine.scan('investor',self.signals,self.clock-timedelta(seconds=60),realtime=True)
  refresh.assert_called_once_with(self.path,'investor')
  self.assertEqual(result['state'],'monitoring');self.assertEqual(result['fills_this_scan'],1)
 def test_stale_status_names_the_held_stock(self):
  self.enable('investor')
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('investor','TEST',1,100)")
   db.execute('UPDATE quotes SET trade_time=trade_time-60000')
  self.run_cycle();status=self.engine.snapshot()['status']['investor']
  self.assertEqual(status['state'],'risk_wait');self.assertIn('TEST (last trade 60s old)',status['message'])
  self.assertEqual(status['stale_holdings'][0]['symbol'],'TEST')

if __name__=='__main__':unittest.main()
