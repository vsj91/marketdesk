import unittest
from marketdesk.report_growth import parse_growth
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
 def test_matching_annual_comparatives(self):
  result=parse_growth(TEXT,BASE);self.assertEqual(result['current']['net_profit'],8);self.assertEqual(result['prior']['net_profit'],7);self.assertEqual(result['prior']['period'],'Mar 2025')
 def test_ligatures_are_decoded_without_changing_numbers(self):
  self.assertEqual(parse_growth(TEXT.replace('Profit','Proﬁt'),BASE)['current']['profit_before_tax'],10)
 def test_wrong_context_anchors_missing_or_negative_denominator_blocked(self):
  for text in [TEXT.replace('STANDALONE','CONSOLIDATED'),TEXT.replace('INR','USD'),TEXT.replace('1000.00','2000.00'),TEXT.replace('700.00','(700.00)'),TEXT.replace('700.00','-'),TEXT.replace('Net Profit After Tax','Other comprehensive income')]:
   with self.assertRaises(ValueError):parse_growth(text,BASE)
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
