import copy,unittest,tempfile,hashlib
from pathlib import Path
from unittest.mock import patch
from marketdesk.annual_conflicts import bridge_components,retain_annual_resolutions,checked_bytes,reviewed_rows
from marketdesk.quality import financial_reasons
from datetime import datetime,timezone

class AnnualConflictTests(unittest.TestCase):
 def setUp(self):
  self.latest={'current_borrowings':12,'non_current_borrowings':23,'total_borrowings':35,'total_lease_liabilities':5}
  self.parsed={'conversion_to_crore':1,'borrowing_components':{'current_borrowings':10,'non_current_borrowings':20},'components':{'current_lease':2,'non_current_lease':3},'lines':{'current_borrowings':'Borrowings    10.00    9.00','non_current_borrowings':'Borrowings    20.00    19.00','current_lease':'Lease liabilities    2.00    1.00','non_current_lease':'Lease liabilities    3.00    2.00'}}
  self.nse={**self.latest,'sources':{k:{'rounding_quantum':.01} for k in ('current_borrowings','non_current_borrowings')}}
 def test_separate_components_prevent_double_counting(self):
  changes,bridge=bridge_components(self.latest,self.parsed,self.nse)
  self.assertEqual(changes['total_borrowings'],30);self.assertEqual(changes['total_borrowings']+self.latest['total_lease_liabilities'],35)
  self.assertEqual([r['classification'] for r in bridge],['includes leases','includes leases'])
 def test_current_only_inclusion_is_not_blanket_subtraction(self):
  latest={**self.latest,'non_current_borrowings':20,'total_borrowings':32};nse={**self.nse,**latest}
  changes,bridge=bridge_components(latest,self.parsed,nse)
  self.assertEqual(changes['total_borrowings'],30);self.assertEqual(bridge[1]['classification'],'excludes leases')
 def test_real_disagreement_missing_components_and_changed_lease_rejected(self):
  for key,value in [('current_borrowings',13),('non_current_borrowings',None),('total_borrowings',34),('total_lease_liabilities',6)]:
   latest={**self.latest,key:value}
   with self.assertRaises(ValueError):bridge_components(latest,self.parsed,{**self.nse,**latest})
 def test_unchanged_debt_is_not_normalized(self):
  latest={**self.latest,'current_borrowings':10,'non_current_borrowings':20,'total_borrowings':30}
  with self.assertRaises(ValueError):bridge_components(latest,self.parsed,{**self.nse,**latest})
 def test_unreviewed_rounding_not_relaxed(self):
  p=copy.deepcopy(self.parsed);p['conversion_to_crore']=.01
  for k in p['lines']:p['lines'][k]='Amount    100.00    90.00'
  latest={**self.latest,'current_borrowings':12.003,'total_borrowings':35.003};nse={**self.nse,**latest,'sources':{k:{'rounding_quantum':.0001} for k in ('current_borrowings','non_current_borrowings')}}
  with self.assertRaises(ValueError):bridge_components(latest,p,nse)
  with self.assertRaises(ValueError):bridge_components(latest,p,nse,{'quantum_crore':.01})
 def test_pdf_integrity_and_duplicate_rows(self):
  with tempfile.TemporaryDirectory() as tmp,patch('marketdesk.annual_conflicts.ROOT',Path(tmp)):
   p=Path(tmp)/'report.pdf';p.write_bytes(b'original');sha=hashlib.sha256(p.read_bytes()).hexdigest();self.assertEqual(checked_bytes('report.pdf',sha),b'original');p.write_bytes(b'changed')
   with self.assertRaises(ValueError):checked_bytes('report.pdf',sha)
  line='Total Equity    100.00    90.00';review={'lines':{'equity':line}}
  self.assertEqual(reviewed_rows(line,review,.01),{'equity':1})
  with self.assertRaises(ValueError):reviewed_rows(line+'\n'+line,review,.01)
 def test_changed_context_fails_closed_on_refresh(self):
  previous={'annual_conflict_resolutions':[{'symbol':'TEST','isin':'ISIN','selected_context':{'period':'old'}}]}
  result=retain_annual_resolutions({'latest':{'period':'new'}},previous,'TEST','ISIN')
  self.assertTrue(result['annual_conflict_recheck_required'])
  self.assertIn('Annual-report correction requires re-verification',financial_reasons(result,None,datetime.now(timezone.utc)))
