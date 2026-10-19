import json,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch


class CompanyRecordTests(unittest.TestCase):
    def test_caro_clause_states_the_audited_basis(self):
        from marketdesk.annual_reports import audited_section_basis
        text='(xxi) The reporting under clause 3(xxi) of the Order is not applicable in respect of audit of standalone financial statements of the Company.'
        self.assertEqual(audited_section_basis(text)[0],'standalone')
        self.assertIsNone(audited_section_basis('in respect of audit of standalone financial statements of the Company'))

    def test_independent_anchors_are_fresh_identified_and_recent(self):
        from marketdesk import company_record
        now=datetime.now(timezone.utc)
        row={'period':'Mar 2026','period_end':'2026-03-31','currency':'INR','profit_before_tax':2.0792e10,'equity':4.774e10,'cash_and_cash_equivalents':4.42e9}
        with tempfile.TemporaryDirectory() as folder,patch.object(company_record,'ROOT',Path(folder)):
            (Path(folder)/'data/fallbacks').mkdir(parents=True)
            def write(yahoo):(Path(folder)/'data/fallbacks/TEST.json').write_text(json.dumps({'yahoo':yahoo}))
            write({'status':'available','symbol':'TEST.NS','observed_at':now.isoformat(),'annual':[row]})
            a=company_record.yahoo_anchors('TEST');self.assertAlmostEqual(a['values']['profit_before_tax'],2079.2);self.assertEqual(a['period'],'Mar 2026')
            # A stale or unidentified cache is re-fetched; a failed re-fetch refuses.
            write({'status':'available','symbol':None,'observed_at':now.isoformat(),'annual':[row]})
            with patch('marketdesk.fallbacks.Fallbacks.yahoo',return_value={'status':'unavailable'}):
                with self.assertRaises(ValueError):company_record.yahoo_anchors('TEST')
            write({'status':'available','symbol':'TEST.NS','observed_at':now.isoformat(),'annual':[{**row,'period_end':'2024-03-31'}]})
            with self.assertRaisesRegex(ValueError,'stale'):company_record.yahoo_anchors('TEST')

    def test_record_is_retained_only_while_evidence_is_intact_and_provider_is_empty(self):
        from marketdesk import company_record
        import hashlib
        with tempfile.TemporaryDirectory() as folder,patch.object(company_record,'ROOT',Path(folder)):
            (Path(folder)/'ex.pdf').write_bytes(b'evidence')
            previous={'latest':{'period':'Mar 2026'},'summary':{'x':1},'selected_source':'company_annual_report','status':'COMPLETE','missing_fields':[],
                      'report_record':{'report':{'evidence_path':'ex.pdf','excerpt_sha256':hashlib.sha256(b'evidence').hexdigest()}}}
            self.assertEqual(company_record.retain_report_record({'latest':{}},previous,'T','I')['latest'],{'period':'Mar 2026'})
            self.assertEqual(company_record.retain_report_record({'latest':{'period':'Mar 2026','source':'upstox'}},previous,'T','I')['latest']['source'],'upstox')
            (Path(folder)/'ex.pdf').write_bytes(b'changed')
            self.assertEqual(company_record.retain_report_record({'latest':{}},previous,'T','I')['latest'],{})


if __name__=='__main__':unittest.main()
