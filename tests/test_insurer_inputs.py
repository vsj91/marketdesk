import copy,unittest
from datetime import datetime,timezone
from marketdesk.insurer_inputs import insurer_annual,insurer_xbrl,insurer_record,form_apl
from marketdesk.bank_inputs import not_applicable
from marketdesk.quality import financial_reasons


def table(rows):return '<table>'+''.join('<tr>'+''.join(f'<td>{c}</td>' for c in r)+'</tr>' for r in rows)+'</table>'

def filing(year=2026,borrowings='0.00',pat='2,000.00',revenue='10,000.00'):
    y,p=str(year),str(year-1)
    meta=[['NSE Symbol','TESTLIFE'],['ISIN','INE000L01010'],['Class of security','Equity'],['Type Of Insurance','Linked Policy'],
          ['Date of start of financial year','01-04-'+p],['Date of end of financial year','31-03-'+y],['Description of presentation currency','INR'],
          ['Level of rounding used in financial results','Lakhs'],['Nature of report standalone or consolidated','Standalone'],
          ['Whether results are audited or unaudited for the Year to date for current period ended/year ended','Audited']]
    results=[['Date of start of reporting period','01-01-'+y,'01-04-'+p],['Date of end of reporting period','31-03-'+y,'31-03-'+y],
             ['Whether results are audited or unaudited','Audited','Audited'],['Nature of report standalone or consolidated','Standalone','Standalone'],
             ['(i)','Gross premium income',''],['','Gross premium income','3,000.00',revenue]]
    shareholders=[['3','Profit/ (loss) before tax','700.00','2,500.00'],['7','Profit/ (loss) after tax and Extraordinary Items','600.00',pat]]
    balance=[['Date of start of reporting period','01-04-'+p],['Date of end of reporting period','31-03-'+y],['Whether results are audited or unaudited','Audited'],
             ['Nature of report standalone or consolidated','Standalone'],['1','Share capital','1,000.00'],['2','Reserves and surplus','9,000.00'],
             ['5','Borrowings',borrowings],['','Total Sources of Funds','50,000.00']]
    flows=[['A','Date of start of reporting period','01-04-'+p],['B','Date of end of reporting period','31-03-'+y],['C','Whether results are audited or unaudited','Audited'],
           ['D','Nature of report standalone or consolidated','Standalone'],['','Cash and cash equivalents cash flow statement at end of period','4,000.00']]
    return table(meta)+table(results)+table(shareholders)+table(balance)+table(flows)

def proof(html):
    return {'status':'verified','symbol':'TESTLIFE','isin':'INE000L01010','source_url':'url','raw_sha256':'x','observed_at':datetime.now(timezone.utc).isoformat(),
            'bank':insurer_annual(html,'TESTLIFE','INE000L01010','url')}

def details():return {'sector':'Insurance','status':'PARTIAL','latest':{'period':'Mar 2017','statement_type':'consolidated'},'summary':{}}


