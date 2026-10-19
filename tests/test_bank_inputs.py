import copy,unittest
from datetime import datetime,timezone
from marketdesk.bank_inputs import bank_annual,apply_bank_inputs,bank_record,not_applicable,anchors
from marketdesk.quality import financial_reasons


def table(rows):return '<table>'+''.join('<tr>'+''.join(f'<td>{c}</td>' for c in r)+'</tr>' for r in rows)+'</table>'

DATES=[['A','Date of start of reporting period','01-01-2026','01-04-2025'],['B','Date of end of reporting period','31-03-2026','31-03-2026'],
       ['C','Whether results are audited or unaudited','Audited','Audited'],['D','Nature of report standalone or consolidated','Standalone','Standalone']]
BAL_DATES=[['Date of start of reporting period','01-04-2025'],['Date of end of reporting period','31-03-2026'],['Whether results are audited or unaudited','Audited'],['Nature of report standalone or consolidated','Standalone']]
META=[['NSE Symbol','TESTBANK'],['ISIN','INE000B01010'],['Name of bank','Test Bank Limited'],['Class of security','Equity'],
      ['Date of start of financial year','01-04-2025'],['Date of end of financial year','31-03-2026'],['Description of presentation currency','INR'],
      ['Level of rounding used in financial results','Lakhs'],['Nature of report standalone or consolidated','Standalone'],
      ['Whether results are audited or unaudited for the Year to date for current period ended/year ended','Audited']]
INCOME=DATES+[['3','Total income','2,500.00','10,000.00'],['4','Interest expenses','1,500.00','6,000.00'],
              ['10','Total profit (loss) from ordinary activities before tax','400.00','1,600.00'],['14','Net profit (loss) for the period','300.00','1,200.00']]
BALANCE=BAL_DATES+[['','Capital','500.00'],['','Reserves and surplus','9,500.00'],['','Deposits','80,000.00'],['','Borrowings','5,000.00'],
                   ['','Cash and Balances with Reserve Bank of India','3,000.00'],['','Balances with Banks and money at call and short notice','2,000.00'],['','Total Assets','1,00,000.00']]
FLOW=[['A','Date of start of reporting period','01-04-2025'],['B','Date of end of reporting period','31-03-2026'],['C','Whether results are audited or unaudited','Audited'],
      ['D','Nature of report standalone or consolidated','Standalone'],['','Cash and cash equivalents cash flow statement at end of period','5,000.00']]

def html(meta=META,income=INCOME,balance=BALANCE,flow=FLOW):return table(meta)+table(income)+table(balance)+table(flow)

LATEST={'period':'Mar 2026','statement_type':'standalone','units_in':'crore','profit_before_tax':16.0,'equity':100.0,'cash_and_cash_equivalents':50.0,
        'finance_costs':None,'total_borrowings':None,'total_lease_liabilities':None,'sources':{'equity':{'inputs':{'_total_assets':{'value':1000.0}}}}}

def details():
    return {'sector':'Bank','status':'PARTIAL','missing_fields':['finance_costs','total_borrowings','total_lease_liabilities'],'latest':copy.deepcopy(LATEST),
            'summary':{'statement_type':'standalone','units':'crore','frequency':'yearly','revenue':100.0,'revenue_period':'Mar 2026','revenue_growth_yoy':5.0,
                       'net_profit':12.0,'net_profit_period':'Mar 2026','net_profit_growth_yoy':4.0}}

def proof(text=None):
    return {'status':'verified','symbol':'TESTBANK','isin':'INE000B01010','source_url':'https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_BANKING_1.html',
            'raw_sha256':'x','observed_at':datetime.now(timezone.utc).isoformat(),'bank':bank_annual(text or html(),'TESTBANK','INE000B01010','url')}


