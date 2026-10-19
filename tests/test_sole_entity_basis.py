import unittest
import pymupdf
from marketdesk.annual_reports import sole_entity_basis,balance_text
from marketdesk.growth_repair import checked_pair

HEADING='Independent Auditor’s Report To the Members of Example Limited Report on the Audit of the Financial Statements Opinion We have audited the accompanying financial statements'
CLAUSE='(f) The Company does not have any subsidiary, associate or joint venture. Accordingly, clause 3(xxi) is not applicable.'


class SoleEntityBasisTests(unittest.TestCase):
    def pages(self,*extra):
        return ['Directors report',HEADING,'Key audit matters',CLAUSE,'Balance Sheet as at March 31, 2026',*extra]

    def test_unlabelled_single_set_with_auditor_no_subsidiary_clause(self):
        self.assertEqual(sole_entity_basis(self.pages()),(3,'company does not have any subsidiary, associate or joint venture'))
        lg='we report that the Company did not have any subsidiaries, joint ventures or associate companies during the year.'
        self.assertEqual(sole_entity_basis(['x',HEADING,lg])[0],2)
        for clause in ('(e) the Company has no subsidiaries, associate and joint ventures, hence reporting under clause 3(ix)(e) is not applicable.',
                       '(e) The Company does not hold any investment in any subsidiaries, associates or joint ventures (as defined under the Act).'):
            self.assertEqual(sole_entity_basis(['x',HEADING,clause])[0],2,clause)
        self.assertIsNone(sole_entity_basis(['x',HEADING,'The Company has subsidiaries, associates and joint ventures.']))

    def test_auditor_title_variants_and_contents_pages(self):
        clause=CLAUSE
        title='Independent Auditors’ Report on the Financial Statements To the members Opinion We have audited the financial statements of X'
        self.assertEqual(sole_entity_basis(['x',title,clause])[0],2)
        # Control characters used as spaces in a text layer.
        spaced='Report\x03on\x03the\x03Audit\x03of\x03the\x03Financial\x03Statements Opinion We\x03have\x03audited\x03the\x03accompanying\x03financial\x03statements'
        self.assertEqual(sole_entity_basis(['x',spaced,clause])[0],2)
        # A contents page citing the report is not the auditor's report.
        contents='Financial statements 156 Independent Auditors’ Report on the Financial Statements 168 Balance Sheet'
        self.assertIsNone(sole_entity_basis([contents,clause]+['notes']*30))

    def test_rejects_any_consolidated_or_labelled_audit(self):
        for extra in ('Report on the Audit of the Consolidated Financial Statements','Report on the audit of the Standalone Financial Statements',
                      'Consolidated Balance Sheet as at March 31, 2026','Consolidated Statement of Profit and Loss for the year'):
            self.assertIsNone(sole_entity_basis(self.pages(extra)),extra)

    def test_requires_auditor_heading_and_full_clause(self):
        self.assertIsNone(sole_entity_basis(['x','Directors report: '+CLAUSE,'Balance Sheet']))
        # A company with an associate or joint venture still consolidates them.
        partial=self.pages();partial[3]='The Company does not have any subsidiary. Its joint venture is equity accounted.'
        self.assertIsNone(sole_entity_basis(partial))
        # The clause must be in the auditor's report, not a board report before it.
        self.assertIsNone(sole_entity_basis([CLAUSE,HEADING,'Balance Sheet']))
        far=[HEADING]+['notes']*21+[CLAUSE]
        self.assertIsNone(sole_entity_basis(far))


class RoundingQuantumTests(unittest.TestCase):
    def setUp(self):
        self.details={'latest':{'period':'Mar 2026','statement_type':'standalone','units_in':'crore','profit_before_tax':821.88,'finance_costs':26.25},'summary':{}}
        base={'statement_type':'standalone','units_in':'crore','observed_at':'2026-10-19T00:00:00+00:00'}
        self.current={**base,'period':'Mar 2026','period_end':'2026-03-31','revenue':11477.6,'net_profit':625.2,'profit_before_tax':821.9,'finance_costs':26.3}
        self.prior={**base,'period':'Mar 2025','period_end':'2025-03-31','revenue':9319.4,'net_profit':605.9}

    def test_printed_million_rounding_matches_half_step_only(self):
        with self.assertRaisesRegex(ValueError,'anchors'):checked_pair(self.details,self.current,self.prior)
        current={**self.current,'rounding_quanta':{'profit_before_tax':.1,'finance_costs':.1}}
        self.assertAlmostEqual(checked_pair(self.details,current,self.prior)['net_profit_growth_yoy'],(625.2/605.9-1)*100)
        with self.assertRaisesRegex(ValueError,'anchors'):checked_pair(self.details,{**current,'profit_before_tax':821.95},self.prior)


