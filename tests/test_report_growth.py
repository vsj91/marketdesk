import unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
from marketdesk.report_growth import parse_growth,recover_report_growth,extract_pdf
BASE={'period':'Mar 2026','statement_type':'standalone','units_in':'crore','profit_before_tax':10,'finance_costs':1,'net_profit':8,'revenue':100}
TEXT='''STANDALONE STATEMENT OF PROFIT AND LOSS
for the year ended March 31, 2026
INR in lakhs
Particulars               March 31, 2026          March 31, 2025
Revenue from operations    10000.00    9000.00
Finance costs    100.00    90.00
Profit before tax    1000.00    900.00
Net Profit After Tax    800.00    700.00
'''
class ReportGrowthTests(unittest.TestCase):
 def make_pdf(self,root,text=TEXT):
  import pymupdf
  doc=pymupdf.open();page=doc.new_page()
  for n,line in enumerate(text.splitlines()):page.insert_text((20,40+n*20),line,fontname='cour',fontsize=7)
  path=root/'source.pdf';doc.save(path);doc.close();return path

 def test_growth_uses_bse_after_nse_network_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);pdf=self.make_pdf(root);response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.iter_content.return_value=[pdf.read_bytes()]
   row={'fileName':'https://www.bseindia.com/verified.pdf','provider':'BSE','identity':{'isin':'TESTISIN'},'index_url':'https://www.bseindia.com/index'}
   with patch('marketdesk.report_growth.ROOT',root),patch('marketdesk.report_growth.Fallbacks'),patch('marketdesk.report_growth.request',side_effect=OSError('NSE unavailable')),patch('marketdesk.report_sources.bse_reports',return_value=[row]) as bse,patch('marketdesk.report_sources.public_request',return_value=response):
    proof=recover_report_growth('TEST','TESTISIN',{'latest':BASE})
   self.assertEqual(proof['provider'],'BSE');self.assertEqual(proof['evidence']['prior']['net_profit'],7)
   self.assertEqual(proof['identity'],{'isin':'TESTISIN'});self.assertEqual(proof['source_checks'][0]['status'],'unavailable')
   self.assertEqual(bse.call_args.args[1:3],('TESTISIN',2026))

 def test_alternate_source_cannot_bypass_financial_anchors(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);pdf=self.make_pdf(root,TEXT.replace('1000.00','2000.00'));response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.iter_content.return_value=[pdf.read_bytes()]
   with patch('marketdesk.report_growth.ROOT',root),patch('marketdesk.report_growth.Fallbacks'),patch('marketdesk.report_growth.request',side_effect=OSError('NSE unavailable')),patch('marketdesk.report_sources.bse_reports',return_value=[{'fileName':'https://www.bseindia.com/verified.pdf'}]),patch('marketdesk.report_sources.public_request',return_value=response):
    with self.assertRaises(ValueError):recover_report_growth('TEST','TESTISIN',{'latest':BASE})

 def test_company_pdf_requires_exact_isin(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);pdf=self.make_pdf(root,TEXT+'\nISIN: INE123A01012')
   self.assertEqual(extract_pdf(pdf,BASE,expected_isin='INE123A01012')[0]['current']['net_profit'],8)
   with self.assertRaisesRegex(ValueError,'expected ISIN'):extract_pdf(pdf,BASE,expected_isin='INE999A01012')

 def test_explicit_year_after_tax_caption_keeps_anchors(self):
  text=TEXT.replace('Profit before tax','Profit for the year before tax (5 - 6)').replace('Net Profit After Tax','Net profit for the year after tax (7 - 8)')
  self.assertEqual(parse_growth(text,BASE)['prior']['net_profit'],7)
  with self.assertRaises(ValueError):parse_growth(text.replace('before tax (5 - 6)','before exceptional items and tax (5 - 6)'),BASE)
 def test_customer_contract_revenue_requires_same_anchors(self):
  text=TEXT.replace('Revenue from operations','Revenue from contracts with customers')
  self.assertEqual(parse_growth(text,BASE)['current']['revenue'],100)
  with self.assertRaises(ValueError):parse_growth(text.replace('10000.00','11000.00'),BASE)
 def test_matching_annual_comparatives(self):
  result=parse_growth(TEXT,BASE);self.assertEqual(result['current']['net_profit'],8);self.assertEqual(result['prior']['net_profit'],7);self.assertEqual(result['prior']['period'],'Mar 2025')
 def test_ligatures_are_decoded_without_changing_numbers(self):
  self.assertEqual(parse_growth(TEXT.replace('Profit','Proﬁt'),BASE)['current']['profit_before_tax'],10)
 def test_wrong_context_anchors_missing_or_negative_denominator_blocked(self):
  for text in [TEXT.replace('STANDALONE','CONSOLIDATED'),TEXT.replace('INR','USD'),TEXT.replace('1000.00','2000.00'),TEXT.replace('700.00','-'),TEXT.replace('Net Profit After Tax','Other comprehensive income')]:
   with self.assertRaises(ValueError):parse_growth(text,BASE)
 def test_negative_prior_profit_keeps_both_years_without_growth(self):
  parsed=parse_growth(TEXT.replace('700.00','(700.00)'),BASE)
  self.assertAlmostEqual(parsed['prior']['net_profit'],-7.0)
 def test_duplicate_profit_rows_rejected(self):
  with self.assertRaises(ValueError):parse_growth(TEXT+'Net Profit After Tax    800.00    700.00\n',BASE)
 def test_company_growth_evidence_time_is_required(self):
  from marketdesk.quality import source_reasons
  from datetime import datetime,timezone,timedelta
  stamp=datetime.now(timezone.utc);fresh=stamp.isoformat()
  d={'selected_source':'nse_audited_filing','latest':{'observed_at':fresh},'summary':{'revenue_growth_yoy':10,'net_profit_growth_yoy':10,'growth_sources':{'revenue':'company_annual_report','net_profit':'company_annual_report'},'growth_observed_at':{'revenue':[fresh,fresh],'net_profit':[fresh,fresh]}}}
  self.assertEqual(source_reasons(d,{},stamp),[])
  d['summary']['growth_observed_at']['net_profit']=[(stamp-timedelta(days=31)).isoformat()]
  self.assertTrue(source_reasons(d,{},stamp))
