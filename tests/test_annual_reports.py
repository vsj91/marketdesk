import copy,unittest,json,tempfile,hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from marketdesk.annual_reports import parse_balance,parse_income,apply_report,balance_text,pdf_worker,retain_report,save_excerpt
from unittest.mock import Mock,patch
from marketdesk.quality import source_reasons

BASE={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','cash_and_cash_equivalents':5,'equity':50,'total_lease_liabilities':None,'missing_fields':['total_lease_liabilities']}
TEXT='''CONSOLIDATED BALANCE SHEET
as at March 31, 2026
Rs in lakhs
Particulars                 March 31, 2026      March 31, 2025
Cash and cash equivalents    12    500.00    400.00
Total equity                      5000.00    4500.00
Non-current liabilities
(ii) Lease liabilities        49      30.00     20.00
Current liabilities
(ii) Lease liabilities        49      20.00     10.00
Total equity and liabilities         5500.00   4900.00
'''
INCOME='''CONSOLIDATED STATEMENT OF PROFIT AND LOSS
for the year ended March 31, 2026
Amounts in INR lakhs
Particulars          March 31, 2026      March 31, 2025
Finance costs      24      50.00     40.00
Profit before tax (V - VI)        1000.00    900.00
'''
class AnnualReportTests(unittest.TestCase):
 def test_spaced_note_references_and_roman_sections(self):
  text=TEXT.replace('12    500.00','7 (c)    500.00').replace('Non-current liabilities','I NON-CURRENT LIABILITIES').replace('Current liabilities','II CURRENT LIABILITIES').replace('Cash and cash equivalents','Cash & Cash Equivalents')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
  with self.assertRaisesRegex(ValueError,'two matching'):
   parse_balance(text,{**BASE,'cash_and_cash_equivalents':10})
  with self.assertRaises(ValueError):parse_balance(text.replace('March 31, 2026','March 31, 2024'),BASE)
 def test_spaced_note_reference_does_not_supply_missing_values(self):
  text=TEXT.replace('12    500.00','7 (c)    500.00').replace('49      30.00','49 (a)      -')
  with self.assertRaisesRegex(ValueError,'No missing balance input'):parse_balance(text,BASE)
 def test_expanded_excerpt_does_not_overwrite_previous_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);p=root/'temporary.pdf';p.write_bytes(b'original balance page');old,digest=save_excerpt(root,p,'a'*64)
   p.write_bytes(b'balance and income pages');new,_=save_excerpt(root,p,'a'*64)
   self.assertNotEqual(old,new);self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(),digest)
   new.write_bytes(b'corrupted evidence')
   with self.assertRaisesRegex(ValueError,'hash mismatch'):save_excerpt(root,p,'a'*64)
 def test_annual_income_requires_pbt_anchor_and_explicit_finance_cost(self):
  latest={**BASE,'profit_before_tax':10,'finance_costs':None}
  self.assertEqual(parse_income(INCOME,latest)['value'],.5)
  self.assertEqual(parse_income(INCOME.replace('Profit before tax','VII Profit/(Loss) before tax').replace('Finance costs','Finance cost (Interest)'),latest)['value'],.5)
  for text in [INCOME.replace('1000.00','2000.00'),INCOME.replace('50.00','-'),INCOME.replace('Finance costs','Interest expense'),INCOME.replace('CONSOLIDATED','STANDALONE'),INCOME.replace('year ended','quarter ended'),INCOME.replace('INR','USD'),INCOME.replace('2026','2024')]:
   with self.assertRaises(ValueError):parse_income(text,latest)
 def test_income_evidence_cannot_fill_finance_without_matched_pbt(self):
  evidence=parse_balance(TEXT,BASE);evidence['values']['finance_costs']=.5
  report={'status':'verified','evidence':evidence,'source_url':'https://example.test/a.pdf','observed_at':'2026-10-10T00:00:00+00:00','raw_sha256':'abc','evidence_path':'balance.pdf'}
  for anchor,expected in [(None,None),(20,None),(10,.5)]:
   evidence['income_evidence']={'matched_profit_before_tax':anchor,'value':.5}
   d={'latest':{**BASE,'profit_before_tax':10,'finance_costs':None},'sector':'Tech'}
   self.assertEqual(apply_report(d,report)['latest']['finance_costs'],expected)
 def test_pdf_preserves_balance_and_income_evidence_pages(self):
  import pymupdf
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);doc=pymupdf.open()
   for text in (TEXT,INCOME):
    page=doc.new_page(width=600,height=840)
    for n,line in enumerate(text.splitlines()):page.insert_text((25,40+n*17),line,fontname='cour',fontsize=7)
   doc.save(root/'report.pdf');doc.close();(root/'context.json').write_text(json.dumps({'latest':{**BASE,'profit_before_tax':10,'finance_costs':None}}))
   pdf_worker(root/'report.pdf',root/'context.json',root/'result.json');r=json.loads((root/'result.json').read_text())
   self.assertEqual(r['status'],'verified',r);self.assertEqual(r['evidence']['values']['finance_costs'],.5);self.assertEqual(r['evidence']['income_evidence']['pages'],[2])
   self.assertEqual(len(pymupdf.open(root/'result.pdf')),2)
 def test_raw_import_retains_only_matching_intact_report_without_reaging(self):
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.annual_reports.ROOT',Path(tmp)):
   p=Path(tmp)/'balance.pdf';p.write_bytes(b'unchanged excerpt')
   base={'latest':copy.deepcopy(BASE),'sector':'Tech','missing_fields':['total_lease_liabilities']}
   report={'status':'verified','symbol':'TEST','isin':'INE123A01012','evidence':parse_balance(TEXT,BASE),'source_url':'https://example.test/a.pdf','observed_at':'2026-10-10T00:00:00+00:00','raw_sha256':'abc','evidence_path':'balance.pdf','excerpt_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
   previous=apply_report(copy.deepcopy(base),report)
   good=retain_report(copy.deepcopy(base),previous,'TEST','INE123A01012');self.assertEqual(good['latest']['total_lease_liabilities'],.5);self.assertEqual(good['latest']['sources']['total_lease_liabilities']['observed_at'],report['observed_at'])
   for key,value in [('period','Mar 2025'),('statement_type','standalone'),('cash_and_cash_equivalents',10)]:
    bad=copy.deepcopy(base);bad['latest'][key]=value;self.assertIsNone(retain_report(bad,previous,'TEST','INE123A01012')['latest']['total_lease_liabilities'])
   self.assertIsNone(retain_report(copy.deepcopy(base),previous,'WRONG','INE123A01012')['latest']['total_lease_liabilities'])
   p.write_bytes(b'tampered');self.assertIsNone(retain_report(copy.deepcopy(base),previous,'TEST','INE123A01012')['latest']['total_lease_liabilities'])
 def test_large_layout_padding_does_not_hide_header(self):
  text='\n'.join(' '*2500+line for line in TEXT.splitlines())
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
 def test_lacs_unit_alias_keeps_conversion(self):
  self.assertEqual(parse_balance(TEXT.replace('lakhs','lacs'),BASE)['value'],.5)
 def test_explicit_rupee_crore_abbreviation(self):
  text=TEXT.replace('Rs in lakhs','Amounts in Rs. cr, unless otherwise stated')
  self.assertEqual(parse_balance(text,{**BASE,'cash_and_cash_equivalents':500,'equity':5000})['value'],50)
  with self.assertRaises(ValueError):parse_balance(text.replace('Rs. cr','USD cr'),BASE)
 def test_currency_before_unit_and_note_suffixes(self):
  for unit in ('Amounts in INR lakhs','All amounts are in ` lakh, unless otherwise stated'):
   text=TEXT.replace('Rs in lakhs',unit).replace('12    500.00','12a    500.00')
   self.assertEqual(parse_balance(text,BASE)['value'],.5)
 def test_report_conflict_is_flagged_without_overwriting_existing_value(self):
  detail={'selected_source':'nse_audited_filing','latest':copy.deepcopy(BASE),'sector':'Tech'}
  e=parse_balance(TEXT,BASE);e['values']['total_borrowings']=3;detail['latest']['total_borrowings']=5
  report={'status':'verified','evidence':e,'source_url':'https://example.test/report.pdf','observed_at':datetime.now(timezone.utc).isoformat(),'raw_sha256':'abc','evidence_path':'data/report.pdf'}
  got=apply_report(detail,report);self.assertEqual(got['latest']['total_borrowings'],5);self.assertEqual(got['source_conflicts'][0]['annual_report'],3)
 def test_borrowings_can_be_recovered_without_guessing_missing_leases(self):
  text=TEXT.replace('30.00','-').replace('Current liabilities','Borrowings    49    200.00    190.00\nCurrent liabilities').replace('Total equity and liabilities','Borrowings    49    100.00    90.00\nTotal equity and liabilities')
  got=parse_balance(text,BASE)
  self.assertIsNone(got['value']);self.assertEqual(got['values']['total_borrowings'],3)
 def test_spread_pdf_and_auditor_context(self):
  import pymupdf
  for contextual in (False,True):
   with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);doc=pymupdf.open()
    if contextual:
     page=doc.new_page();page.insert_text((30,40),'Report on the Audit of the Consolidated Financial Statements')
    page=doc.new_page(width=1190,height=840)
    lines=TEXT.replace('CONSOLIDATED ','') if contextual else TEXT
    for n,line in enumerate(lines.splitlines()):page.insert_text((25,40+n*17),line,fontname='cour',fontsize=7)
    for n,line in enumerate(['STANDALONE STATEMENT OF PROFIT AND LOSS','March 31, 2026','Rs in lakhs','Profit before tax     888     777']):page.insert_text((640,40+n*17),line,fontname='cour',fontsize=7)
    doc.save(root/'report.pdf');doc.close();(root/'context.json').write_text(json.dumps({'latest':BASE}))
    pdf_worker(root/'report.pdf',root/'context.json',root/'result.json');r=json.loads((root/'result.json').read_text())
    self.assertEqual(r['status'],'verified',r);self.assertEqual(r['evidence']['layout'],'left_half');self.assertAlmostEqual(r['evidence']['value'],.5)
    if contextual:self.assertEqual(r['evidence']['basis_evidence_page'],1)
 def test_company_pdf_requires_isin(self):
  import pymupdf
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);doc=pymupdf.open();doc.new_page().insert_text((30,40),'Wrong company');doc.save(root/'report.pdf');doc.close()
   (root/'context.json').write_text(json.dumps({'latest':BASE,'require_pdf_isin':True,'isin':'INE123A01012'}))
   pdf_worker(root/'report.pdf',root/'context.json',root/'result.json');r=json.loads((root/'result.json').read_text());self.assertEqual(r['status'],'unresolved');self.assertIn('ISIN',r['reason'])
 def test_context_anchors_and_conversion(self):
  r=parse_balance(TEXT,BASE);self.assertEqual(r['value'],.5);self.assertEqual(len(r['matched_fields']),2)
 def test_wrong_year_basis_unit_and_anchor_rejected(self):
  for text in [TEXT.replace('2026','2024'),TEXT.replace('CONSOLIDATED','STANDALONE'),TEXT.replace('lakhs','unknown'),TEXT.replace('500.00','900.00'),TEXT.replace('5000.00','9000.00')]:
   with self.assertRaises(ValueError):parse_balance(text,BASE)
 def test_foreign_or_unspecified_currency_rejected(self):
  for text in (TEXT.replace('Rs in lakhs','USD in lakhs'),TEXT.replace('Rs in lakhs','in lakhs')):
   with self.assertRaises(ValueError):parse_balance(text,BASE)
 def test_custom_rupee_glyph_requires_rupee_font_and_unit(self):
  for font,expected in [('ITFRupee','₹ in crores'),('Regular','H in crores')]:
   page=Mock();page.get_text.side_effect=['H in crores',{'blocks':[{'lines':[{'spans':[{'text':'H','font':font},{'text':' in crores','font':'Regular'}]}]}]}]
   self.assertEqual(balance_text(page),expected)
 def test_dashes_and_missing_components_not_zero(self):
  for text in [TEXT.replace('30.00','-'),TEXT.replace('(ii) Lease liabilities        49      30.00     20.00','')]:
   with self.assertRaises(ValueError):parse_balance(text,BASE)
  self.assertEqual(parse_balance(TEXT.replace('30.00','0.00'),BASE)['value'],.2)
 def test_lease_assets_and_payments_not_balance(self):
  for name in ['Lease assets','Payments of lease liabilities','Other liabilities including lease liabilities']:
   with self.assertRaises(ValueError):parse_balance(TEXT.replace('Lease liabilities',name),BASE)
 def test_duplicate_captions_and_negative_values_rejected(self):
  for text in [TEXT.replace('Current liabilities','(ii) Lease liabilities        49      30.00     20.00\nCurrent liabilities'),TEXT.replace('30.00','(30.00)')]:
   with self.assertRaises(ValueError):parse_balance(text,BASE)
 def test_current_prior_columns_cannot_be_reversed(self):
  text=TEXT.replace('March 31, 2026      March 31, 2025','March 31, 2025      March 31, 2026')
  # Header date alone must not validate reversed data columns.
  with self.assertRaises(ValueError):parse_balance(text,BASE)
 def test_supplement_preserves_selected_statement_and_conflicts(self):
  detail={'selected_source':'nse_audited_filing','latest':copy.deepcopy(BASE),'sector':'Tech','missing_fields':['total_lease_liabilities'],'source_conflicts':[{'field':'equity'}]}
  report={'status':'verified','evidence':parse_balance(TEXT,BASE),'source_url':'https://nsearchives.nseindia.com/annual_reports/test.pdf','observed_at':datetime.now(timezone.utc).isoformat(),'raw_sha256':'abc','evidence_path':'data/report.pdf'}
  got=apply_report(detail,report);self.assertEqual(got['latest']['total_lease_liabilities'],.5);self.assertEqual(got['latest']['equity'],50);self.assertTrue(got['source_conflicts'])
  report['evidence']['statement_type']='standalone';detail['latest']['total_lease_liabilities']=None
  self.assertIsNone(apply_report(detail,report)['latest']['total_lease_liabilities'])
 def test_annual_report_evidence_age_checked(self):
  clock=datetime.now(timezone.utc);f={'selected_source':'nse_audited_filing','latest':{'observed_at':clock.isoformat(),'sources':{'total_lease_liabilities':{'source':'company_annual_report','observed_at':(clock-timedelta(days=31)).isoformat()}}}}
  self.assertTrue(source_reasons(f,{},clock))