class MarginIconTests(unittest.TestCase):
    def test_margin_icon_does_not_keep_navigation_rail_in_table_text(self):
        doc=pymupdf.open();page=doc.new_page(width=594,height=774)
        page.insert_text((60,270),'Finance costs');page.insert_text((500,270),'9.29')
        # The rotated rail overlaps the row, so an uncropped page joins it to 9.29.
        page.insert_text((570,250),'Financial Statements',rotate=270)
        page.insert_text((566,30),'Q')
        row=[line for line in balance_text(page).splitlines() if 'Finance costs' in line][0]
        self.assertRegex(row,r'9\.29$')
        # A number in the margin is never treated as an icon.
        page.insert_text((566,60),'12')
        row=[line for line in balance_text(page).splitlines() if 'Finance costs' in line][0]
        self.assertRegex(row,'Financial$')


W=' '*10
RITES_TEXT=f"""CONSOLIDATED BALANCE SHEET
as at 31st March, 2026
(₹ in crore)
Particulars{W}Note No.{W}As at 31.03.2026{W}As at 31.03.2025
Assets
Cash and Cash Equivalents-Owned Fund{W}15.1{W}96.03{W}65.59
Cash and Cash Equivalents-Clients Fund{W}16.1{W}460.98{W}622.51
Equity
Equity Share Capital{W}21{W}480.60{W}480.60
Other Equity{W}22{W}2,200.64{W}2,159.55
Non-Controlling Interests{W}22.4{W}112.79{W}109.11
{W}2,794.03{W}2,749.26
Liabilities
Non Current Liabilities
Lease Liabilities{W}56(a){W}4.90{W}6.59
Current Liabilities
Lease Liabilities{W}56(a){W}1.69{W}2.44
Total Equity and Liabilities{W}5,925.94{W}6,119.20
The accompanying notes form an integral part of standalone financial statements"""


