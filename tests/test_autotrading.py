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
 def test_repeated_blocked_scan_events_are_logged_once_per_session(self):
  from marketdesk.autotrading import event
  later=self.clock+timedelta(seconds=15);next_day=self.clock+timedelta(days=1)
  with connect(self.path) as db:
   for clock in (self.clock,later):event(db,'investor','blocked','No whole share fits the current risk or position budget.','TEST',clock=clock)
   event(db,'investor','blocked','Position limit reached.','TEST',clock=later)
   event(db,'investor','blocked','No whole share fits the current risk or position budget.','TEST',clock=next_day)
   rows=db.execute("SELECT reason,COUNT(*) FROM auto_events WHERE kind='blocked' GROUP BY reason ORDER BY reason").fetchall()
  self.assertEqual([tuple(r) for r in rows],[('No whole share fits the current risk or position budget.',2),('Position limit reached.',1)])
 def test_activity_feed_collapses_legacy_repeated_blocks(self):
  from marketdesk.autotrading import event_snapshot
  with connect(self.path) as db:
   for n in range(40):db.execute("INSERT INTO auto_events(created_at,portfolio,kind,symbol,reason) VALUES(?,?,?,?,?)",((self.clock+timedelta(seconds=15*n)).isoformat(),'investor','blocked','TEST','No whole share fits.'))
   db.execute("INSERT INTO auto_events(created_at,portfolio,kind,symbol,reason) VALUES(?,?,?,?,?)",((self.clock+timedelta(hours=1)).isoformat(),'investor','fill','TEST','BUY 1'))
   db.execute("INSERT INTO auto_events(created_at,portfolio,kind,symbol,reason) VALUES(?,?,?,?,?)",(self.clock.isoformat(),'investor','blocked','OTHER','No whole share fits.'))
   rows=event_snapshot(db)
  self.assertEqual([r['kind'] for r in rows],['blocked','fill'])
  self.assertEqual(rows[0]['symbols'],['OTHER','TEST'])
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
 def test_investor_latest_financial_thresholds_override_cached_signal(self):
  self.enable('investor')
  for field,value,section in [('revenue_growth_yoy',-1,'summary'),('net_profit_growth_yoy',0,'summary'),
    ('profit_before_tax',-1,'latest'),('finance_costs',100,'latest'),('total_borrowings',1000,'latest')]:
   with self.subTest(field=field):
    f=copy.deepcopy(self.signal['fundamentals']);f[section][field]=value
    with connect(self.path) as db:db.execute("UPDATE fundamentals SET details=? WHERE symbol='TEST'",(json.dumps(f),))
    with self.assertRaisesRegex(ValueError,'Entry conditions changed'):self.investor_order()
  self.assertEqual(self.trades('investor'),[])
 def test_investor_financial_thresholds_rechecked_after_depth(self):
  self.enable('investor')
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:
    f=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()[0])
    f['summary']['net_profit_growth_yoy']=-20
    db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(f),symbol))
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'Entry conditions changed'):self.investor_order()
  self.assertEqual(self.trades('investor'),[])
 def test_investor_valid_financial_refresh_keeps_entry_and_stops(self):
  self.enable('investor')
  with connect(self.path) as db:
   f=copy.deepcopy(self.signal['fundamentals']);f['summary']['net_profit_growth_yoy']=25
   db.execute("UPDATE fundamentals SET details=? WHERE symbol='TEST'",(json.dumps(f),))
  self.investor_order();self.set_price('TEST',90);self.run_cycle()
  self.assertEqual([r['side'] for r in self.trades('investor')],['BUY','SELL'])
  self.assertIn('10% loss threshold',self.trades('investor')[-1]['reason'])
 def enable_investor_fee_cap(self):
  self.increase_investor_capacity()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['max_round_trip_fee_pct']=.5;set_state(db,'auto_config',cfg)
 def test_investor_fee_cap_rejects_tiny_buy_without_ledger_changes(self):
  self.enable_investor_fee_cap()
  with self.assertRaisesRegex(ValueError,'Buy too small'):self.investor_order(qty=10)
  with connect(self.path) as db:
   self.assertEqual(db.execute("SELECT cash FROM portfolios WHERE name='investor'").fetchone()[0],1000000)
   for table in ('trades','positions','depth_used'):self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
  result=self.investor_order(qty=300);check=json.loads(result['execution'])['buy_cost_check']
  self.assertEqual(result['quantity'],300);self.assertLessEqual(check['estimated_round_trip_fee_pct'],.5)
 def test_investor_fee_cap_checks_actual_partial_fill(self):
  self.enable_investor_fee_cap()
  def shallow(path,symbol):
   book=fixture_book(path,symbol);book['depth']['sell'][0]['quantity']=100
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=shallow):
   with self.assertRaisesRegex(ValueError,'Buy too small'):self.investor_order(qty=900)
  self.assertEqual(self.trades('investor'),[])
  self.assertEqual(self.investor_order(qty=300)['quantity'],300)
 def test_investor_fee_cap_is_reloaded_after_depth(self):
  self.increase_investor_capacity()
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:
    cfg=get_state(db,'auto_config');cfg['investor']['max_round_trip_fee_pct']=.5;set_state(db,'auto_config',cfg)
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'Buy too small'):self.investor_order(qty=10)
 def test_investor_fee_cap_keeps_tiny_stop_exit_and_no_reentry(self):
  self.enable_investor_fee_cap()
  with connect(self.path) as db:db.execute("INSERT INTO positions VALUES('investor','TEST',1,102)")
  self.set_price('TEST',90);self.run_cycle()
  self.assertEqual([(r['side'],r['quantity']) for r in self.trades('investor')],[('SELL',1)])
  self.set_price('TEST',102)
  with self.assertRaisesRegex(ValueError,'same-session re-entry'):self.investor_order(qty=300)
 def test_investor_scan_skips_uneconomic_stock_and_buys_next(self):
  self.enable_investor_fee_cap();self.add_stock('ZZZ')
  def depth(path,symbol):
   book=fixture_book(path,symbol)
   if symbol=='TEST':book['depth']['sell'][0]['quantity']=100
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=depth):self.run_cycle()
  self.assertEqual([r['symbol'] for r in self.trades('investor')],['ZZZ'])
  with connect(self.path) as db:
   self.assertEqual(db.execute("SELECT COUNT(*) FROM auto_events WHERE kind='blocked' AND reason LIKE 'Buy too small%'").fetchone()[0],1)
 def test_investor_fee_cap_keeps_budget_and_daily_loss_checks(self):
  self.enable_investor_fee_cap()
  with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=1000)
  with connect(self.path) as db:
   db.execute("INSERT INTO auto_days VALUES('investor',?,1000000,1)",(self.clock.astimezone(IST).date().isoformat(),))
  with self.assertRaisesRegex(ValueError,'Daily loss'):self.investor_order(qty=300)
 def test_fee_cap_persists_and_is_visible_in_rules(self):
  self.enable_investor_fee_cap();self.engine=AutoTrading(self.path)
  rules=self.engine.snapshot()['rules']['investor']
  self.assertEqual(rules['max_round_trip_fee_pct'],.5)
  self.assertIn('actual filled value',rules['risk']);self.assertEqual(rules['stop_pct'],10)
 def enable_investor_portfolio_cap(self):
  self.increase_investor_capacity()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['portfolio_risk_cap_pct']=3;set_state(db,'auto_config',cfg)
 def set_investor_buy_target(self):
  self.enable_investor_portfolio_cap()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor'].update(buy_target_inr=3500,max_round_trip_fee_pct=2.5);set_state(db,'auto_config',cfg)
 def test_investor_target_sizes_future_buy_preserves_existing_holding_and_exit(self):
  self.set_investor_buy_target();self.add_stock('HELD')
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('investor','HELD',100,10200)")
   db.execute("UPDATE portfolios SET cash=cash-10200 WHERE name='investor'")
  self.run_cycle()
  buys=self.trades('investor');self.assertEqual(len(buys),1)
  cost=buys[0]['quantity']*buys[0]['price']+buys[0]['fee']
  self.assertGreater(cost,3200);self.assertLessEqual(cost,3500)
  with connect(self.path) as db:
   held=db.execute("SELECT quantity,cost FROM positions WHERE portfolio='investor' AND symbol='HELD'").fetchone()
   self.assertEqual(tuple(held),(100,10200))
  self.engine=AutoTrading(self.path)
  rules=self.engine.snapshot()['rules']['investor']
  self.assertEqual(rules['buy_target_inr'],3500);self.assertEqual(rules['position_pct'],10)
  self.assertEqual(rules['stop_pct'],10);self.assertEqual(rules['portfolio_risk_cap_pct'],3)
  self.set_price('HELD',90);self.run_cycle()
  sells=[r for r in self.trades('investor') if r['side']=='SELL']
  self.assertEqual([(r['symbol'],r['quantity']) for r in sells],[('HELD',100)])
 def test_investor_target_rechecked_at_fill_keeps_fee_check(self):
  self.set_investor_buy_target()
  with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=40)
  with self.assertRaisesRegex(ValueError,'Buy too small'):self.investor_order(qty=1)
  self.assertEqual(self.trades('investor'),[])
  # A target changed while depth is fetched must be re-read before committing.
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['buy_target_inr']=5000;set_state(db,'auto_config',cfg)
  def changed_target(path,symbol):
   with connect(path) as db:
    cfg=get_state(db,'auto_config');cfg['investor']['buy_target_inr']=3500;set_state(db,'auto_config',cfg)
   return fixture_book(path,symbol)
  with patch('marketdesk.paper.fetch_book',side_effect=changed_target):
   with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=40)
 def test_investor_target_does_not_override_portfolio_risk_or_trader_budget(self):
  from marketdesk.autotrading import buy_budget,RULES
  self.seed_investor_over_cap();self.set_investor_buy_target();self.run_cycle()
  self.assertEqual(self.trades('investor'),[])
  self.assertEqual(self.engine.snapshot()['status']['investor']['state'],'risk_wait')
  account={'cash':100000,'initial_cash':100000}
  self.assertEqual(buy_budget('trader',account,RULES['trader'],{'buy_target_inr':3500}),10000)
  self.assertEqual(buy_budget('investor',{'cash':2000,'initial_cash':100000},RULES['investor'],{'buy_target_inr':3500}),2000)
 def seed_investor_over_cap(self):
  self.enable_investor_portfolio_cap();self.add_stock('HELD')
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('investor','HELD',3000,306000)")
   db.execute("UPDATE portfolios SET cash=694000 WHERE name='investor'")
 def test_investor_cap_blocks_new_buys_preserves_holdings_and_stop_exit(self):
  self.seed_investor_over_cap();self.run_cycle()
  self.assertEqual(self.trades('investor'),[])
  status=self.engine.snapshot()['status']['investor']
  self.assertEqual(status['state'],'risk_wait');self.assertGreater(status['portfolio_risk']['risk_pct'],3)
  with self.assertRaisesRegex(ValueError,'portfolio risk cap'):self.investor_order(qty=300)
  self.set_price('HELD',90);self.run_cycle()
  sells=[r for r in self.trades('investor') if r['side']=='SELL']
  self.assertEqual([(r['symbol'],r['quantity']) for r in sells],[('HELD',3000)])
  self.assertIn('10% loss threshold',sells[0]['reason'])
 def test_investor_cap_keeps_trend_exit_above_stop(self):
  self.seed_investor_over_cap();self.signals['HELD']['dma200']=103;self.run_cycle()
  sells=[r for r in self.trades('investor') if r['side']=='SELL']
  self.assertEqual([r['symbol'] for r in sells],['HELD']);self.assertIn('200-day average',sells[0]['reason'])
 def test_investor_cap_sizes_down_and_preserves_fee_filter(self):
  from marketdesk.autotrading import investor_portfolio_risk
  self.enable_investor_portfolio_cap()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['max_round_trip_fee_pct']=.5;set_state(db,'auto_config',cfg)
  for i in range(5):self.add_stock('NEW'+str(i))
  self.run_cycle();self.run_cycle()
  buys=self.trades('investor');self.assertEqual(len(buys),3)
  self.assertLess(buys[2]['quantity'],buys[0]['quantity'])
  for buy in buys:self.assertLessEqual(json.loads(buy['execution'])['buy_cost_check']['estimated_round_trip_fee_pct'],.5)
  with connect(self.path) as db:
   risk=investor_portfolio_risk(db,self.clock)
   self.assertLessEqual(risk['risk'],risk['limit']);self.assertGreater(risk['risk_pct'],2.95)
 def test_investor_cap_rechecked_after_depth_fetch(self):
  self.enable_investor_portfolio_cap();self.add_stock('HELD')
  def changed(path,symbol):
   book=fixture_book(path,symbol)
   with connect(path) as db:
    db.execute("INSERT INTO positions VALUES('investor','HELD',3000,306000)")
    db.execute("UPDATE portfolios SET cash=694000 WHERE name='investor'")
   return book
  with patch('marketdesk.paper.fetch_book',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'portfolio risk cap'):self.investor_order(qty=300)
  with connect(self.path) as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],0)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM depth_used').fetchone()[0],0)
 def test_investor_cap_uses_partial_fill_and_actual_fees(self):
  from marketdesk.autotrading import investor_portfolio_risk,investor_stop_risk
  self.enable_investor_portfolio_cap();self.add_stock('HELD')
  with connect(self.path) as db:
   db.execute("INSERT INTO positions VALUES('investor','HELD',2400,244800)")
   db.execute("UPDATE portfolios SET cash=755200 WHERE name='investor'")
  with self.assertRaisesRegex(ValueError,'portfolio risk cap'):self.investor_order(qty=900)
  def partial(path,symbol):
   book=fixture_book(path,symbol);book['depth']['sell'][0]['quantity']=3000;return book
  with patch('marketdesk.paper.fetch_book',side_effect=partial):filled=self.investor_order(qty=900)
  self.assertEqual(filled['quantity'],300)
  with connect(self.path) as db:
   risk=investor_portfolio_risk(db,self.clock)
   self.assertLessEqual(risk['risk'],risk['limit'])
   cost=db.execute("SELECT cost FROM positions WHERE portfolio='investor' AND symbol='TEST'").fetchone()[0]
   self.assertAlmostEqual(risk['risk'],investor_stop_risk(244800)+investor_stop_risk(cost))
 def test_investor_cap_persists_without_changing_trader_or_other_rules(self):
  before=self.engine.snapshot();self.enable_investor_portfolio_cap()
  self.engine=AutoTrading(self.path);self.engine.configure('investor',False);self.enable('investor')
  after=self.engine.snapshot();self.assertEqual(after['config']['investor']['portfolio_risk_cap_pct'],3)
  self.assertEqual(after['rules']['investor']['portfolio_risk_cap_pct'],3)
  self.assertEqual(after['rules']['investor']['stop_pct'],10);self.assertEqual(after['rules']['investor']['daily_loss_pct'],2)
  self.assertEqual(after['rules']['trader'],before['rules']['trader']);self.assertEqual(after['config']['trader'],before['config']['trader'])
 def test_investor_cap_keeps_budget_daily_halt_and_freshness_checks(self):
  self.enable_investor_portfolio_cap()
  with self.assertRaisesRegex(ValueError,'Position budget'):self.investor_order(qty=1000)
  with connect(self.path) as db:
   db.execute("INSERT INTO auto_days VALUES('investor',?,1000000,1)",(self.clock.astimezone(IST).date().isoformat(),))
  with self.assertRaisesRegex(ValueError,'Daily loss'):self.investor_order(qty=300)
  with connect(self.path) as db:db.execute('UPDATE quotes SET trade_time=trade_time-60000')
  with self.assertRaisesRegex(ValueError,'live trade price'):self.investor_order(qty=300)
 def test_investor_cost_risk_includes_fees_once_and_respects_boundary(self):
  from marketdesk.autotrading import investor_stop_risk,investor_buy_fits
  from marketdesk.execution import charges
  # 100000 is the entire remaining fee-inclusive cost basis.
  self.assertAlmostEqual(investor_stop_risk(100000),10000+charges(90000,'SELL',True)['total']+500)
  cost=10200+charges(10200,'BUY')['total'];added=investor_stop_risk(cost);after=1000000-charges(10200,'BUY')['total']
  risk={'equity':1000000,'risk':after*.03-added-.01,'cap_pct':3}
  self.assertTrue(investor_buy_fits(100,102,102,risk))
  risk['risk']+=.02;self.assertFalse(investor_buy_fits(100,102,102,risk))
 def test_investor_cap_uses_stale_valuation_policy_and_blocks_prior_session(self):
  from marketdesk.autotrading import investor_portfolio_risk
  self.enable_investor_stale_valuation()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['investor']['portfolio_risk_cap_pct']=3;set_state(db,'auto_config',cfg)
   self.assertIsNotNone(investor_portfolio_risk(db,self.clock))
   db.execute("UPDATE quotes SET trade_time=trade_time-86400000 WHERE symbol='JNKINDIA'")
   with self.assertRaisesRegex(ValueError,'no fresh price'):investor_portfolio_risk(db,self.clock)
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
 def test_investor_rank_prefers_growth_profitability_and_trend_among_qualifiers(self):
  base=entry_signal('investor',self.signal,102,self.clock)[1]
  weak_growth=copy.deepcopy(self.signal);weak_growth['fundamentals']['summary'].update(revenue_growth_yoy=5,net_profit_growth_yoy=5)
  weak_quality=copy.deepcopy(self.signal);weak_quality['fundamentals']['latest'].update(equity=weak_quality['fundamentals']['latest']['equity']*10)
  self.assertLess(entry_signal('investor',weak_growth,102,self.clock)[1],base)
  self.assertLess(entry_signal('investor',weak_quality,102,self.clock)[1],base)
  self.assertGreater(entry_signal('investor',self.signal,110,self.clock)[1],base)
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

 def test_exits_only_skips_buys_but_still_sells(self):
  self.enable()
  with connect(self.path) as db:
   cfg=get_state(db,'auto_config');cfg['trader'].update(exits_only=True,exits_only_reason='Backtest lost money.');set_state(db,'auto_config',cfg)
   db.execute('INSERT INTO positions VALUES(?,?,?,?)',('trader','TEST',1,102))
  self.add_stock('NEW');self.run_cycle();self.assertNotIn('NEW',[t['symbol'] for t in self.trades()])
  self.set_price('TEST',98);self.run_cycle();self.assertEqual(self.trades()[-1]['side'],'SELL')

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
 def test_daily_refresh_retries_only_stocks_still_behind(self):
  self.enable();self.run_cycle();refresh=Mock();self.engine.refresh=refresh;self.add_stock('LATE')
  self.clock+=timedelta(days=1)
  with connect(self.path) as db:
   for symbol in ('TEST','LATE'):db.execute('DELETE FROM candles WHERE symbol=?',(symbol,))
   db.execute("INSERT INTO candles VALUES('TEST','2026-10-12T00:00:00+05:30',1,1,1,1,1,'test','x')")
   db.execute("INSERT INTO candles VALUES('LATE','2026-10-09T00:00:00+05:30',1,1,1,1,1,'test','x')")
  self.engine.refresh_daily_candles(self.clock);refresh.start.assert_called_once_with([],'candles')
  self.engine.refresh_daily_candles(self.clock+timedelta(minutes=5));self.assertEqual(refresh.start.call_count,1)
  for attempt in range(1,5):self.engine.refresh_daily_candles(self.clock+timedelta(minutes=20*attempt))
  self.assertEqual(refresh.start.call_count,4)
  self.assertEqual(refresh.start.call_args.args,(['LATE'],'candles'))
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

 def run_checks(self,count):
  calls=[]
  def wait(seconds):
   calls.append(seconds)
   if len(calls)>=count:self.engine.stop_event.set()
  self.engine.stop_event.wait=wait;self.engine.run()
 def test_one_off_engine_error_retries_without_pausing(self):
  self.enable();self.enable('investor')
  self.engine.cycle=Mock(side_effect=[RuntimeError('blip'),RuntimeError('blip'),None,RuntimeError('blip'),RuntimeError('blip')])
  with patch('sys.stderr'):self.run_checks(5)
  snapshot=self.engine.snapshot()
  self.assertTrue(snapshot['config']['trader']['enabled']);self.assertTrue(snapshot['config']['investor']['enabled'])
  self.assertFalse(any(e['kind']=='error' for e in snapshot['events']))
 def test_unexpected_engine_error_pauses_enabled_portfolios(self):
  self.enable();self.enable('investor');self.engine.cycle=Mock(side_effect=RuntimeError('test failure'))
  with patch('sys.stderr'):self.run_checks(3)
  snapshot=self.engine.snapshot()
  self.assertTrue(all(not c['enabled'] for c in snapshot['config'].values()))
  # Trader, Investor, the baskets that start enabled (momentum, low volatility, 12-1 momentum, alpha, Stocks on the Move and the three research baskets), the four live quant baskets and the three signal baskets all pause.
  self.assertEqual(sum(e['kind']=='error' for e in snapshot['events']),17)
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
