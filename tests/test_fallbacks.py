import unittest
from marketdesk.fallbacks import nse_annual,apply_fallback
from marketdesk.provider import FUNDAMENTAL_REQUIRED_FIELDS

def fixture():
 def table(rows):return '<table>'+''.join('<tr>'+''.join('<td>'+str(c)+'</td>' for c in row)+'</tr>' for row in rows)+'</table>'
 meta=[['NSE Symbol','TEST'],['ISIN','INE000A01010'],['Class of security','Equity'],['Description of presentation currency','INR'],['Level of rounding used in financial results','Lakhs'],['Nature of report standalone or consolidated','Consolidated'],['Date of start of financial year','01-04-2025'],['Date of end of financial year','31-03-2026'],['Whether results are audited or unaudited for the Year to date for current period ended/year ended','Audited']]
 headers=[['Date of start of reporting period','01-04-2025','01-04-2024'],['Date of end of reporting period','31-03-2026','31-03-2025'],['Whether results are audited or unaudited','Audited','Audited'],['Nature of report standalone or consolidated','Consolidated','Consolidated']]
 income=headers+[['Total profit before tax',1000,900],['Finance costs',100,90]]
 balance=headers+[['Total equity',5000,4000],['Non-current liabilities'],['Borrowings, non-current',200,150],['Lease liabilities',30,20],['Current liabilities'],['Borrowings, current',100,80],['Lease liabilities',20,10],['Cash and cash equivalents',500,400]]
 return table(meta)+table(income)+table(balance)

class FallbackTests(unittest.TestCase):
 def test_empty_revenue_heading_does_not_mask_reported_total(self):
  marker='<tr><td>Finance costs</td><td>100</td><td>90</td></tr>'
  rows='<tr><td>Revenue from operations</td><td></td><td></td></tr><tr><td>Total Revenue From Operations</td><td>10000</td><td>9000</td></tr>'
  html=fixture().replace(marker,marker+rows)
  for income_only in (False,True):
   self.assertEqual(nse_annual(html,'TEST','INE000A01010','url',income_only=income_only)['revenue'],100)
   self.assertEqual(nse_annual(html,'TEST','INE000A01010','url',year_offset=-1,income_only=income_only)['revenue'],90)
   with self.assertRaisesRegex(ValueError,'Conflicting NSE income'):
    nse_annual(html.replace('<td></td><td></td>','<td>11000</td><td>9000</td>'), 'TEST','INE000A01010','url',income_only=income_only)
 def test_explicit_lease_caption_variants_keep_period_and_section(self):
  for label in ('Lease Liablity','Lease libilities','Lease Lianilities','Lease liabities','Lease Liabilitiea','leaseliabilities','ROU Lease Liabilities','Finance Lease Liability','Finance Lease Liabilities','Net Liability in Leased Assets','Current Maturity of Leased Liability'):
   row=nse_annual(fixture().replace('Lease liabilities',label),'TEST','INE000A01010','url')
   self.assertEqual(row['total_lease_liabilities'],.5,label)
  html=fixture().replace('Lease liabilities','Lease Liabilities - Non Current',1)
  html=html.replace('Lease liabilities','Lease Liabilities - Current',1)
  self.assertEqual(nse_annual(html,'TEST','INE000A01010','url')['total_lease_liabilities'],.5)
  for label in ('Lease Liability - Right of Use Assets','Unamortised lease liabilities','Other non-current financial lease liabilities'):
   self.assertEqual(nse_annual(fixture().replace('Lease liabilities',label,1),'TEST','INE000A01010','url')['total_lease_liabilities'],.5)
  for label in ('Lease liabilities, Capital Creditors and Deferred Expenses','Advance Lease Rental','Leasehold Land','Finance lease receiveables'):
   self.assertIsNone(nse_annual(fixture().replace('Lease liabilities',label),'TEST','INE000A01010','url')['total_lease_liabilities'])
 def test_explicit_annual_context_and_unit_conversion(self):
  row=nse_annual(fixture(),'TEST','INE000A01010','https://nsearchives.nseindia.com/example')
  self.assertEqual(row['profit_before_tax'],10)
  self.assertEqual(row['finance_costs'],1)
  self.assertEqual(row['total_borrowings'],3)
  self.assertEqual(row['total_lease_liabilities'],.5)
  self.assertEqual(row['missing_fields'],[])
 def test_wrong_company_rejected(self):
  for symbol,isin in (('OTHER','INE999A01010'),('TEST','INE999A01010'),('OTHER','INE000B01010')):
   with self.assertRaises(ValueError):nse_annual(fixture(),symbol,isin,'url')
  # The identical ISIN under a renamed symbol is the same security.
  self.assertEqual(nse_annual(fixture(),'RENAMED','INE000A01010','url')['filing_identity'],'symbol_renamed')
 def test_bad_basis_and_units_rejected(self):
  for text in [fixture().replace('Lakhs','Thousands'),fixture().replace('Consolidated','Unknown')]:
   with self.assertRaises(ValueError):nse_annual(text,'TEST','INE000A01010','url')
 def test_no_quarter_as_annual(self):
  with self.assertRaises(ValueError):nse_annual(fixture().replace('01-04-2025','01-01-2026'),'TEST','INE000A01010','url')
 def test_omitted_lease_not_zero(self):
  row=nse_annual(fixture().replace('Lease liabilities','Other liabilities'),'TEST','INE000A01010','url')
  self.assertIsNone(row['total_lease_liabilities'])
  self.assertIn('total_lease_liabilities',row['missing_fields'])
 def test_nse_replacement_records_conflict_without_mixing(self):
  row=nse_annual(fixture(),'TEST','INE000A01010','url')
  details={'latest':{'profit_before_tax':11,'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','missing_fields':['finance_costs']},'sector':'Test'}
  result=apply_fallback(details,{'nse':{'annual':[row]}})
  self.assertEqual(result['latest']['profit_before_tax'],10)
  self.assertEqual(result['source_conflicts'][0]['upstox'],11)
  self.assertEqual(result['status'],'COMPLETE')
 def test_yahoo_unknown_basis_never_fills_upstox_gaps(self):
  details={'latest':{'period':'Mar 2026'},'missing_fields':['finance_costs'],'status':'PARTIAL'}
  result=apply_fallback(details,{'yahoo':{'annual':[{'finance_costs':0}]}})
  self.assertEqual(result['status'],'PARTIAL');self.assertNotIn('finance_costs',result['latest'])
if __name__=='__main__':unittest.main()
