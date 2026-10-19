import unittest
from datetime import datetime,timezone
from marketdesk.reit_inputs import reit_annual,apply_reit_inputs
from marketdesk.quality import financial_reasons


def table(rows):return '<table>'+''.join('<tr>'+''.join(f'<td>{c}</td>' for c in r)+'</tr>' for r in rows)+'</table>'

META=[['NSE Symbol','TESTREIT'],['ISIN','INE041025011'],['Class of security - REITs or INVITs','REITs'],['Date of start of financial year','01-04-2025'],
      ['Date of end of financial year','31-03-2026'],['Description of presentation currency','INR'],['Level of rounding','Lakhs'],
      ['Nature of report standalone or consolidated','Consolidated'],['Whether results are audited or unaudited for the Year to date for current period ended/year ended','Audited']]
DATES=[['A','Date of start of reporting period','01-01-2026','01-04-2025'],['B','Date of end of reporting period','31-03-2026','31-03-2026'],
       ['C','Whether results are audited or unaudited','Audited','Audited'],['D','Nature of report standalone or consolidated','Consolidated','Consolidated']]
INCOME=DATES+[['(e)','Finance costs','37,391.70','1,49,504.20'],['5','Total profit/(loss) before tax','16,352.50','1,01,842.00']]
BALANCE=[['Date of start of reporting period','01-04-2025'],['Date of end of reporting period','31-03-2026'],['Whether results are audited or unaudited','Audited'],
         ['Nature of report standalone or consolidated','Consolidated'],['','Cash and cash equivalents','96,966.60'],['','Total assets','50,39,824.00'],
         ['','Total Unit Capital','20,78,017.90'],['','Borrowings, non-current','17,21,074.50'],['','Lease liabilities, non-current','14,159.50'],
         ['','Borrowings, current','5,17,404.40'],['','Lease liabilities, current','891.90'],['','Total liabilities','29,61,806.10']]

def html(meta=META,balance=BALANCE):return table(meta)+table(INCOME)+table(balance)

def details():
    return {'sector':'Real Estate Investment Trusts','status':'PARTIAL','missing_fields':['finance_costs','total_borrowings','total_lease_liabilities'],
            'latest':{'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','profit_before_tax':1133.57,'equity':20780.18,'cash_and_cash_equivalents':969.67,
                      'finance_costs':None,'total_borrowings':None,'total_lease_liabilities':None},
            'summary':{'statement_type':'consolidated','units':'crore','frequency':'yearly','revenue':4582.36,'revenue_period':'Mar 2026','revenue_growth_yoy':12.0,
                       'net_profit':338.55,'net_profit_period':'Mar 2026','net_profit_growth_yoy':5.0}}

def proof(text=None):
    return {'status':'verified','symbol':'TESTREIT','isin':'INE041025011','source_url':'url','raw_sha256':'x','observed_at':datetime.now(timezone.utc).isoformat(),
            'reit':reit_annual(text or html(),'TESTREIT','INE041025011','url')}


class ReitInputTests(unittest.TestCase):
    def test_fills_missing_lines_with_two_anchors(self):
        out=apply_reit_inputs(details(),proof(),'TESTREIT','INE041025011')
        latest=out['latest']
        self.assertAlmostEqual(latest['finance_costs'],1495.042);self.assertAlmostEqual(latest['total_borrowings'],22384.789);self.assertAlmostEqual(latest['total_lease_liabilities'],150.514)
        self.assertEqual(out['reit_inputs']['matched_anchors'],['cash_and_cash_equivalents','equity'])
        self.assertEqual(financial_reasons(out,latest['observed_at'] if latest.get('observed_at') else datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc)),[])

    def test_identity_audit_anchor_and_unprinted_part(self):
        for bad in (html(meta=[r if r[0]!='ISIN' else ['ISIN','INE999025011'] for r in META]),html(meta=[r if not r[0].startswith('Whether results') else [r[0],'Unaudited'] for r in META])):
            with self.assertRaises(ValueError):reit_annual(bad,'TESTREIT','INE041025011','url')
        d=details();d['latest'].update(equity=1.0,cash_and_cash_equivalents=1.0)
        with self.assertRaisesRegex(ValueError,'anchors'):apply_reit_inputs(d,proof(),'TESTREIT','INE041025011')
        blank=html(balance=[r if r[1]!='Lease liabilities, current' else ['','Lease liabilities, current',''] for r in BALANCE])
        out=apply_reit_inputs(details(),proof(blank),'TESTREIT','INE041025011')
        self.assertIsNone(out['latest']['total_lease_liabilities'])


if __name__=='__main__':unittest.main()
