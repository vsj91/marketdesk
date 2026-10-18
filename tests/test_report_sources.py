import hashlib,unittest
from marketdesk.report_sources import bse_security,bse_pdf_url,company_reports,download_report
from unittest.mock import Mock,MagicMock,patch
import requests
from pathlib import Path
import tempfile

class ReportSourceTests(unittest.TestCase):
 def response(self,chunks,headers=None):
  response=MagicMock();response.__enter__.return_value=response
  response.headers=headers or {};response.iter_content.return_value=chunks
  return response
 def test_interrupted_pdf_restarts_and_hashes_only_complete_response(self):
  def broken():
   yield b'%PDF-partial'
   raise requests.exceptions.ChunkedEncodingError('interrupted')
  raw=b'%PDF-complete'
  first=self.response(broken());second=self.response([raw],{'Content-Length':str(len(raw))})
  request=Mock(side_effect=[first,second])
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.report_sources.time.sleep'):
   path=Path(tmp)/'report.pdf';sha,size=download_report(request,Mock(),'https://source.test/report.pdf',path)
   self.assertEqual(path.read_bytes(),raw);self.assertEqual(size,len(raw));self.assertEqual(sha,hashlib.sha256(raw).hexdigest())
  self.assertEqual(request.call_count,2);first.__exit__.assert_called_once();second.__exit__.assert_called_once()
 def test_incomplete_download_is_bounded_and_leaves_no_partial_file(self):
  request=Mock(side_effect=[self.response([b'%PDF-'],{'Content-Length':'100'}) for _ in range(3)])
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.report_sources.time.sleep'):
   path=Path(tmp)/'report.pdf'
   with self.assertRaises(requests.exceptions.ChunkedEncodingError):download_report(request,Mock(),'https://source.test/r.pdf',path)
   self.assertFalse(path.exists());self.assertEqual(request.call_count,3)
 def test_invalid_oversized_and_access_denied_responses_are_not_retried(self):
  for request,limit in [(Mock(return_value=self.response([b'<html>blocked</html>'])),100),(Mock(return_value=self.response([b'%PDF-too-large'])),5),(Mock(side_effect=ValueError('NSE HTTP 403')),100)]:
   with tempfile.TemporaryDirectory() as tmp:
    path=Path(tmp)/'report.pdf'
    with self.assertRaises(ValueError):download_report(request,Mock(),'https://source.test/r.pdf',path,limit=limit)
    self.assertFalse(path.exists());self.assertEqual(request.call_count,1)
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
 def test_legacy_http_company_links_upgrade_only_same_host(self):
  response=Mock();response.text='<a href="http://3pland.com/Annual%20Report%202025-2026.pdf">Download</a><a href="http://evil.test/Annual-Report-2026.pdf">Annual report</a>';response.content=response.text.encode()
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.report_sources.public_request',return_value=response):
   got=company_reports(Mock(),'INE105C01023',2026,Path(tmp));self.assertEqual(len(got),1);self.assertTrue(got[0]['fileName'].startswith('https://3pland.com/'))
 def test_upload_year_does_not_override_older_financial_year(self):
  response=Mock();response.text='<a href="https://acglgoa.com/2026/Annual-Report-2024-25.pdf">Annual Report 2024-25</a>';response.content=response.text.encode()
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.report_sources.public_request',return_value=response):
   self.assertEqual(company_reports(Mock(),'INE451C01013',2026,Path(tmp)),[])
