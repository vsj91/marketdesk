"""Regression fragments from inspected official PDF pages; no financial defaults."""
import json,unittest
from pathlib import Path
from unittest.mock import Mock
import pymupdf
from marketdesk.annual_reports import parse_balance,balance_text,statement_context
F=Path(__file__).parent/'fixtures'
class LeaseLayoutTests(unittest.TestCase):
 def fixture(self,s):
  return (F/(s+'_lease_layout.txt')).read_text(),json.loads((F/(s+'_lease_layout.json')).read_text())
 def test_tatasteel_printed_page_references_do_not_mask_cash(self):
  text,base=self.fixture('tatasteel');e=parse_balance(text,base,allow_identity=False)
  self.assertEqual(e['values']['total_lease_liabilities'],7425.29)
  self.assertEqual(e['matched_fields'],['cash_and_cash_equivalents','equity'])
  self.assertEqual(e['borrowings_reconciliation']['reason'],'vendor figure includes leases')
  with self.assertRaises(ValueError):parse_balance(text.replace('Note       Page','Note       Other'),base,allow_identity=False)
 def test_joined_basis_title_and_hyphenated_total_equity(self):
  text,base=self.fixture('caplipoint');e=parse_balance(text,base)
  self.assertEqual(e['values']['total_lease_liabilities'],4.04)
  self.assertEqual(e['matched_fields'],['cash_and_cash_equivalents','equity'])
  with self.assertRaises(ValueError):parse_balance(text.replace('CONSOLIDATED','STANDALONE'),base)
  with self.assertRaises(ValueError):parse_balance(text.replace('3,630.80','3,631.80'),base)
 def test_singular_cash_caption_preserves_two_anchor_check(self):
  text,base=self.fixture('ltts');e=parse_balance(text,base)
  self.assertEqual(e['values']['total_lease_liabilities'],578.5)
  with self.assertRaises(ValueError):parse_balance(text.replace('16,497','16,4979'),base)
 def test_adjacent_currency_scale_still_needs_explicit_currency(self):
  text,base=self.fixture('ltts')
  import re
  text=re.sub(r'\([^\n]*(?:million|lakh)[^\n]*\)','(All amounts in `million unless otherwise stated)',text,flags=re.I)
  self.assertEqual(statement_context(text,base)[2],('million',.1))
  with self.assertRaises(ValueError):statement_context(text.replace('`million','million'),base)
 def test_only_known_nonoverlapping_left_margin_navigation_is_cropped(self):
  for label,edge,crop in [('GARDEN REACH SHIPBUILDERS & ENGINEERS LTD. | ANNUAL REPORT 2025-26',30,True),('Lease liabilities | ANNUAL REPORT 2025-26',70,False),('Consolidated Financial Statements',30,True),('Lease liabilities',30,False),('Consolidated Financial Statements',70,False)]:
   page=Mock();page.rect=pymupdf.Rect(0,0,600,800)
   data={'blocks':[{'lines':[{'dir':(0.,-1.),'bbox':(20,200,edge,400),'spans':[{'text':label,'font':'Regular'}]}, {'dir':(1.,0.),'bbox':(60,220,550,230),'spans':[{'text':'Non-current liabilities','font':'Regular'}]}]}]}
   page.get_text.side_effect=lambda *a,**kw: data if a and a[0]=='dict' else ('clean' if 'clip' in kw else 'raw')
   self.assertEqual(balance_text(page),'clean' if crop else 'raw')
 def test_explicit_lease_year_end_labels_do_not_read_maturity_totals(self):
  from marketdesk.annual_reports import parse_lease_note
  base={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore'}
  for symbol,value in [('pnbhousing',170.44),('swancorp',154.8534)]:
   text=(F/(symbol+'_lease_note.txt')).read_text()
   self.assertAlmostEqual(parse_lease_note(text,base)['value'],value)
   with self.assertRaises(ValueError):parse_lease_note(text.replace('Consolidated','Standalone').replace('CONSOLIDATED','STANDALONE'),base)
   with self.assertRaises(ValueError):parse_lease_note(text.replace('Lease liability as at the end of the year','Undiscounted lease liability as at the end of the year').replace('Lease Liability as at end of the year','Undiscounted Lease Liability as at end of the year'),base)
 def test_caption_markers_are_not_missing_values(self):
  from marketdesk.annual_reports import caption
  self.assertEqual(caption('i(a) Lease Liability'),'lease liability')
  self.assertEqual(caption('Cash and cash equivalents........'),'cash and cash equivalents')
  self.assertEqual(caption('Other equity *'),'other equity')
  text,base=self.fixture('ltts')
  self.assertEqual(parse_balance(text.replace('Total equity','Total - Equity (3)'),base)['value'],578.5)
 def test_short_closing_caption_still_requires_dated_carrying_balance(self):
  from marketdesk.annual_reports import parse_lease_note
  base={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore'}
  text=(F/'vtl_lease_note.txt').read_text()
  self.assertEqual(parse_lease_note(text,base)['value'],.20)
  for bad in [text.replace('Balance at the end','Payment of Lease Liabilities'),text.replace('Balance at the end','Undiscounted balance at the end'),text.replace('Consolidated','Standalone'),text.replace('0.20','-')]:
   with self.assertRaises(ValueError):parse_lease_note(bad,base)
 def test_auditor_annexure_requires_explicit_own_audit_basis(self):
  from marketdesk.annual_reports import audited_section_basis
  clause='in conjunction with our audit of the standalone financial statements of the Company for the year ended on that date'
  text='ANNEXURE TO THE INDEPENDENT AUDITOR’S REPORT We have audited internal controls '+clause
  self.assertEqual(audited_section_basis(text),('standalone',clause))
  self.assertIsNone(audited_section_basis(text.replace('INDEPENDENT AUDITOR’S REPORT','DIRECTORS REPORT')))
  self.assertIsNone(audited_section_basis(text.replace(clause,'The Company has no subsidiaries')))
  self.assertIsNone(audited_section_basis(text+' Report on the audit of the consolidated financial statements'))
 def test_pdf_coordinates_verify_jagged_headers_without_reordering_values(self):
  from marketdesk.annual_reports import verified_column_transcription
  import copy
  text=(F/'paradeep_header.txt').read_text();words=json.loads((F/'paradeep_header_words.json').read_text());base={'period':'Mar 2026','units_in':'crore','statement_type':'consolidated'}
  page=Mock();page.rect=pymupdf.Rect(0,0,595,842);page.get_text.return_value=words
  fixed,proof=verified_column_transcription(text,page,base)
  self.assertEqual([h['year'] for h in proof['headers']],[2026,2025]);self.assertTrue(fixed.endswith(text))
  statement_context(fixed,base)
  for mode in ['reverse','missing_prior','ambiguous']:
   bad=copy.deepcopy(words)
   if mode=='reverse':
    for w in bad:
     if w[0]>400 and w[4] in ['2025','2026']:w[4]='2025' if w[4]=='2026' else '2026'
   elif mode=='missing_prior':bad=[w for w in bad if w[4]!='2025']
   else:bad+=copy.deepcopy([w for w in bad if w[4]=='2025'])
   page.get_text.return_value=bad;self.assertEqual(verified_column_transcription(text,page,base),(text,None))
 def test_margin_folio_does_not_hide_vertical_navigation(self):
  page=Mock();page.rect=pymupdf.Rect(0,0,600,800)
  lines=[{'dir':(0.,-1.),'bbox':(20,200,35,400),'spans':[{'text':'Financial Statements','font':'Regular'}]}, {'dir':(1.,0.),'bbox':(50,220,550,230),'spans':[{'text':'Non-current liabilities','font':'Regular'}]}, {'dir':(1.,0.),'bbox':(20,50,35,60),'spans':[{'text':'274','font':'Regular'}]}]
  page.get_text.side_effect=lambda *a,**kw: {'blocks':[{'lines':lines}]} if a and a[0]=='dict' else ('clean' if 'clip' in kw else 'raw')
  self.assertEqual(balance_text(page),'clean')
