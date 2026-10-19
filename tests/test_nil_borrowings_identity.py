import unittest
from marketdesk.balance_identity import borrowings_absent_identity
from marketdesk.fallbacks import nil_by_financial_liability_identity

W=' '*10
SHEET=f"""LIABILITIES
I.   Non-current liabilities
    Financial liabilities
    (a) Lease liabilities{W}15{W}37.31{W}21.67
    Provisions{W}18 (a){W}856.16{W}569.82
    Deferred tax liabilities (net){W}13 (a){W}-{W}22.83
    Total non-current liabilities{W}893.47{W}614.32
II.  Current liabilities
    Financial liabilities
    (a) Lease liabilities{W}15{W}14.72{W}12.69
    (b) Trade payables{W}16
           -   Total outstanding dues of micro enterprises and small enterprises{W}491.75{W}347.31
           -   Total outstanding dues of creditors other than micro enterprises and{W}3,922.51{W}3,371.94
           small enterprises
     (c) Other financial liabilities{W}17{W}696.74{W}508.01
    Other current liabilities{W}19{W}4,241.74{W}3,105.15
    Provisions{W}18 (b){W}838.52{W}592.66
    Current tax liabilities (net){W}13 (b){W}38.74{W}74.70
    Total current liabilities{W}10,244.72{W}8,012.46
    Total Liabilities{W}11,138.19{W}8,626.78"""


class NilBorrowingsTests(unittest.TestCase):
    def test_exhaustive_sections_prove_nil_borrowings(self):
        proof=borrowings_absent_identity(SHEET,.1)
        self.assertEqual(proof['value'],0.);self.assertEqual(len(proof['current']['lines']),7)

    def test_any_borrowing_caption_or_broken_sum_refuses(self):
        for bad in (SHEET.replace('Other current liabilities','Borrowings'),SHEET.replace('4,241.74','4,241.75'),
                    SHEET.replace('Total current liabilities{W}10,244.72'.format(W=W),'Total financial liabilities{W}5,125.72{W}4,239.95\n    Total current liabilities{W}10,244.72'.format(W=W)),
                    SHEET.replace('    Total non-current liabilities{W}893.47{W}614.32\n'.format(W=W),'')):
            self.assertIsNone(borrowings_absent_identity(bad,.1))

    def test_division_iii_reported_rows_prove_blank_debt_nil(self):
        rows=[['2.1','Financial Liabilities',''],['(a)','Derivative financial instruments',''],['','(i) total outstanding dues of micro enterprises','319.10'],
              ['','(ii) total outstanding dues of creditors','19,500.60'],['(C )','Debt Securities',''],['(d)','Borrowings (Other than Debt Securities)','0.00'],
              ['(f)','Subordinated Liabilities',''],['1','Other financial liabilities','43,431.60'],['','Total of Other financial liabilities','43,431.60'],
              ['','Total Financial Liabilities','63,251.30']]
        self.assertEqual(nil_by_financial_liability_identity(rows,(0,1))['total'],'63251.30')
        self.assertIsNone(nil_by_financial_liability_identity([r if r[1]!='Total Financial Liabilities' else ['', 'Total Financial Liabilities','63,251.31'] for r in rows],(0,1)))


if __name__=='__main__':unittest.main()
