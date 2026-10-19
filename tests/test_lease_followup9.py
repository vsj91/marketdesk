import copy,json,unittest
from pathlib import Path
from marketdesk.annual_reports import parse_balance,parse_financing_lease_reconciliation
F=Path(__file__).parent/'fixtures'
class LeaseFollowup9Tests(unittest.TestCase):
 def test_nbfc_anchor_matches_all_three_printed_components_and_definition(self):
  t=(F/'sundarmfin_balance.txt').read_text();b=json.loads((F/'sundarmfin_baseline.json').read_text())['latest']
  e=parse_balance(t,b,allow_existing=True);self.assertEqual(e['matched_fields'],['cash_and_cash_equivalents','total_borrowings']);self.assertAlmostEqual(e['nbfc_borrowings_anchor']['result'],60763.26)
  self.assertIsNone(e['balance_equity']);self.assertNotIn('equity',e['values']);self.assertNotIn('total_borrowings',e['values'])
  for before,after in [('2,642.24','-'),('2,642.24','2,640.24'),('Debt securities','Deposits and debt'),('Subordinated liabilities','Other liabilities')]:
   with self.assertRaisesRegex(ValueError,'two matching'):parse_balance(t.replace(before,after),b,allow_existing=True)
  c=copy.deepcopy(b);c['sources']['total_borrowings']['formula']='other debt definition'
  with self.assertRaisesRegex(ValueError,'two matching'):parse_balance(t,c,allow_existing=True)
 def test_financing_reconciliation_closing_cell_keeps_unknown_movement_dashes(self):
  t=(F/'mmfin_financing_lease.txt').read_text();b=json.loads((F/'mmfin_baseline.json').read_text())['latest'];e=parse_financing_lease_reconciliation(t,b)
  self.assertEqual(e['value'],487.29);self.assertEqual(e['tables'][0]['printed_cells'][1],'-');self.assertEqual(e['prior_year_bridge']['current_opening'],503.99)
 def test_financing_reconciliation_rejects_wrong_dates_basis_columns_and_missing_values(self):
  t=(F/'mmfin_financing_lease.txt').read_text();b=json.loads((F/'mmfin_baseline.json').read_text())['latest']
  for bad in [t.replace('487.29','-'),t.replace('Consolidated','Standalone'),t.replace('March 31','March 30'),t.replace('Business','Other'),t.replace('Year ended March 31, 2025','Year ended March 31, 2024'),t.replace('123.70      503.99','123.70      504.99'),t.replace('Lease liabilities','Undiscounted lease payments'),t.replace('` in crore','USD in crore')]:
   with self.assertRaises(ValueError,msg=bad[:50]):parse_financing_lease_reconciliation(bad,b)
 def test_unclassified_note_requires_its_own_context_and_both_parent_totals(self):
  from marketdesk.annual_reports import parse_unclassified_lease_note,matching_unclassified_note
  t=(F/'sundarmfin_ofl_note.txt').read_text();balance=(F/'sundarmfin_balance.txt').read_text();b=json.loads((F/'sundarmfin_baseline.json').read_text())['latest'];e=parse_unclassified_lease_note(t,b)
  self.assertEqual(e['value'],154.24);self.assertTrue(matching_unclassified_note(e,balance,1));self.assertFalse(matching_unclassified_note(e,balance,.01))
  c=copy.deepcopy(b);c['current_lease_liabilities']=200
  with self.assertRaises(ValueError):parse_unclassified_lease_note(t,c)
  for bad in [balance.replace('720.98','721.98'),balance.replace('603.20','603.30'),balance.replace('Other financial liabilities','Other liabilities'),'Current liabilities\n'+balance]:
   self.assertFalse(matching_unclassified_note(e,bad,1))
  for bad in [t.replace('154.24','-'),t.replace('720.98','720.00'),t.replace('Consolidated','Standalone').replace('CONSOLIDATED','STANDALONE'),t.replace('Note 23: Other Financial Liabilities','Note 23: Other Financial Liabilities - current'),t.replace('31st March 2026','31st March 2025')]:
   with self.assertRaises(ValueError):parse_unclassified_lease_note(bad,b)
 def test_lettered_equity_subtotal_includes_printed_equity_instruments(self):
  from marketdesk.annual_reports import explicit_equity_section_subtotal
  t=(F/'hudco_equity_subtotal.txt').read_text();e=explicit_equity_section_subtotal(t,'standalone',1)
  self.assertEqual(e['value'],21977.20);self.assertEqual([r['printed_value'] for r in e['components']],[2001.90,1442.00,18533.30])
  for bad in [t.replace('Sub Total (B)','Sub Total (C)'),t.replace('21,977.20','21,970.20'),t.replace('1,442.00','-'),t.replace('Instruments entirely equity in nature','Other liabilities')]:
   self.assertIsNone(explicit_equity_section_subtotal(bad,'standalone',1))
  self.assertIsNone(explicit_equity_section_subtotal(t,'consolidated',1))
