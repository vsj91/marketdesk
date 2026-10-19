import copy,json,unittest
from pathlib import Path
from unittest.mock import Mock
import pymupdf
from marketdesk.annual_reports import verified_current_nci,parse_balance,parse_dated_lease_rollforward
F=Path(__file__).parent/'fixtures'
class LeaseFollowup8Tests(unittest.TestCase):
 def setUp(self):
  self.text=(F/'aiil_balance.txt').read_text();self.base=json.loads((F/'aiil_baseline.json').read_text())['latest'];self.words=json.loads((F/'aiil_balance_words.json').read_text());self.page=Mock();self.page.rect=pymupdf.Rect(0,0,595,842);self.page.get_text.return_value=self.words
 def test_current_nci_printed_cell_has_no_inferred_comparative(self):
  proof=verified_current_nci(self.text,self.page,self.base);self.assertEqual(proof['printed_value'],38.02)
  with self.assertRaisesRegex(ValueError,'two matching'):parse_balance(self.text,self.base,allow_existing=True)
  result=parse_balance(self.text,self.base,allow_existing=True,current_nci=proof);self.assertAlmostEqual(result['balance_equity'],14765.86);self.assertEqual(result['matched_fields'],['cash_and_cash_equivalents','equity'])
 def test_nci_wrong_column_or_ambiguous_header_rejected(self):
  for mode in ['prior','extra','missing','wrong_basis']:
   words=copy.deepcopy(self.words);text=self.text
   if mode=='prior':
    for w in words:
     if w[4]=='38.02':w[0]+=85;w[2]+=85
   elif mode=='extra':words+=[w for w in words if w[4]=='2025']
   elif mode=='missing':words=[w for w in words if w[4]!='2025']
   else:text=text.replace('Consolidated Balance Sheet','Standalone Balance Sheet')
   self.page.get_text.return_value=words;self.assertIsNone(verified_current_nci(text,self.page,self.base),mode)
 def test_dated_lease_closing_total_not_maturity(self):
  t=(F/'aiil_lease_note.txt').read_text();e=parse_dated_lease_rollforward(t,self.base);self.assertEqual(e['value'],2.62);self.assertNotIn('3.35',e['table'])
  for bad in [t.replace('Balance as at 31st March','Balance as at 30th March'),t.replace('Balance as at 1 April','Beginning unknown'),t.replace('Consolidated','Standalone'),t.replace('(B) Lease liabilities','(B) Undiscounted Lease liabilities'),t.replace('2.62','-')]:
   with self.assertRaises(ValueError):parse_dated_lease_rollforward(bad,self.base)

 def test_shared_lease_table_dates_are_retained_only_for_that_table(self):
  from marketdesk.annual_reports import parse_lease_note
  t=(F/'nuvama_shared_lease_header.txt').read_text();e=parse_lease_note(t,self.base);self.assertEqual(e['value'],195.58);self.assertIn('March 31, 2026',e['shared_table_header'])
  # Leaving the identical dates in the preceding commitments table is insufficient.
  for bad in [t.replace('C.  Leases','C.  Other commitments'),t.replace('A.  Movement in the Right to use assets','A.  Other financial assets'),t.replace('B.  Movement in the Lease liabilities','D.  Movement in the Lease liabilities')]:
   with self.assertRaises(ValueError):parse_lease_note(bad,self.base)
 def test_auditor_own_audit_clause_requires_auditor_section(self):
  from marketdesk.annual_reports import audited_section_basis
  clause='In connection with our audit of the standalone financial statements'
  header='Auditor’s Responsibility for the Audit of the Financial Statements'
  self.assertEqual(audited_section_basis(header+' '+clause),('standalone',clause))
  self.assertIsNone(audited_section_basis(clause))
  self.assertIsNone(audited_section_basis(header+' We considered other standalone financial statements'))
  self.assertIsNone(audited_section_basis(header+' '+clause+' Report on the audit of the consolidated financial statements'))

 def test_classified_carrying_amount_caption_requires_both_components(self):
  from marketdesk.annual_reports import parse_classified_lease_note
  t=(F/'ptcil_classified_lease_note.txt').read_text();e=parse_classified_lease_note(t,self.base);self.assertAlmostEqual(e['value'],6.3783)
  for bad in [t.replace('637.83','638.83'),t.replace('307.45','-'),t.replace('Carrying amount of lease liability:','Carrying amount of right-of-use assets:'),t.replace('consolidated','standalone')]:
   with self.assertRaises(ValueError):parse_classified_lease_note(bad,self.base)