class InsurerInputTests(unittest.TestCase):
    def test_record_from_two_audited_filings_with_zero_borrowings(self):
        prior={**proof(filing(2025,pat='1,600.00',revenue='8,000.00'))}
        out=insurer_record(details(),{'current':proof(filing()),'prior':prior},'TESTLIFE','INE000L01010')
        latest=out['latest']
        self.assertEqual((latest['profit_before_tax'],latest['equity'],latest['total_borrowings'],latest['cash_and_cash_equivalents']),(25.0,100.0,0.0,40.0))
        self.assertEqual(not_applicable(out),{'finance_costs','total_lease_liabilities'})
        self.assertAlmostEqual(out['summary']['revenue_growth_yoy'],25.0);self.assertAlmostEqual(out['summary']['net_profit_growth_yoy'],25.0)
        self.assertEqual(financial_reasons(out,latest['observed_at'],datetime.now(timezone.utc)),[])

    def test_insurer_with_borrowings_needs_a_real_finance_cost(self):
        out=insurer_record(details(),{'current':proof(filing(borrowings='2,500.00')),'prior':None},'TESTLIFE','INE000L01010')
        self.assertEqual(not_applicable(out),{'total_lease_liabilities'});self.assertIsNone(out['latest']['finance_costs'])
        self.assertIn('Annual financial inputs incomplete',financial_reasons(out,out['latest']['observed_at'],datetime.now(timezone.utc)))

    def test_identity_audit_and_lease_caption_are_enforced(self):
        for bad in (filing().replace('INE000L01010','INE999L01010'),filing().replace('year ended</td><td>Audited','year ended</td><td>Unaudited'),filing().replace('<td>Audited</td><td>Audited</td>','<td>Audited</td><td>Unaudited</td>'),
                    filing().replace('Lakhs','Thousands')):
            with self.assertRaises(ValueError):insurer_annual(bad,'TESTLIFE','INE000L01010','url')
        leased=filing().replace('<tr><td></td><td>Total Sources of Funds</td>','<tr><td></td><td>Lease liabilities</td><td>5.00</td></tr><tr><td></td><td>Total Sources of Funds</td>')
        with self.assertRaisesRegex(ValueError,'lease caption'):insurer_record(details(),{'current':proof(leased),'prior':None},'TESTLIFE','INE000L01010')

    def test_form_apl_reads_interest_on_subordinated_debt(self):
        W=' '*8
        page=f"""FORM A-PL
Consolidated Profit and Loss Account for the year ended March 31, 2026
Shareholders' Account (Non-technical Account){W}(₹ in Lakh)
Particulars{W}For the year ended{W}For the year ended
{W}March 31, 2026{W}March 31, 2025
Interest on subordinated debt{W}22,000{W}11,716
Profit / (Loss) before tax{W}196,233{W}187,794
Profit / (Loss) after tax{W}191,232{W}181,082"""
        v=form_apl(page,{'period':'Mar 2026'})
        self.assertEqual((v['basis'],v['finance_costs'][0],v['profit_before_tax'][0]),('consolidated',220.0,1962.33))
        with self.assertRaisesRegex(ValueError,'unit'):form_apl(page.replace('(₹ in Lakh)',''),{'period':'Mar 2026'})
        with self.assertRaisesRegex(ValueError,'dates'):form_apl(page,{'period':'Mar 2027'})

    def test_xbrl_instance_values_and_letter_o_isin_typo(self):
        def fact(tag,ctx,value,d='-5'):return f'<in-capmkt:{tag} contextRef="{ctx}"'+(f' decimals="{d}" unitRef="INR"' if d else '')+f'>{value}</in-capmkt:{tag}>'
        ctx=('<xbrli:context id="Y"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:startDate>2025-04-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>'
             '<xbrli:context id="I"><xbrli:entity><xbrli:identifier scheme="s">1</xbrli:identifier></xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period></xbrli:context>')
        body=''.join([fact('Symbol','Y','TESTGEN',None),fact('ISIN','Y','INE575PO1O11',None),fact('DescriptionOfPresentationCurrency','Y','INR',None),fact('NatureOfReportStandaloneConsolidated','Y','Standalone',None),
                      fact('DateOfStartOfFinancialYear','Y','2025-04-01',None),fact('DateOfEndOfFinancialYear','Y','2026-03-31',None),fact('WhetherResultsAreAuditedOrUnaudited','Y','Audited',None),
                      fact('ProfitOrLossBeforeTax','Y','7543100000'),fact('GrossPremiumsWritten','Y','186219200000'),fact('ProfitLossAfterTax','Y','5569700000'),
                      fact('ShareCapital','I','5884000000'),fact('ReservesAndSurplus','I','70008300000'),fact('Borrowings','I','4700000000'),fact('CashAndCashEquivalentsCashFlowStatement','I','14435600000')])
        xml='<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/in-capmkt">'+ctx+body+'</xbrli:xbrl>'
        v=insurer_xbrl(xml,'TESTGEN','INE575P01011','u.xml')
        self.assertAlmostEqual(v['profit_before_tax'],754.31);self.assertAlmostEqual(v['borrowings'],470.0);self.assertEqual(v['filed_isin'],'INE575PO1O11')
        self.assertFalse(v['borrowings_printed_zero'])
        with self.assertRaisesRegex(ValueError,'identity'):insurer_xbrl(xml,'TESTGEN','INE575P01012','u.xml')


if __name__=='__main__':unittest.main()
