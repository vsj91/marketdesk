import copy,json,unittest
from pathlib import Path
from unittest.mock import Mock
import pymupdf
from marketdesk.annual_reports import active_financial_navigation,parse_numbered_lease_disclosure
F=Path(__file__).parent/'fixtures'
class NumberedLeaseDisclosureTests(unittest.TestCase):
 def setUp(self):
  self.text=(F/'tatainvest_active_lease.txt').read_text();self.base=json.loads((F/'tatainvest_baseline.json').read_text())['latest'];self.nav=json.loads((F/'tatainvest_navigation.json').read_text())
 def page(self,data):
  p=Mock();p.rect=pymupdf.Rect(data['rect']);p.get_drawings.return_value=[{**d,'rect':pymupdf.Rect(d['rect'])} for d in data['drawings']]
  p.get_text.side_effect=lambda *args,**kw: {'blocks':[{'lines':[{'spans':data['spans'],'dir':(1.,0.),'bbox':[0,75,560,86]}]}]} if args and args[0]=='dict' else self.text.split('\n',1)[1]
  return p
 def test_selected_basis_requires_unambiguous_white_text_on_colored_tab(self):
  text,proof=active_financial_navigation(self.page(self.nav));self.assertTrue(text.startswith('Consolidated Financial Statements\n'));self.assertIn('Standalone Financial Statements',proof['inactive_titles']);self.assertEqual(proof['method'],'printed_active_navigation_tab')
  for mode in ['no_white','two_white','no_fill','misplaced_fill','missing_tab']:
   a=copy.deepcopy(self.nav)
   if mode=='no_white':a['spans'][-1]['color']=0
   if mode=='two_white':a['spans'][-2]['color']=0xffffff
   if mode=='no_fill':a['drawings']=[]
   if mode=='misplaced_fill':
    for d in a['drawings']:d['rect']=[0,0,10,10]
   if mode=='missing_tab':a['spans']=a['spans'][1:]
   self.assertIsNone(active_financial_navigation(self.page(a)),mode)
 def test_own_lease_header_and_dotted_closing_row(self):
  e=parse_numbered_lease_disclosure(self.text,self.base);self.assertEqual(e['value'],1.3978);self.assertIn('139.78',e['line']);self.assertNotIn('151.94',e['line']);self.assertIn('31.03.2026',e['own_table_header'])
 def test_no_basis_inference_or_maturity_substitution(self):
  for bad in [self.text.replace('Consolidated Financial Statements','Standalone Financial Statements'),self.text.replace('139.78','-'),self.text.replace('13.   Disclosures for leasing arrangements','13. Other disclosures'),self.text.replace('Amount recognised during the year','Other disclosures'),self.text.replace('31.03.2026','31.03.2025'),self.text.replace('Balance at the end of the year','Undiscounted maturities'),self.text.replace('(` in lacs)','(USD in lacs)'),self.text.replace('Consolidated Financial Statements','Consolidated Financial Statements\nStandalone Financial Statements')]:
   with self.assertRaises(ValueError):parse_numbered_lease_disclosure(bad,self.base)
 def test_contingency_dates_cannot_replace_own_lease_table_dates(self):
  start=self.text.index('(b)   Amount recognised during the year');end=self.text.index('(c)   The movement',start)
  bad=self.text[:start]+self.text[start:end].replace('31.03.2026','unknown').replace('31.03.2025','unknown')+self.text[end:]
  with self.assertRaises(ValueError):parse_numbered_lease_disclosure(bad,self.base)
 def test_conflicting_body_basis_cannot_be_discarded_with_prefix(self):
  bad=self.text.replace('(A)   Company as a Lessee','(A)   Company as a Lessee\nStandalone Financial Statements')
  self.assertNotEqual(bad,self.text)
  with self.assertRaisesRegex(ValueError,'Mixed statement basis'):parse_numbered_lease_disclosure(bad,self.base)
