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


OLECTRA=f"""Equity and Liabilities
Non-current liabilities
Financial Liabilities
Borrowings{W}17{W}17,041.50{W}14,169.66
Provisions{W}18{W}4,581.73{W}4,000.81
Deferred tax liabilities, net{W}9{W}1,697.35{W}1,348.05
23,320.58{W}19,518.52
Current liabilities
Financial Liabilities
Borrowings{W}17{W}20,987.88{W}11,315.13
Other financial liabilities{W}20{W}6,864.78{W}9,110.07
Provisions{W}18{W}104.97{W}41.16
27,957.63{W}20,466.36
Total liabilities{W}53,278.21{W}39,984.88"""


class NilLeaseTests(unittest.TestCase):
    def test_unlabelled_section_totals_and_no_lease_line(self):
        from marketdesk.balance_identity import lease_absent_identity
        self.assertEqual(lease_absent_identity(OLECTRA,.01)['value'],0.)
        self.assertIsNone(borrowings_absent_identity(OLECTRA,.01))
        for bad in (OLECTRA.replace('Provisions{W}18{W}104.97'.format(W=W),'Lease liabilities{W}18{W}104.97'.format(W=W)),OLECTRA.replace('6,864.78','6,864.79')):
            self.assertIsNone(lease_absent_identity(bad,.01))


ROLL=f"""Notes to the Consolidated Financial Statements
(₹ in million)
 ii)   The following is the carrying value of lease liability :
           Particulars{W}31-March-2026{W}31-March-2025
         Opening Balance{W}136{W}187
           Additions during the year
         Finance cost accrued during the year{W}14{W}15
          Payment of lease liabilities during the year{W}(29){W}(66)
          Closing Balance{W}121{W}136"""


class LeaseRollforwardTests(unittest.TestCase):
    latest={'period':'Mar 2026','statement_type':'consolidated'}

    def test_reconciled_rollforward_with_unit_dates_and_basis(self):
        from marketdesk.annual_reports import parse_lease_rollforward
        r=parse_lease_rollforward(ROLL,self.latest)
        self.assertAlmostEqual(r['value'],12.1);self.assertEqual(r['sign_reading'],'printed_signs')
        unsigned=ROLL.replace('(29)','29').replace('(66)','66')
        self.assertEqual(parse_lease_rollforward(unsigned,self.latest)['sign_reading'],'unsigned_outflows_subtracted')

    def test_refuses_broken_arithmetic_wrong_basis_or_missing_unit(self):
        from marketdesk.annual_reports import parse_lease_rollforward
        for text,latest in ((ROLL.replace('Closing Balance{W}121'.format(W=W),'Closing Balance{W}122'.format(W=W)),self.latest),
                            (ROLL,{'period':'Mar 2026','statement_type':'standalone'}),(ROLL.replace('(₹ in million)',''),self.latest),
                            (ROLL.replace('31-March-2025','31-March-2024'),self.latest),(ROLL.replace('Opening Balance{W}136'.format(W=W),'Opening Balance{W}135'.format(W=W)),self.latest)):
            with self.assertRaises(ValueError):parse_lease_rollforward(text,latest)


if __name__=='__main__':unittest.main()


class FilingIdentityTests(unittest.TestCase):
    def test_same_security_after_rename_or_isin_reissue_only(self):
        from marketdesk.fallbacks import filing_identity
        self.assertEqual(filing_identity('TDPOWERSYS','INE419M01027','TDPOWERSYS','INE419M01035'),'isin_reissued')
        self.assertEqual(filing_identity('HEG','INE545A01024','HEGAM','INE545A01024'),'symbol_renamed')
        for args in (('OTHER','INE419M01027','TDPOWERSYS','INE419M01035'),('TDPOWERSYS','INE419N01027','TDPOWERSYS','INE419M01035'),
                     ('TDPOWERSYS','INE419M02027','TDPOWERSYS','INE419M01035'),('HEG','INE545A01032','HEGAM','INE545A01024')):
            self.assertIsNone(filing_identity(*args),args)


class CreditaccLayoutTests(unittest.TestCase):
    def test_classified_lease_table_with_dated_heading(self):
        from marketdesk.annual_reports import parse_classified_lease_note
        text=f"""Notes to the Consolidated Financial Statements
for the year ended March 31, 2026
36.2 Total lease liabilities are analysed as follows:
{W}₹ in crore
     Lease liabilities{W}March 31, 2026{W}March 31, 2025
     Current{W}28.48{W}24.48
     Non-current{W}80.37{W}83.22
     Total{W}108.85{W}107.70"""
        latest={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore'}
        self.assertAlmostEqual(parse_classified_lease_note(text,latest)['value'],108.85)
        with self.assertRaises(ValueError):parse_classified_lease_note(text.replace('108.85','108.95'),latest)

    def test_nbfc_dash_matches_only_an_audited_explicit_zero(self):
        from marketdesk.annual_reports import parse_balance
        text=f"""Consolidated Balance Sheet as at March 31, 2026
₹ in crore
Particulars{W}Note{W}March 31, 2026{W}March 31, 2025
Cash and cash equivalents{W}4{W}1,151.58{W}1,271.49
Debt securities{W}13{W}912.25{W}1,541.75
Borrowings (other than debt securities){W}14{W}22,728.88{W}18,878.73
Subordinated liabilities{W}15{W}-{W}25.29
Total liabilities and equity{W}31,930.23{W}27,802.45"""
        latest={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','cash_and_cash_equivalents':1151.58,'equity':7842.25,
                'total_borrowings':23641.13,'debt_securities':912.25,'other_borrowings':22728.88,'subordinated_liabilities':0.0,
                'sources':{'total_borrowings':{'formula':'debt_securities + other_borrowings + subordinated_liabilities'}}}
        self.assertEqual(parse_balance(text,latest,allow_existing=True)['matched_fields'],['cash_and_cash_equivalents','total_borrowings'])
        with self.assertRaisesRegex(ValueError,'two matching'):parse_balance(text,{**latest,'subordinated_liabilities':0.5,'total_borrowings':23641.63},allow_existing=True)


class NumberedSectionTests(unittest.TestCase):
    def test_numbered_section_headings(self):
        text=f"""(3)  Non-current liabilities
(a)  Financial liabilities
(i)  Other non-current financial liabilities{W}17{W}68.76{W}58.02
(ii)  Lease liabilities{W}36{W}3.83{W}0.31
(b)  Deferred tax liabilities (net){W}33{W}38.70{W}26.27
(c)  Non-current provisions{W}18{W}4.37{W}3.49
Total non-current liabilities{W}115.66{W}88.09
(4)  Current liabilities
(ii)  Lease liabilities{W}36{W}0.93{W}0.70
(iii)  Other current financial liabilities{W}20{W}2,459.31{W}1,180.78
(d)  Income tax liabilities (net){W}23{W}61.20{W}-
Total current liabilities{W}2,521.44{W}1,181.48"""
        self.assertEqual(borrowings_absent_identity(text,1.)['value'],0.)
