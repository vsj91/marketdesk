import json,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from marketdesk.strategy_performance import attribution,trader_performance
from marketdesk.storage import init,connect,set_state
from marketdesk.paper import init_paper,portfolios

CLOCK=datetime(2026,10,13,8,0,tzinfo=timezone.utc)
SWITCH='2026-10-13T07:00:00+00:00'

def fill(side,qty,price,fee=1,cohort='claude',symbol='TEST'):
 return dict(symbol=symbol,side=side,quantity=qty,price=price,fee=fee,
  created_at='2026-10-13T06:00:00+00:00' if cohort=='legacy' else '2026-10-13T07:30:00+00:00',
  source='manual' if cohort=='other' else 'automatic',request_id='claude-example-request' if cohort=='claude' else 'legacy-example-request',
  reason='Original Claude V2 completed breakout; signal 2026-10-12' if cohort=='claude' and side=='BUY' else 'Claude protective stop')

def position(qty,cost,mark=110,symbol='TEST'):
 return dict(symbol=symbol,quantity=qty,cost=cost,mark=mark,mark_time=CLOCK.isoformat(),mark_source='last_trade')

def groups(trades,positions):
 return {g['key']:g for g in attribution(trades,positions,SWITCH,CLOCK)['cohorts']}

class AttributionTests(unittest.TestCase):
 def test_no_claude_trades_does_not_claim_inherited_gain(self):
  g=groups([fill('BUY',10,100,2,'legacy')],[position(10,1002)])
  self.assertEqual(g['claude']['net_after_charged_costs'],0)
  self.assertEqual(g['claude']['buy_fills'],0)
  self.assertEqual(g['legacy']['net_after_charged_costs'],98)
 def test_claude_sell_of_legacy_is_not_claude_profit(self):
  g=groups([fill('BUY',10,100,2,'legacy'),fill('SELL',10,110,3)],[])
  self.assertEqual(g['legacy']['realized_net'],95)
  self.assertEqual(g['legacy']['fees_charged'],5)
  self.assertEqual(g['claude']['realized_net'],0)
  self.assertEqual(g['claude']['fees_charged'],0)
 def test_closed_then_reentered_same_symbol_separates_strategies(self):
  g=groups([fill('BUY',10,100,2,'legacy'),fill('SELL',10,90,3),fill('BUY',5,95,2)],[position(5,477,100)])
  self.assertEqual(g['legacy']['realized_net'],-105)
  self.assertEqual(g['claude']['unrealized_net'],23)
  self.assertEqual(g['claude']['fees_charged'],2)
 def test_partial_sale_cost_allocation_and_fees_not_deducted_twice(self):
  g=groups([fill('BUY',10,100,2),fill('SELL',4,110,3)],[position(6,601.2)])['claude']
  self.assertEqual(g['realized_net'],36.2)
  self.assertEqual(g['unrealized_net'],58.8)
  self.assertEqual(g['net_after_charged_costs'],95)
  self.assertEqual(g['fees_charged'],5)
  self.assertAlmostEqual(g['net_after_estimated_exit_fees'],95-g['estimated_exit_fees'])
 def test_mixed_manual_position_uses_proportional_average_cost(self):
  g=groups([fill('BUY',10,100,2),fill('BUY',10,120,2,'other'),fill('SELL',10,130,4)],[position(10,1102,125)])
  self.assertEqual(g['claude']['realized_net'],147)
  self.assertEqual(g['other']['realized_net'],47)
  self.assertEqual(g['claude']['unrealized_net'],124)
  self.assertEqual(g['other']['unrealized_net'],24)
  self.assertEqual(sum(r['fees_charged'] for r in g.values()),8)
 def test_repeated_mixed_partial_sales_fully_close(self):
  trades=[fill('BUY',7,100,2),fill('BUY',10,120,2,'other')]
  trades += [fill('SELL',q,130,1) for q in (1,2,3,4,7)]
  g=groups(trades,[])
  self.assertEqual(sum(x['realized_net'] for x in g.values()),301)
  self.assertTrue(all(not x['open_symbols'] for x in g.values()))
 def test_missing_mark_not_reported_as_zero_profit(self):
  g=groups([fill('BUY',10,100)],[position(10,1001,None)])['claude']
  self.assertIsNone(g['unrealized_net']);self.assertIsNone(g['net_after_charged_costs'])
 def test_stale_marks_are_flagged(self):
  p=position(10,1001);p['mark_time']=(CLOCK-timedelta(seconds=31)).isoformat()
  a=attribution([fill('BUY',10,100)],[p],SWITCH,CLOCK)
  self.assertEqual(a['stale_symbols'],['TEST'])
 def test_sale_without_history_and_mismatched_basis_fail_closed(self):
  with self.assertRaises(ValueError):groups([fill('SELL',10,100)],[])
  with self.assertRaises(ValueError):groups([fill('BUY',10,100)],[position(10,999)])
 def test_manual_prefix_cannot_claim_claude_entry(self):
  t=fill('BUY',10,100);t['source']='manual'
  g=groups([t],[position(10,1001)])
  self.assertEqual(g['claude']['buy_fills'],0);self.assertEqual(g['other']['buy_fills'],1)
 def test_sell_fee_estimate_avoids_duplicate_same_day_dp(self):
  a=groups([fill('BUY',10,100),fill('SELL',4,110)],[position(6,600.6)])['claude']['estimated_exit_fees']
  b=groups([fill('BUY',6,100,.6)],[position(6,600.6)])['claude']['estimated_exit_fees']
  self.assertAlmostEqual(b-a,23.6)

class LedgerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'test.db';init(self.path);init_paper(self.path)
  with connect(self.path) as d:
   d.execute("INSERT INTO instruments VALUES('TEST','NSE_EQ|TEST','TEST','Test','Test')")
   set_state(d,'auto_config',{'trader':{'enabled':True,'strategy':'claude-original-v2','strategy_selected_at':SWITCH},'investor':{'enabled':True}})
 def test_uses_full_ledger_beyond_200_fills_and_changes_nothing(self):
  with connect(self.path) as d:
   cash=1000000
   for i in range(202):
    t=fill('BUY' if i%2==0 else 'SELL',1,100 if i%2==0 else 110)
    cash+=-101 if i%2==0 else 109
    d.execute('INSERT INTO trades(id,request_id,portfolio,symbol,side,quantity,price,fee,quote_time,created_at,source,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(str(i),'claude-'+str(i),'trader','TEST',t['side'],1,t['price'],1,0,t['created_at'],'automatic',t['reason']))
   d.execute("UPDATE portfolios SET cash=? WHERE name='trader'",(cash,))
  with connect(self.path) as d:before='\n'.join(d.iterdump())
  result={a['name']:a for a in portfolios(self.path)}
  p=result['trader']['strategy_performance'];self.assertEqual(p['status'],'ok')
  self.assertEqual(p['cohorts'][0]['realized_net'],808)
  self.assertEqual(p['cohorts'][0]['buy_fills'],101)
  self.assertEqual(len(result['trader']['trades']),200)
  self.assertNotIn('strategy_performance',result['investor'])
  with connect(self.path) as d:self.assertEqual(before,'\n'.join(d.iterdump()))
 def test_unexplained_cash_change_is_not_strategy_profit(self):
  with connect(self.path) as d:d.execute("UPDATE portfolios SET cash=cash+100 WHERE name='trader'")
  t=next(a for a in portfolios(self.path) if a['name']=='trader')
  self.assertEqual(t['strategy_performance']['status'],'unavailable')

if __name__=='__main__':unittest.main()
