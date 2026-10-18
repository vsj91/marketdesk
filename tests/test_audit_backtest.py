import copy,unittest
from datetime import date,timedelta
from marketdesk.audit_backtest import run,metrics

class BacktestCausalityTests(unittest.TestCase):
 def data(self):
  days=[];d=date(2025,1,1)
  while len(days)<205:
   if d.weekday()<5:days.append(str(d))
   d+=timedelta(days=1)
  bars={day:dict(ts=day+'T00:00:00+05:30',open=102,high=104,low=99,close=102,volume=1000000) for day in days}
  signals={day:dict(as_of=day,close=102,atr=2,dma50=99,dma200=90,high20=101,volume_ratio=1.5,turnover20=20000000,rank=.2,quality=True) for day in days}
  data=dict(symbols=['TEST'],bars={'TEST':bars},signals={'TEST':signals},sectors={'TEST':'Test'},funds={'TEST':({},'2099-01-01T00:00:00+00:00')},evidence={'TEST':{}},benchmark={day:dict(as_of=day,allowed=True) for day in days},sessions=days)
  return data,days[201:]
 def test_current_close_cannot_change_same_day_buy(self):
  data,days=self.data();a=run(copy.deepcopy(data),days,100000,'trader')
  changed=copy.deepcopy(data);changed['bars']['TEST'][days[0]]['close']=150
  b=run(changed,days,100000,'trader')
  self.assertTrue(a['trades']);self.assertEqual(a['trades'][0],b['trades'][0])
  self.assertNotEqual(a['daily'][0]['equity'],b['daily'][0]['equity'])
 def test_missing_point_in_time_fundamentals_are_not_invented(self):
  data,days=self.data();r=run(data,days,100000,'investor')
  self.assertEqual(r['trades'],[]);self.assertTrue(all(c['investor_evidence_eligible']==0 for c in r['coverage']))
  self.assertIsNone(r['metrics']['win_rate_pct'])
 def test_fee_floor_never_enlarges_orders(self):
  data,days=self.data();a=run(copy.deepcopy(data),days,100000,'trader',fee_limit=None)
  b=run(copy.deepcopy(data),days,100000,'trader',fee_limit=.1)
  self.assertTrue(a['trades']);self.assertFalse(b['trades']);self.assertGreater(b['rejections']['fee_floor'],0)
 def test_metrics_report_undefined_ratios_as_missing(self):
  m=metrics([], [dict(equity=100000,cash=100000)],100000,{})
  self.assertIsNone(m['profit_factor']);self.assertIsNone(m['fees_pct_closed_gross_profit']);self.assertIsNone(m['average_win'])