class AdditionalLayoutTests(unittest.TestCase):
 def test_numeric_and_non_march_dates_keep_year_order(self):
  text=TEXT.replace('March 31, 2026','31.03.2026').replace('March 31, 2025','31.03.2025')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
  dec=TEXT.replace('March','December').replace('CONSOLIDATED','asCONSOLIDATEDat')
  self.assertEqual(parse_balance(dec,{**BASE,'period':'Dec 2026'})['value'],.5)
  for bad in (text.replace('31.03.2025','31.03.2024'),text.replace('31.03.2026','30.06.2026'),text.replace('31.03.2026      31.03.2025','31.03.2025      31.03.2026')):
   with self.assertRaises(ValueError):parse_balance(bad,BASE)
 def test_explicit_equity_components_require_nci_for_consolidated(self):
  text=TEXT.replace('Total equity                      5000.00    4500.00','Equity attributable to owners of the Company    4000.00    3500.00\nNon-controlling interests    1000.00    1000.00')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
  with self.assertRaises(ValueError):parse_balance(text.replace('Non-controlling interests    1000.00    1000.00',''),BASE)
 def test_continued_balance_spread_is_read_in_printed_order(self):
  import pymupdf
  left,right=TEXT.split('Current liabilities')
  right='CONSOLIDATED BALANCE SHEET (Contd.)\nAs at March 31, 2026\nRs in lakhs\nParticulars     March 31, 2026      March 31, 2025\nCurrent liabilities'+right
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);doc=pymupdf.open();p=doc.new_page(width=1190,height=840)
   for x,txt in ((25,left),(640,right)):
    for n,line in enumerate(txt.splitlines()):p.insert_text((x,40+n*17),line,fontname='cour',fontsize=7)
   doc.save(root/'r.pdf');doc.close();(root/'c.json').write_text(json.dumps({'latest':BASE}));pdf_worker(root/'r.pdf',root/'c.json',root/'r.json');r=json.loads((root/'r.json').read_text())
   self.assertEqual(r['status'],'verified',r);self.assertEqual(r['evidence']['value'],.5);self.assertEqual(r['evidence']['layout'],'joined_balance_spread')
 def test_lease_note_uses_carrying_closing_value_not_blank_components(self):
  from marketdesk.annual_reports import parse_lease_note
  note='''Notes to Consolidated Financial Statements
Set out below are the carrying amounts of lease liabilities and the movements during the year:
Rs in lakhs
Particulars              March 31, 2026      March 31, 2025
Opening balance       -       -
Additions           50.00      -
Closing balance     50.00      -
Current liabilities   -       -
'''
  self.assertEqual(parse_lease_note(note,BASE)['value'],.5)
  for bad in (note.replace('50.00      -\nCurrent','-      -\nCurrent'),note.replace('carrying amounts','undiscounted carrying amounts'),note.replace('Consolidated','Standalone'),note.replace('2026','2024')):
   with self.assertRaises(ValueError):parse_lease_note(bad,BASE)
  with self.assertRaises(ValueError):parse_lease_note(note,{**BASE,'non_current_lease_liabilities':2})
 def test_roman_row_and_note_labels_preserve_cash_anchor(self):
  text=TEXT.replace('Cash and cash equivalents    12','iii) Cash and cash equivalents    9(iii)').replace('49      30','39(ii)      30')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
 def test_failed_followup_keeps_verified_report_for_import(self):
  report={'status':'verified','excerpt_sha256':'a','evidence':parse_balance(TEXT,BASE),'source_url':'https://example.test/report.pdf','observed_at':'2026-10-13T00:00:00+00:00','raw_sha256':'b','evidence_path':'balance.pdf'}
  d=apply_report({'latest':copy.deepcopy(BASE),'sector':'Tech'},report)
  apply_report(d,{'status':'unresolved','reason':'New field unavailable'})
  self.assertEqual(d['annual_report_check'],report);self.assertEqual(d['annual_report_attempt']['status'],'unresolved')
 def test_note_recovery_still_requires_two_balance_anchors(self):
  import pymupdf
  note='''Notes to Consolidated Financial Statements
Set out below are the carrying amounts of lease liabilities and the movements during the year:
Rs in lakhs
Particulars              March 31, 2026      March 31, 2025
Closing balance     50.00      -
'''
  for cash,expected in [(500,'verified'),(700,'unresolved')]:
   with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);doc=pymupdf.open()
    balance=TEXT.replace('500.00',str(cash)+'.00').replace('20.00     10.00','-     10.00')
    for txt in (balance,note):
     p=doc.new_page(width=600,height=840)
     for n,line in enumerate(txt.splitlines()):p.insert_text((25,40+n*17),line,fontname='cour',fontsize=7)
    doc.save(root/'r.pdf');doc.close();(root/'c.json').write_text(json.dumps({'latest':BASE}));pdf_worker(root/'r.pdf',root/'c.json',root/'r.json');r=json.loads((root/'r.json').read_text())
    self.assertEqual(r['status'],expected,r)
    if expected=='verified':self.assertEqual(r['evidence']['lease_note_evidence']['value'],.5);self.assertIn(2,r['evidence']['pages'])
 def test_archive_dedup_requires_fresh_observation_and_fresh_matching_copy(self):
  from marketdesk.annual_reports import known_archive_duplicate
  row={'provider':'BSE','fileName':'https://www.bseindia.com/xml-data/corpfiling/AttachHis/a.pdf'}
  old={'observed_at':datetime.now(timezone.utc).isoformat(),'attempts':[{'provider':'BSE','source_url':row['fileName'],'raw_sha256':'abc'}]}
  self.assertEqual(known_archive_duplicate(row,old,{'abc'})['raw_sha256'],'abc')
  self.assertIsNone(known_archive_duplicate(row,old,{'different'}))
  self.assertIsNone(known_archive_duplicate({**row,'provider':'Company website'},old,{'abc'}))
  self.assertIsNone(known_archive_duplicate(row,{**old,'observed_at':(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()},{'abc'}))

class BalanceLayoutRecoveryTests(unittest.TestCase):
 def test_bullet_prefixes_do_not_hide_cash_or_leases(self):
  text=TEXT.replace('Cash and cash equivalents','- Cash and cash equivalents').replace('(ii) Lease liabilities','-Lease liabilities')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
 def test_unlabelled_equity_subtotal_requires_component_arithmetic(self):
  text=TEXT.replace('Total equity                      5000.00    4500.00','Equity\nEquity share capital    100.00    100.00\nOther equity    4900.00    4400.00\n5000.00    4500.00')
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
  with self.assertRaises(ValueError):parse_balance(text.replace('5000.00    4500.00','5100.00    4500.00'),BASE)
 def test_end_of_balance_does_not_read_following_note_rows(self):
  text=TEXT+'\nNon-controlling interest    1.00    1.00\nNon-controlling interest    2.00    2.00\n'
  self.assertEqual(parse_balance(text,BASE)['value'],.5)
 def test_dash_remains_missing_not_zero(self):
  with self.assertRaises(ValueError):parse_balance(TEXT.replace('30.00','-').replace('20.00','-'),BASE)