class BankInputTests(unittest.TestCase):
    def test_parses_audited_annual_column_in_crore(self):
        b=bank_annual(html(),'TESTBANK','INE000B01010','url')
        self.assertEqual((b['interest_expenses'],b['borrowings'],b['profit_before_tax'],b['cash_flow_closing']),(60.0,50.0,16.0,50.0))
        self.assertFalse(b['lease_caption_present'])
        for bad in (html(meta=[r if r[0]!='ISIN' else ['ISIN','OTHER'] for r in META]),html(meta=[r if 'Year to date' not in r[0] else [r[0],'Unaudited'] for r in META]),
                    html(meta=[r if r[0]!='Description of presentation currency' else [r[0],'USD'] for r in META])):
            with self.assertRaises(ValueError):bank_annual(bad,'TESTBANK','INE000B01010','url')

    def test_fills_missing_lines_and_marks_lease_not_applicable(self):
        out=apply_bank_inputs(details(),proof(),'TESTBANK','INE000B01010')
        latest=out['latest']
        self.assertEqual((latest['finance_costs'],latest['total_borrowings']),(60.0,50.0));self.assertIsNone(latest['total_lease_liabilities'])
        self.assertEqual(not_applicable(out),{'total_lease_liabilities'});self.assertEqual(out['status'],'COMPLETE')
        self.assertEqual(financial_reasons(out,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc)),[])

    def test_refuses_without_two_anchors_or_with_a_lease_caption(self):
        d=details();d['latest'].update(profit_before_tax=99.0,cash_and_cash_equivalents=1.0)
        with self.assertRaisesRegex(ValueError,'anchors'):apply_bank_inputs(d,proof(),'TESTBANK','INE000B01010')
        leased=html(balance=BALANCE+[['','Lease liabilities','10.00']])
        with self.assertRaisesRegex(ValueError,'lease caption'):apply_bank_inputs(details(),proof(leased),'TESTBANK','INE000B01010')
        d=details();d['latest']['statement_type']='consolidated'
        with self.assertRaises(ValueError):apply_bank_inputs(d,proof(),'TESTBANK','INE000B01010')

    def test_contradicting_cash_corrected_only_when_filing_confirms_it_twice(self):
        d=details();d['latest']['cash_and_cash_equivalents']=0.5
        out=apply_bank_inputs(d,proof(),'TESTBANK','INE000B01010')
        self.assertEqual(out['latest']['cash_and_cash_equivalents'],50.0);self.assertEqual(out['latest']['sources']['cash_and_cash_equivalents']['corrected_from'],0.5)
        unconfirmed=html(flow=FLOW[:-1]+[['','Cash and cash equivalents cash flow statement at end of period','4,000.00']])
        with self.assertRaisesRegex(ValueError,'confirm'):apply_bank_inputs(d,proof(unconfirmed),'TESTBANK','INE000B01010')

    def test_lease_not_applicable_needs_bank_source_and_matching_period(self):
        out=apply_bank_inputs(details(),proof(),'TESTBANK','INE000B01010')
        out['latest']['not_applicable']['total_lease_liabilities']['period']='Mar 2025'
        self.assertEqual(not_applicable(out),set())
        self.assertIn('Annual financial inputs incomplete',financial_reasons(out,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc)))

    def test_whole_record_replaces_only_an_older_provider_record(self):
        d=details();d['latest']['period']='Mar 2017'
        prior_html=html().replace('10,000.00','8,000.00').replace('1,200.00','-50.00').replace('2026','2025').replace('2025-','2024-')
        prior_html=html(meta=[[k,v.replace('2026','2025').replace('-04-2025','-04-2024')] for k,v in META],
                        income=[[c.replace('2026','2025').replace('-04-2025','-04-2024').replace('-01-2026','-01-2025') for c in r] for r in INCOME[:4]]+[['3','Total income','2,000.00','8,000.00'],['4','Interest expenses','1,000.00','5,000.00'],
                                ['10','Total profit (loss) from ordinary activities before tax','100.00','500.00'],['14','Net profit (loss) for the period','(10.00)','(50.00)']],
                        balance=[[c.replace('2026','2025').replace('-04-2025','-04-2024') for c in r] for r in BALANCE],flow=[[c.replace('2026','2025').replace('-04-2025','-04-2024') for c in r] for r in FLOW])
        prior={**proof(),'bank':bank_annual(prior_html,'TESTBANK','INE000B01010','url')}
        out=bank_record(d,{'current':proof(),'prior':prior},'TESTBANK','INE000B01010')
        self.assertEqual(out['latest']['period'],'Mar 2026');self.assertEqual(out['latest']['equity'],100.0)
        self.assertAlmostEqual(out['summary']['revenue_growth_yoy'],25.0)
        # Prior-year loss: growth stays undefined, both years kept, readiness accepts the verified inputs.
        self.assertIsNone(out['summary']['net_profit_growth_yoy']);self.assertEqual(out['summary']['growth_inputs']['net_profit']['prior'],-0.5)
        self.assertEqual(financial_reasons(out,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc)),[])
        current=details()
        with self.assertRaisesRegex(ValueError,'not older'):bank_record(current,{'current':proof(),'prior':prior},'TESTBANK','INE000B01010')


if __name__=='__main__':unittest.main()
