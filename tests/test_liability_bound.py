import copy,unittest
from marketdesk.liability_bound import liability_bound,bounded_debt_pass
from marketdesk.quality import financial_reasons
from marketdesk.autotrading import entry_signal
from datetime import datetime,timezone

def statement(current=500,non_current=200,equity=1000,period='Mar 2026'):
    rows={'Current Liabilities':current,'Non-Current Liabilities':non_current,'Equity Capital':equity,'Total Equity & Liabilities':equity+current+non_current}
    return {'data':{'type':'consolidated','units_in':'crore','full_statement':[{'particular':k,'history':[{'period':period,'value':v}]} for k,v in rows.items()]}}

def record(missing=('total_lease_liabilities',)):
    latest={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','profit_before_tax':100,'finance_costs':10,'equity':1000,'total_borrowings':100,'total_lease_liabilities':10,'cash_and_cash_equivalents':300}
    for k in missing:latest[k]=None
    return {'status':'PARTIAL','missing_fields':list(missing),'latest':latest,
            'summary':{'revenue_period':'Mar 2026','net_profit_period':'Mar 2026','statement_type':'consolidated','units':'crore','frequency':'yearly','revenue_growth_yoy':10,'net_profit_growth_yoy':20}}

class LiabilityBoundTests(unittest.TestCase):
    def test_bound_needs_matching_balanced_statement(self):
        f=record()
        self.assertEqual(liability_bound(f,statement())['total_liabilities'],700)
        self.assertIsNone(liability_bound(f,statement(period='Mar 2025')))
        unbalanced=statement();unbalanced['data']['full_statement'][3]['history'][0]['value']+=100
        self.assertIsNone(liability_bound(f,unbalanced))
        other=statement();other['data']['type']='standalone'
        self.assertIsNone(liability_bound(f,other))

    def test_worst_case_net_debt_decides(self):
        low=record();low['liability_bound']=liability_bound(low,statement())
        self.assertTrue(bounded_debt_pass(low))   # (700-300)/1000 = 0.4
        high=record();high['liability_bound']=liability_bound(high,statement(current=900))
        self.assertFalse(bounded_debt_pass(high)) # (1100-300)/1000 = 0.8: unknown, still excluded
        other=record(('finance_costs',));other['liability_bound']=liability_bound(other,statement())
        self.assertFalse(bounded_debt_pass(other))

    def test_bounded_record_is_eligible_but_unbounded_is_not(self):
        clock=datetime(2026,10,19,5,tzinfo=timezone.utc)
        f=record();f['liability_bound']=liability_bound(f,statement())
        self.assertNotIn('Annual financial inputs incomplete',financial_reasons(f,clock.isoformat(),clock))
        signal={'as_of':'2026-10-16T00:00:00+05:30','close':100,'dma200':90,'turnover20':20_000_000,'fundamentals':f}
        self.assertIsNotNone(entry_signal('investor',signal,101,clock))
        bare=copy.deepcopy(f);bare.pop('liability_bound')
        self.assertIn('Annual financial inputs incomplete',financial_reasons(bare,clock.isoformat(),clock))
        self.assertIsNone(entry_signal('investor',{**signal,'fundamentals':bare},101,clock))

if __name__=='__main__':unittest.main()
