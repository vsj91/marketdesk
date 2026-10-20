import json,unittest
from marketdesk.valuation import shares_crore,metrics,NM

LATEST={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','profit_before_tax':130.0,'finance_costs':20.0,'equity':500.0,'total_borrowings':250.0}
SUMMARY={'statement_type':'consolidated','units':'crore','frequency':'annual','net_profit':100.0,'net_profit_period':'Mar 2026','revenue':1000.0,'revenue_period':'Mar 2026'}
def details(latest=None,summary=None):return {'latest':{**LATEST,**(latest or {})},'summary':{**SUMMARY,**(summary or {})}}
def statement(pat=100.0,eps=5.0,period='Mar 2026',kind='consolidated',units='crore'):
    return json.dumps({'status':'success','data':{'type':kind,'units_in':units,'full_statement':[
        {'particular':'Profit After Tax','history':[{'value':pat,'period':period}]},{'particular':'EPS - Basic','history':[{'value':eps,'period':period}]}]}})

class ValuationTests(unittest.TestCase):
    def test_formulas_use_the_latest_year(self):
        v=metrics(details(),price=60.0,shares=20.0,financial=False)
        self.assertEqual(v['market_cap'],1200.0)
        self.assertAlmostEqual(v['pe'],12.0);self.assertAlmostEqual(v['pb'],2.4)
        self.assertAlmostEqual(v['roe'],20.0);self.assertAlmostEqual(v['net_margin'],10.0)
        self.assertAlmostEqual(v['roce'],20.0)  # (130+20)/(500+250)
    def test_financials_skip_roce(self):
        self.assertNotIn('roce',metrics(details(),60.0,20.0,True))
    def test_loss_and_non_positive_denominators_are_not_meaningful(self):
        loss=metrics(details(summary={'net_profit':-10.0}),60.0,20.0,False)
        self.assertEqual((loss['pe'],loss['roe'],loss['net_margin']),(NM,NM,NM))
        self.assertEqual(metrics(details(summary={'net_profit':0.0}),60.0,20.0,False)['pe'],NM)
        neg=metrics(details(latest={'equity':-300.0,'total_borrowings':100.0}),60.0,20.0,False)
        self.assertEqual((neg['pb'],neg['roe'],neg['roce']),(NM,NM,NM))
        self.assertEqual(metrics(details(latest={'profit_before_tax':-50.0}),60.0,20.0,False)['roce'],NM)
        self.assertEqual(metrics(details(summary={'revenue':0.0}),60.0,20.0,False)['net_margin'],NM)
    def test_missing_inputs_stay_missing_never_zero(self):
        v=metrics(details(latest={'total_borrowings':None,'equity':None}),None,20.0,False)
        self.assertEqual((v['market_cap'],v['pe'],v['pb'],v['roe'],v['roce']),(None,None,None,None,None))
        self.assertAlmostEqual(v['net_margin'],10.0)
        self.assertIsNone(metrics(details(),60.0,None,False)['pe'])
        self.assertIsNone(metrics({},60.0,20.0,False)['roe'])
    def test_other_year_or_basis_is_not_mixed(self):
        old=metrics(details(summary={'net_profit_period':'Mar 2025'}),60.0,20.0,False)
        self.assertEqual((old['pe'],old['roe'],old['net_margin']),(None,None,None))
        self.assertIsNone(metrics(details(summary={'statement_type':'standalone'}),60.0,20.0,False)['roe'])
    def test_shares_from_matching_income_statement_only(self):
        self.assertEqual(shares_crore(statement(),LATEST),20.0)
        for bad in (statement(period='Mar 2025'),statement(kind='standalone'),statement(units='lakh'),statement(eps=0),statement(pat=-100.0),'{}','not json',None):
            self.assertIsNone(shares_crore(bad,LATEST))
        self.assertEqual(shares_crore(statement(pat=-100.0,eps=-5.0),LATEST),20.0)

if __name__=='__main__':unittest.main()
