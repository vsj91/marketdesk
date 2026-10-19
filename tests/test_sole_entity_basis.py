import unittest
import pymupdf
from marketdesk.annual_reports import sole_entity_basis,balance_text
from marketdesk.growth_repair import checked_pair

HEADING='Independent Auditor’s Report To the Members of Example Limited Report on the Audit of the Financial Statements Opinion We have audited'
CLAUSE='(f) The Company does not have any subsidiary, associate or joint venture. Accordingly, clause 3(xxi) is not applicable.'


class SoleEntityBasisTests(unittest.TestCase):
    def pages(self,*extra):
        return ['Directors report',HEADING,'Key audit matters',CLAUSE,'Balance Sheet as at March 31, 2026',*extra]

    def test_unlabelled_single_set_with_auditor_no_subsidiary_clause(self):
        self.assertEqual(sole_entity_basis(self.pages()),(3,'company does not have any subsidiary, associate or joint venture'))
        lg='we report that the Company did not have any subsidiaries, joint ventures or associate companies during the year.'
        self.assertEqual(sole_entity_basis(['x',HEADING,lg])[0],2)

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


if __name__=='__main__':unittest.main()
