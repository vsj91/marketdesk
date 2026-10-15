import copy,unittest
from datetime import datetime,timezone
from unittest.mock import patch
from contextlib import ExitStack
from marketdesk.liability_reviews import verify_liability_review,reconcile_liability_review
from marketdesk.annual_conflicts import retain_annual_resolutions

class LiabilityReviewTests(unittest.TestCase):
 def setup_case(self,method='debt_with_trade_credit'):
  latest={'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','raw_sha256':'nse','equity':50,'cash_and_cash_equivalents':5,'profit_before_tax':10,'finance_costs':1,'current_borrowings':19,'non_current_borrowings':23,'total_borrowings':42,'total_lease_liabilities':5}
  parsed={'matched_fields':['equity','cash_and_cash_equivalents'],'conversion_to_crore':1,'borrowing_components':{'current_borrowings':10,'non_current_borrowings':20},'components':{'current_lease':2,'non_current_lease':3},'values':{'total_borrowings':30,'total_lease_liabilities':5}}
  line='(iii)  Trade Credits & Acceptances    30    7.00    6.00';text='  II  CURRENT LIABILITIES\n'+line
  field='total_borrowings';raw=b'placeholder';nse=copy.deepcopy(latest)
  if method=='mislabelled_lease_component':
   latest.update(current_borrowings=10,non_current_borrowings=None,total_borrowings=30,total_lease_liabilities=6)
   nse={**latest,'non_current_lease_liabilities':4,'current_lease_liabilities':2,'sources':{'equity':{'conversion_to_crore':1}}}
   line='(iii) Other financial liabilities    29    4.00    3.00';text='  (2) Non-current liabilities\n'+line;field='total_lease_liabilities'
   rows=[['','Total equity','50'],['','Cash and cash equivalents','5'],['','Non-current liabilities',''],['','Borrowings, non-current','20'],['1','Borrowings, non-current','3'],['2','Lease Liabilities','4'],['','Total of other non-current financial liabilities','7'],['','Current liabilities','']]
   raw=('<table>'+''.join('<tr>'+''.join('<td>'+c+'</td>' for c in r)+'</tr>' for r in rows)+'</table>').encode()
  proof={'source_url':'https://primary/report.pdf','liability_review':{'method':method,'extra_line':line,'nse':{'observed_at':datetime.now(timezone.utc).isoformat(),'source_url':'https://primary/filing.html','raw_sha256':'nse','evidence_path':'filing.html'}}}
  proof.update(observed_at=datetime.now(timezone.utc).isoformat(),raw_sha256='pdf',evidence_path='report.pdf',excerpt_sha256='excerpt')
  conflict={'field':field,'selected':'nse_audited_filing','period':'Mar 2026','units':'crore','source_url':proof['source_url'],'selected_value':latest[field],'annual_report':parsed['values'][field]}
  details={'selected_source':'nse_audited_filing','latest':latest,'summary':{'unchanged':True},'source_conflicts':[conflict]}
  return details,proof,parsed,nse,text,raw
 def run_case(self,case,apply=False):
  d,p,parsed,nse,text,raw=case
  with ExitStack() as stack:
   for name,value in [('checked_bytes',raw),('nse_annual',nse),('report_text',text),('parse_balance',parsed)]:stack.enter_context(patch('marketdesk.liability_reviews.'+name,return_value=value))
   return (reconcile_liability_review if apply else verify_liability_review)(d,p,'TEST','ISIN')
 def test_trade_credit_is_retained_and_only_leases_removed(self):
  case=self.setup_case();changes,_,_=self.run_case(case)
  self.assertEqual(changes['current_borrowings'],17);self.assertEqual(changes['total_borrowings'],37)
  self.assertEqual(changes['total_borrowings']+case[0]['latest']['total_lease_liabilities'],42)
 def test_incomplete_or_mismatching_classifications_fail_closed(self):
  for change in ('credit','section','lease','nse','context','conflict'):
   c=list(self.setup_case())
   if change=='credit':c[1]['liability_review']['extra_line']=c[1]['liability_review']['extra_line'].replace('Trade Credits & Acceptances','Trade Payables');c[4]=c[4].replace('Trade Credits & Acceptances','Trade Payables')
   if change=='section':c[4]=c[4].replace('CURRENT LIABILITIES','NON-CURRENT LIABILITIES')
   if change=='lease':c[2]['components']['current_lease']=None
   if change=='nse':c[3]['current_borrowings']=18
   if change=='context':c[3]['statement_type']='standalone'
   if change=='conflict':c[0]['source_conflicts'][0]['annual_report']=31
   with self.assertRaises((ValueError,TypeError),msg=change):self.run_case(c)
 def test_mislabelled_rows_require_subtotal_and_current_component_agreement(self):
  c=self.setup_case('mislabelled_lease_component');changes,_,_=self.run_case(c)
  self.assertEqual(changes,{'total_lease_liabilities':5,'non_current_lease_liabilities':3,'non_current_borrowings':20})
  for bad in ('subtotal','lease','duplicate'):
   c=list(self.setup_case('mislabelled_lease_component'))
   if bad=='subtotal':c[5]=c[5].replace(b'<td>7</td>',b'<td>8</td>')
   if bad=='lease':c[3]['current_lease_liabilities']=3
   if bad=='duplicate':c[5]=c[5].replace(b'<tr><td></td><td>Current liabilities',b'<tr><td>1</td><td>Borrowings, non-current</td><td>3</td></tr><tr><td></td><td>Current liabilities')
   with self.assertRaises(ValueError,msg=bad):self.run_case(c)
 def test_correction_is_audited_and_retention_reverifies(self):
  case=self.setup_case();updated,record=self.run_case(case,apply=True)
  self.assertEqual(updated['source_conflicts'],[]);self.assertEqual(updated['summary'],case[0]['summary'])
  with patch('marketdesk.liability_reviews.verify_liability_review',return_value=(record['changes'],record['conflicts'],{})):
   restored=retain_annual_resolutions(case[0],updated,'TEST','ISIN');self.assertEqual(restored['latest'],updated['latest'])
  with patch('marketdesk.liability_reviews.verify_liability_review',side_effect=ValueError('Changed evidence')):
   blocked=retain_annual_resolutions(case[0],updated,'TEST','ISIN');self.assertTrue(blocked['annual_conflict_recheck_required']);self.assertEqual(blocked['source_conflicts'],case[0]['source_conflicts'])
