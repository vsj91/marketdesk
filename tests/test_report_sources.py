import unittest
from marketdesk.report_sources import bse_security,bse_pdf_url,company_reports
from unittest.mock import Mock,patch
from pathlib import Path
import tempfile

class ReportSourceTests(unittest.TestCase):
 def test_exact_isin_match_required(self):
  rows=[{'ISIN_NUMBER':'INE451C01013','Segment':'Equity','SCRIP_CD':'505036','Scrip_Name':'Example'}]
  self.assertEqual(bse_security(rows,'INE451C01013')['SCRIP_CD'],'505036')
  for data,isin in [(rows,'INE451C01014'),(rows+rows,'INE451C01013')]:
   with self.assertRaises(ValueError):bse_security(data,isin)
 def test_only_expected_attachment_names(self):
  name='c5a55e5f-0370-4b43-9a35-aa817cf714c2.pdf'
  self.assertEqual(bse_pdf_url('505036','\\'+name+'.pdf'),'https://www.bseindia.com/xml-data/corpfiling/AttachHis/'+name)
  self.assertEqual(bse_pdf_url('505036','73060505036.pdf'),'https://www.bseindia.com/bseplus/AnnualReport/505036/73060505036.pdf')
  for bad in ['../../private.pdf','https://evil.example/report.pdf','javascript:test','bad.pdf']:
   with self.assertRaises(ValueError):bse_pdf_url('505036',bad)
 def test_company_links_stay_on_reviewed_domain_and_period(self):
  response=Mock();response.text='''<a href="https://acglgoa.com/Annual-Report-2026.pdf">Annual Report 2026</a><a href="https://evil.example/Annual-Report-2026.pdf">Annual Report 2026</a><a href="https://acglgoa.com/Annual-Report-2024.pdf">Annual Report 2024</a>''';response.content=response.text.encode()
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.report_sources.public_request',return_value=response):
   got=company_reports(Mock(),'INE451C01013',2026,Path(tmp));self.assertEqual(len(got),1);self.assertTrue(got[0]['require_pdf_isin'])
