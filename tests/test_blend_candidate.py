import unittest
from datetime import datetime,timezone,timedelta
from marketdesk.blend_candidate import trader,investor

class BlendCandidateTests(unittest.TestCase):
 def test_trader_requires_quality_and_rejects_chasing(self):
  s=dict(quality=True,close=101,high20=100,dma50=95,dma200=90,turnover20=60_000_000,volume_ratio=1.5,rank=.2,atr=2)
  self.assertTrue(trader(s,101)['eligible']);self.assertFalse(trader(s,105)['eligible']);self.assertFalse(trader(s,99)['eligible']);self.assertFalse(trader(s,101,False)['eligible'])
 def test_investor_does_not_infer_value_or_governance_from_growth(self):
  f={'latest':dict(equity=100,total_borrowings=10,total_lease_liabilities=1,profit_before_tax=25,finance_costs=1),'summary':dict(net_profit=20,revenue_growth_yoy=15,net_profit_growth_yoy=15)}
  clock=datetime.now(timezone.utc);q={'investor_ready':True}
  self.assertTrue(investor(f,q,'Engineering','ISIN',clock=clock)['quantitative_pass'])
  self.assertFalse(investor(f,q,'Engineering','ISIN',clock=clock)['eligible'])
  review={'isin':'ISIN','observed_at':clock.isoformat(),**{k:{'approved':True,'source_url':'https://example.com/verified-evidence'} for k in ('valuation','governance','business_quality')}}
  self.assertTrue(investor(f,q,'Engineering','ISIN',review,clock)['eligible'])
  self.assertFalse(investor(f,q,'Bank','ISIN',review,clock)['eligible'])
  review['observed_at']=(clock-timedelta(days=91)).isoformat();self.assertFalse(investor(f,q,'Engineering','ISIN',review,clock)['eligible'])
  review['observed_at']=(clock+timedelta(days=1)).isoformat();self.assertFalse(investor(f,q,'Engineering','ISIN',review,clock)['eligible'])
 def test_missing_financials_never_become_zero(self):
  r=investor({}, {'investor_ready':True},'Engineering','ISIN');self.assertFalse(r['eligible']);self.assertFalse(r['quantitative_pass'])
