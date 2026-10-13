import copy,hashlib,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from test_fallbacks import fixture
from marketdesk.fallbacks import nse_annual
from marketdesk.conflict_repair import reconcile,retain_resolutions

class ConflictRepairTests(unittest.TestCase):
 def setUp(self):
  self.raw=fixture().encode();self.sha=hashlib.sha256(self.raw).hexdigest();self.row=nse_annual(self.raw.decode(),'TEST','INE000A01010','https://nsearchives.nseindia.com/corporate/ixbrl/test.html')
  self.conflict={'field':'profit_before_tax','upstox':11,'nse':10,'selected':'nse_audited_filing','period':'Mar 2026','units':'crore'}
  self.d={'selected_source':'nse_audited_filing','latest':{**self.row,'raw_sha256':self.sha},'summary':{'revenue_growth_yoy':5},'source_conflicts':[self.conflict]}
  self.e={'symbol':'TEST','isin':'INE000A01010','annual':self.row,'source_url':'https://nsearchives.nseindia.com/corporate/ixbrl/test.html','raw_sha256':self.sha,'evidence_path':'report.html','observed_at':datetime.now(timezone.utc).isoformat()}
 def test_official_verification_retains_both_values_and_never_changes_inputs(self):
  result,records=reconcile(self.d,self.e)
  self.assertEqual(result['source_conflicts'],[]);self.assertEqual(result['latest'],self.d['latest']);self.assertEqual(result['summary'],self.d['summary']);self.assertEqual(records[0]['conflict'],self.conflict)
  self.assertEqual(self.d['source_conflicts'],[self.conflict])
 def test_primary_source_conflict_remains_blocked(self):
  d=copy.deepcopy(self.d);c={'field':'equity','selected_value':50,'annual_report':60,'period':'Mar 2026','units':'crore'};d['source_conflicts'].append(c)
  result,records=reconcile(d,self.e);self.assertEqual(result['source_conflicts'],[c]);self.assertEqual(len(records),1)
 def test_changed_filing_or_context_rejected(self):
  for key,value in [('period','Mar 2025'),('statement_type','standalone'),('units_in','lakh'),('profit_before_tax',10.01),('equity',70)]:
   e=copy.deepcopy(self.e);e['annual'][key]=value
   with self.assertRaises(ValueError):reconcile(self.d,e)
  d=copy.deepcopy(self.d);d['source_conflicts'][0]['nse']=15
  self.assertEqual(reconcile(d,self.e)[1],[])
 def test_reimport_requires_exact_conflict_context_hash_identity_and_freshness(self):
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.conflict_repair.ROOT',Path(tmp)):
   path=Path(tmp)/'report.html';path.write_bytes(self.raw);previous,_=reconcile(self.d,self.e)
   got=retain_resolutions(self.d,previous,'TEST','INE000A01010');self.assertEqual(got['source_conflicts'],[])
   self.assertEqual(got['source_conflict_resolutions'][0]['evidence']['observed_at'],self.e['observed_at'])
   for key,value in [('cash_and_cash_equivalents',6),('period','Mar 2025')]:
    changed=copy.deepcopy(self.d);changed['latest'][key]=value;self.assertTrue(retain_resolutions(changed,previous,'TEST','INE000A01010')['source_conflicts'])
   changed=copy.deepcopy(self.d);changed['source_conflicts'][0]['upstox']=12;self.assertTrue(retain_resolutions(changed,previous,'TEST','INE000A01010')['source_conflicts'])
   self.assertTrue(retain_resolutions(self.d,previous,'WRONG','INE000A01010')['source_conflicts'])
   old=copy.deepcopy(previous);old['source_conflict_resolutions'][0]['evidence']['observed_at']=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat();self.assertTrue(retain_resolutions(self.d,old,'TEST','INE000A01010')['source_conflicts'])
   path.write_bytes(b'tampered');self.assertTrue(retain_resolutions(self.d,previous,'TEST','INE000A01010')['source_conflicts'])
 def test_legacy_row_without_url_uses_verified_index_and_rejects_offsite_url(self):
  import json
  from unittest.mock import Mock
  from marketdesk.conflict_repair import verify_nse
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.conflict_repair.ROOT',Path(tmp)):
   path=Path(tmp)/'data/fundamental-repair/sources/TEST/index.json';path.parent.mkdir(parents=True)
   record={'symbol':'TEST','observed_at':datetime.now(timezone.utc).isoformat(),'payload':[{'symbol':'TEST','qe_Date':'31-MAR-2026','consolidated':'Consolidated','ixbrl':self.e['source_url']}]};path.write_text(json.dumps(record))
   with patch('marketdesk.conflict_repair.request',return_value=Mock(content=self.raw)) as request:
    evidence=verify_nse('TEST','INE000A01010',self.d);self.assertEqual(evidence['raw_sha256'],self.sha);request.assert_called_once()
   record['payload'][0]['ixbrl']='https://untrusted.example/file';path.write_text(json.dumps(record))
   with patch('marketdesk.conflict_repair.request') as request:
    with self.assertRaises(ValueError):verify_nse('TEST','INE000A01010',self.d)
    request.assert_not_called()
