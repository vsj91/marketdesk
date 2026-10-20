import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from fundamentals_history import parse_xbrl,action_factors,dividends,rupees

def xbrl(facts,instant='2024-03-31'):
    ctx=f'<xbrli:context id="OneI"><xbrli:entity></xbrli:entity><xbrli:period><xbrli:instant>{instant}</xbrli:instant></xbrli:period></xbrli:context>'
    body=''.join(f'<in-bse-fin:{t} contextRef="{c}" unitRef="INR" decimals="-5">{v}</in-bse-fin:{t}>' for t,c,v in facts)
    return '<xbrli:xbrl>'+ctx+body+'</xbrli:xbrl>'
YEAR=[('DateOfStartOfReportingPeriod','FourD','2023-04-01'),('DateOfEndOfReportingPeriod','FourD','2024-03-31'),
      ('DateOfStartOfReportingPeriod','OneD','2024-01-01'),('DateOfEndOfReportingPeriod','OneD','2024-03-31'),
      ('WhetherResultsAreAuditedOrUnaudited','FourD','Audited'),('NatureOfReportStandaloneConsolidated','FourD','Consolidated')]

class FundamentalsHistoryTests(unittest.TestCase):
    def test_full_year_column_is_read_from_period_facts_not_the_quarter(self):
        r=parse_xbrl(xbrl(YEAR+[('ProfitLossForPeriod','OneD','5000000000'),('ProfitLossForPeriod','FourD','20000000000'),
            ('RevenueFromOperations','FourD','100000000000'),('Equity','OneI','80000000000'),('FaceValueOfEquityShareCapital','FourD','5')]),2024)
        self.assertEqual((r['net_profit'],r['revenue'],r['equity'],r['face_value']),(2000.,10000.,8000.,5.))   # crore; per-share in rupees
        self.assertTrue(r['audited']);self.assertEqual(r['basis'],'consolidated')
    def test_missing_conflicting_or_unaudited_values_stay_missing(self):
        r=parse_xbrl(xbrl(YEAR+[('FinanceCosts','FourD','1'),('FinanceCosts','FourD','2')]),2024)
        self.assertIsNone(r['finance_costs']);self.assertIsNone(r['borrowings_nc'])          # conflicting / not filed
        unaudited=[x if x[0]!='WhetherResultsAreAuditedOrUnaudited' else (x[0],x[1],'Unaudited') for x in YEAR]
        self.assertFalse(parse_xbrl(xbrl(unaudited),2024)['audited'])
        self.assertIsNone(parse_xbrl(xbrl(YEAR+[('Equity','OneI','1')],instant='2023-03-31'),2024)['equity'])   # prior year-end column
    def test_bonus_and_split_ratios_and_non_equity_bonuses(self):
        rows=lambda *s:[{'subject':t,'exDate':'04-Sep-2018'} for t in s]
        self.assertEqual(action_factors(rows('Bonus 1:1','Bonus- 1:2')),[('2018-09-04',2.),('2018-09-04',1.5)])
        self.assertEqual(action_factors(rows('Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share')),[('2018-09-04',10.)])
        self.assertEqual(action_factors(rows('Scheme Of Arrangement - Bonus Ncrps 4:1','Bonus Preference Shares 21:1','Dividend - Rs 5 Per Share')),[])
        self.assertIsNone(action_factors(rows('Bonus issue, ratio to be announced')))           # unreadable: no share counts at all
    def test_dividends_convert_to_todays_share_units(self):
        acts=[{'subject':'Final Dividend - Rs 10 Per Share','exDate':'01-Jul-2018'},{'subject':'Interim Dividend - Rs 2.50 Per Share','exDate':'01-Jan-2020'}]
        self.assertEqual(dividends(acts,[('2019-01-01',2.)]),[('2018-07-01',5.),('2020-01-01',2.5)])   # a later 1:1 bonus halves the old amount
    def test_rupee_filings_get_an_explicit_unit_only_when_none_is_stated(self):
        html='<table><tr><th>Description of presentation currency</th><td>INR  (in Actuals)</td></tr><!-- <tr><th>Level of rounding used in financial results</th><td>Lakhs</td></tr> --></table>'
        self.assertIn('<td>Level of rounding used in financial results</td><td>Rupees',rupees(html))
        stated='<table><tr><td>INR</td></tr><tr><td>Level of rounding used in financial results</td><td>Lakhs</td></tr></table>'
        self.assertEqual(rupees(stated),stated)

if __name__=='__main__':unittest.main()
