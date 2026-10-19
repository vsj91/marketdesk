import copy,unittest
from marketdesk.yahoo_crosscheck import apply_yahoo_crosscheck

def details():
    return {'sector':'Defence','status':'PARTIAL','missing_fields':['total_lease_liabilities'],'latest':{
        'period':'Mar 2026','units_in':'crore','statement_type':'standalone','profit_before_tax':567.8202,'equity':4240.9693,
        'finance_costs':3.38,'total_borrowings':0.0,'total_lease_liabilities':None,'cash_and_cash_equivalents':863.3149,
        'missing_fields':['total_lease_liabilities'],'sources':{}}}

def yahoo(**over):
    row={'period':'Mar 2026','period_end':'2026-03-31','currency':'INR','profit_before_tax':5678202000.0,'equity':42409693000.0,
         'cash_and_cash_equivalents':8633149000.0,'current_lease_liabilities':3152000.0,'non_current_lease_liabilities':1000000.0}
    row.update(over)
    return {'status':'available','symbol':'BDL.NS','observed_at':'2026-10-19T00:00:00+00:00','annual':[row]}

class YahooCrossCheckTests(unittest.TestCase):
    def test_matching_anchors_fill_lease_total_and_complete_the_row(self):
        out=apply_yahoo_crosscheck(details(),yahoo())
        self.assertAlmostEqual(out['latest']['total_lease_liabilities'],.4152)
        self.assertEqual(out['status'],'COMPLETE');self.assertEqual(out['missing_fields'],[])
        self.assertEqual(out['latest']['sources']['total_lease_liabilities']['source'],'yahoo_finance_cross_checked')
    def test_one_matching_anchor_is_not_enough(self):
        src=yahoo(equity=1.0,cash_and_cash_equivalents=1.0);d=details()
        self.assertEqual(apply_yahoo_crosscheck(d,src),d)
    def test_a_contradicting_anchor_rejects_even_with_two_matches(self):
        d=details();self.assertEqual(apply_yahoo_crosscheck(d,yahoo(cash_and_cash_equivalents=9999999999.0)),d)
    def test_no_zero_inference_and_no_partial_components(self):
        d=details()
        self.assertEqual(apply_yahoo_crosscheck(d,yahoo(current_lease_liabilities=0.0,non_current_lease_liabilities=0.0)),d)
        self.assertEqual(apply_yahoo_crosscheck(d,yahoo(non_current_lease_liabilities=None)),d)
    def test_period_unit_and_currency_must_match_and_input_is_unchanged(self):
        d=details();before=copy.deepcopy(d)
        for src in (yahoo(period='Mar 2025'),yahoo(currency='USD')):self.assertEqual(apply_yahoo_crosscheck(d,src),d)
        out=apply_yahoo_crosscheck(d,yahoo());self.assertEqual(d,before);self.assertIsNot(out,d)
    def test_borrowings_need_both_components(self):
        d=details();d['latest']['total_lease_liabilities']=1.0;d['latest']['total_borrowings']=None
        d['latest']['missing_fields']=['total_borrowings'];d['missing_fields']=['total_borrowings']
        self.assertEqual(apply_yahoo_crosscheck(d,yahoo(current_borrowings=100000000.0)),d)
        out=apply_yahoo_crosscheck(d,yahoo(current_borrowings=100000000.0,non_current_borrowings=50000000.0))
        self.assertAlmostEqual(out['latest']['total_borrowings'],15.0)
if __name__=='__main__':unittest.main()