class BalanceRefinementTests(unittest.TestCase):
    latest={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','cash_and_cash_equivalents':557.01,'equity':2794.03,'total_borrowings':0.0}

    def test_captioned_cash_parts_anchor_only_as_their_exact_sum(self):
        from marketdesk.annual_reports import parse_balance
        values=parse_balance(RITES_TEXT,self.latest,allow_existing=True)['values']
        self.assertEqual(values['total_lease_liabilities'],6.59);self.assertAlmostEqual(values['cash_and_cash_equivalents'],557.01)
        # One part alone, a repeated part, or a different sum is not an anchor.
        for bad in (RITES_TEXT.replace('Cash and Cash Equivalents-Clients Fund','Other bank balances'),
                    RITES_TEXT.replace('Clients Fund','Owned Fund'),RITES_TEXT.replace('460.98','461.98')):
            with self.assertRaisesRegex(ValueError,'two matching'):parse_balance(bad,self.latest,allow_existing=True)

    def test_basis_after_closing_total_is_not_heading(self):
        from marketdesk.annual_reports import statement_context
        self.assertEqual(statement_context(RITES_TEXT,self.latest)[1],'consolidated')
        # Before the closing total, a contradicting basis is still rejected.
        mixed=RITES_TEXT.replace('Assets\n','Assets\nStandalone\n',1)
        with self.assertRaisesRegex(ValueError,'Mixed'):statement_context(mixed,self.latest)


class IncomeMarkerTests(unittest.TestCase):
    def test_single_letter_row_marker_on_profit(self):
        from marketdesk.report_growth import parse_growth
        latest={'period':'Mar 2026','statement_type':'standalone','units_in':'crore','revenue':3.41,'net_profit':212.07,'profit_before_tax':463.3,'finance_costs':0.78}
        text=f"""standalone
Statement of Profit and Loss for the year ended March 31, 2026{W}(₹ in Crore)
Particulars{W}Note{W}Year Ended{W}Year Ended
{W}No.{W}March 31, 2026{W}March 31, 2025
Revenue From Operations{W}23{W}3.41{W}2.69
Finance Costs{W}29{W}0.78{W}0.88
Profit / (Loss) Before Tax{W}463.30{W}97.20
Profit / (Loss) for the year (A){W}212.07{W}69.53"""
        parsed=parse_growth(text,latest);self.assertEqual(parsed['prior']['net_profit'],69.53)
        with self.assertRaisesRegex(ValueError,'rows missing'):parse_growth(text.replace('(A)','(AB)'),latest)


OFL_NOTE=f"""{W}As at{W}As at
Particulars{W}March 31, 2026{W}March 31, 2025
19. Other financial liabilities
Unpaid dividends{W}7.03{W}-
Lease Liability (Refer Note: 38){W}6,013.39{W}5,724.38
Employee related payables{W}4,410.63{W}3,657.00
Others{W}155.43{W}82.58
Total{W}10,586.48{W}9,463.96
20. Provisions
Provision for gratuity{W}1,422.79{W}1,724.74"""
BALANCE_ROW=f"Financial Liabilities\nDebt securities{W}17{W}78,323.23{W}78,178.40\nOther financial liabilities{W}19{W}10,586.48{W}9,463.96\n"


class BoundOflLeaseTests(unittest.TestCase):
    def test_lease_row_binds_only_to_identical_unclassified_balance_row(self):
        from marketdesk.annual_reports import parse_numbered_ofl_note,bound_ofl_lease
        note=parse_numbered_ofl_note(OFL_NOTE);self.assertEqual(note['printed_value'],6013.39)
        self.assertAlmostEqual(bound_ofl_lease(note,BALANCE_ROW,.01)['value'],60.1339)
        for bad in (BALANCE_ROW.replace('10,586.48','10,586.49'),BALANCE_ROW.replace('19','21',1).replace('liabilities{W}19'.format(W=W),'liabilities{W}21'.format(W=W)),
                    'Current Liabilities\n'+BALANCE_ROW):
            self.assertIsNone(bound_ofl_lease(note,bad,.01))

    def test_note_must_sum_and_not_be_maturity_classified(self):
        from marketdesk.annual_reports import parse_numbered_ofl_note
        for bad in (OFL_NOTE.replace('155.43','156.43'),OFL_NOTE.replace('Others','Payable within twelve months current'),
                    OFL_NOTE.replace('6,013.39','-'),OFL_NOTE.replace('Lease Liability (Refer Note: 38)','Security deposits')):
            with self.assertRaises(ValueError):parse_numbered_ofl_note(bad)

    def test_grouped_twelve_month_rows_and_wrapped_unlabelled_total(self):
        from marketdesk.annual_reports import parse_numbered_ofl_note
        grouped=f"""19 OTHER FINANCIAL LIABILITIES
{W}As at{W}As at Particulars{W}March 31, 2026{W}March 31, 2025
 Payable within twelve month after reporting period
 Unpaid dividends{W}0.21{W}0.32
 Lease liability*{W}61.28{W}66.00
 Payable after twelve months after reporting date
 Lease liability*{W}105.26{W}158.07
 Total{W}166.75{W}224.39"""
        self.assertAlmostEqual(parse_numbered_ofl_note(grouped)['printed_value'],166.54)
        with self.assertRaises(ValueError):parse_numbered_ofl_note(grouped.replace(' Payable after twelve months after reporting date\n',''))
        wrapped=f"""17 Other financial liabilities{W}31 March 2026{W}31 March 2025
 Lease liabilities (Refer note 42){W}374.47{W}292.28
 Amounts payable under assignment of receivables (collections made on behalf of
 assignee){W}326.11{W}278.77
 Employee benefits payable{W}206.40{W}146.72
{W}906.98{W}717.77"""
        note=parse_numbered_ofl_note(wrapped);self.assertEqual((note['printed_value'],note['printed_totals']),(374.47,[906.98,717.77]))
        # Two unexplained text lines in a row are never stitched into one caption.
        with self.assertRaises(ValueError):parse_numbered_ofl_note(wrapped.replace(' assignee)',' extra words\n assignee)'))


if __name__=='__main__':unittest.main()
