import copy,unittest
from marketdesk.balance_identity import identity_evidence,borrowings_reconciliation

TEXT='''Total equity attributable to owners of the Company    8,098.96    7,389.13
Non-current liabilities
(a) Financial liabilities
(i) Lease liabilities    14    99.91    81.36
(ii) Other financial liabilities    18    5.06    -
(b) Provisions    15    297.45    210.62
(c) Deferred tax liabilities (net)    16    94.35    74.26
(d) Other non-current liabilities    20    98.55    99.59
Total non-current liabilities    595.32    465.83
Current liabilities
(i) Borrowings    13    276.46    144.57
Total current liabilities    2,762.44    2,313.33
Total equity and liabilities    11,456.72    10,168.29
'''
class IdentityTests(unittest.TestCase):
 def test_exact_identities_and_printed_arithmetic(self):
  r=identity_evidence(TEXT,1)
  self.assertEqual(r['nci']['total_equity_crore'],8098.96)
  self.assertEqual(r['non_current_borrowings']['value'],0)
  self.assertEqual(len(r['non_current_borrowings']['operands']),5)
 def test_missing_dash_blank_or_non_tying_is_never_zero(self):
  for bad in (TEXT.replace('5.06','-'),TEXT.replace('5.06',' '),TEXT.replace('5.06','5.07'),TEXT.replace('Other financial liabilities','Borrowings'),TEXT.replace('Total non-current liabilities','Non-current subtotal not identified')):
   self.assertNotIn('non_current_borrowings',identity_evidence(bad,1))
  for bad in (TEXT.replace('11,456.72','11,456.73'),TEXT.replace('11,456.72','-'),TEXT.replace('Current liabilities\n','Non-controlling interests    -    -\nCurrent liabilities\n')):
   self.assertNotIn('nci',identity_evidence(bad,1))
 def test_only_authorized_fields_and_decimal_scaling(self):
  r=identity_evidence(TEXT,.01);self.assertEqual(set(r),{'nci','non_current_borrowings'})
  self.assertAlmostEqual(r['nci']['total_equity_crore'],80.9896)
 def test_reconciliation_uses_printed_rounding_and_preserves_vendor(self):
  lines={'b':'Borrowings    46,016.36    36,382.92','l':'Lease liabilities    202.37    185.40'}
  r=borrowings_reconciliation(63935.700000000004,63649.8,285.9,lines,1)
  self.assertEqual(r['reason'],'vendor figure includes leases')
  self.assertEqual(r['rounding_bound_crore'],'0.005')
  for vendor in (63935.71,63649.8,64000):self.assertIsNone(borrowings_reconciliation(vendor,63649.8,285.9,lines,1))
  lines['nil']='Lease liabilities    Nil    Nil'
  self.assertIsNone(borrowings_reconciliation(63935.71,63649.8,285.9,lines,1))
