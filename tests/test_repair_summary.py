import copy,unittest
from marketdesk.repair_summary import combine_reports
from marketdesk.provider import FUNDAMENTAL_REQUIRED_FIELDS

def detail(gaps=()):
 return {'status':'PARTIAL' if gaps else 'COMPLETE','sector':'Test','latest':{k:None if k in gaps else 1 for k in FUNDAMENTAL_REQUIRED_FIELDS}}

def report(rows,complete_before):
 return {'status':'complete','total':len(rows),'results':rows,'complete_before':complete_before,'started_at':'start','finished_at':'end','updated_at':'end','done':len(rows),'newly_complete':0,'recovered_fields':0}

class SummaryTests(unittest.TestCase):
 def test_followup_keeps_original_gaps_and_counts_each_stock_once(self):
  lease='total_lease_liabilities';finance='finance_costs';borrow='total_borrowings'
  a=report([{'symbol':'A','before_missing':[lease]}, {'symbol':'B','before_missing':[borrow,finance]}, {'symbol':'C','before_missing':[lease]}],1)
  b=report([{'symbol':'B','before_missing':[finance]}, {'symbol':'C','before_missing':[lease]}],2)
  got=combine_reports(a,b,{'A':detail(),'B':detail(),'C':detail([lease]),'D':detail()})
  self.assertEqual(got['newly_complete'],2);self.assertEqual(got['recovered_fields'],3);self.assertEqual(got['improved_stocks'],2);self.assertEqual(got['complete_now'],3);self.assertEqual(got['incomplete_now'],1)
  row=next(r for r in got['results'] if r['symbol']=='B');self.assertEqual(row['before_missing'],[borrow,finance]);self.assertEqual(row['after_missing'],[])
 def test_rejects_running_pass_and_unrelated_stocks(self):
  a=report([{'symbol':'A','before_missing':['finance_costs']}],0);b=report([{'symbol':'OTHER','before_missing':[]}],0)
  with self.assertRaises(ValueError):combine_reports(a,b,{'A':detail()})
  b=copy.deepcopy(a);b['status']='running'
  with self.assertRaises(ValueError):combine_reports(a,b,{'A':detail()})
 def test_prior_verified_evidence_survives_unresolved_followup(self):
  a=report([{'symbol':'A','before_missing':['finance_costs','total_borrowings'],'annual_report':{'status':'verified','provider':'BSE','source_checks':[{'provider':'BSE','status':'available'}]}}],0)
  b=report([{'symbol':'A','before_missing':['finance_costs'],'annual_report':{'status':'unresolved','reason':'PBT mismatch','source_checks':[{'provider':'BSE','status':'available'}]}}],0)
  got=combine_reports(a,b,{'A':detail(['finance_costs'])});self.assertEqual(got['bse_checked'],1);self.assertEqual(got['annual_reports_verified'],1);self.assertEqual(got['results'][0]['annual_report']['followup_reason'],'PBT mismatch')
