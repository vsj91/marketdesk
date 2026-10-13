import copy,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from datetime import datetime,timezone,timedelta
from test_fallbacks import fixture
from marketdesk.fallbacks import nse_annual,apply_fallback,lease_label,Fallbacks
from marketdesk.financial_sources import AnnualSources
from marketdesk.fundamental_repair import run_repair,targets
from marketdesk.quality import source_reasons
from marketdesk.storage import init,connect,now,get_state
from marketdesk.paper import init_paper,portfolios

ISIN='INE000A01010'
def parsed(text=None):return nse_annual(text or fixture(),'TEST',ISIN,'https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_test.html')
def partial():return {'status':'PARTIAL','sector':'Test','missing_fields':['finance_costs'],'latest':{'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','missing_fields':['finance_costs'],'profit_before_tax':10},'summary':{'statement_type':'consolidated','units':'crore','frequency':'yearly','revenue_period':'Mar 2026','net_profit_period':'Mar 2026','revenue_growth_yoy':100,'net_profit_growth_yoy':100}}

class AnnualRepairTests(unittest.TestCase):
 def test_report_repair_reuses_recent_filing_check_without_reaging(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);(root/'data/fallbacks').mkdir(parents=True)
   old={'version':2,'parser_revision':5,'symbol':'TEST','isin':ISIN,'observed_at':now(),'nse':{'errors':[{'reason':'Unsupported lease caption'}]},'yahoo':{}}
   (root/'data/fallbacks/TEST.json').write_text(json.dumps(old))
   with patch('marketdesk.fallbacks.ROOT',root),patch.object(AnnualSources,'fetch') as fetch:
    self.assertEqual(Fallbacks().fetch('TEST',ISIN,reuse_recent=True),old);fetch.assert_not_called()
    old['parser_revision']=4;(root/'data/fallbacks/TEST.json').write_text(json.dumps(old));fetch.return_value={'observed_at':now()}
    Fallbacks().fetch('TEST',ISIN,reuse_recent=True);fetch.assert_called_once()
 def test_observed_lease_aliases_and_non_lease_captions(self):
  for caption in ('(ii) Lease liabilities','Lease Liablities','Lease Liabilties','Current Lease liabilities','Lease liabilities, current','Long Term Lease Liabilities','Lease liabilities (Non-current)','i) Lease Liabilities','Lease Laibilities','Lease Liabiities','Lease Liabililty','Lease Liablility','Lease Liabitliy','Lease Libailities','Lease Liabliities','Lease Liab'):
   self.assertEqual(parsed(fixture().replace('Lease liabilities',caption))['total_lease_liabilities'],.5,caption)
  for caption in ('Advance for Lease','Right of use assets','Rent liabilities','Lease','Other financial liabilities including lease liability','Lease liabilities, Capital Creditors and Deferred Expenses'):
   self.assertFalse(lease_label(caption))
   self.assertIsNone(parsed(fixture().replace('Lease liabilities',caption))['total_lease_liabilities'])
 def test_duplicate_lease_caption_is_ambiguous(self):
  html=fixture().replace('<td>Lease liabilities</td><td>30</td><td>20</td></tr>','<td>Lease liabilities</td><td>30</td><td>20</td></tr><tr><td>Lease liabilities</td><td>30</td><td>20</td></tr>')
  self.assertIsNone(parsed(html)['total_lease_liabilities'])
 def test_non_classified_lease_alias_requires_one_unambiguous_total(self):
  html=fixture().replace('Non-current liabilities','Financial liabilities').replace('Current liabilities','Other liabilities')
  html=html.replace('<tr><td>Lease liabilities</td><td>20</td><td>10</td></tr>','')
  for label in ('Lease liability','Lease Liabilites','Lease Liabilities'):
   self.assertEqual(parsed(html.replace('Lease liabilities',label))['total_lease_liabilities'],.3)
  self.assertIsNone(parsed(html.replace('Lease liabilities','Other liabilities including leases'))['total_lease_liabilities'])
  self.assertIsNone(parsed(html.replace('Lease liabilities','Current lease liabilities'))['total_lease_liabilities'])
  self.assertIsNone(parsed(html.replace('Financial liabilities','Non-current liabilities'))['total_lease_liabilities'])
 def test_standalone_whole_statement_drops_consolidated_growth(self):
  row=parsed(fixture().replace('Consolidated','Standalone'));old=partial()
  result=apply_fallback(copy.deepcopy(old),{'version':2,'nse':{'annual':[row]}})
  self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['latest']['statement_type'],'standalone')
  self.assertIsNone(result['summary']['revenue_growth_yoy'])
  legacy=apply_fallback(copy.deepcopy(old),{'nse':{'annual':[row]}})
  self.assertEqual(legacy['status'],'PARTIAL')
 def test_comparative_columns_provide_matched_growth(self):
  html=fixture().replace('<td>Finance costs</td>', '<td>Revenue from operations</td><td>200</td><td>100</td></tr><tr><td>Total profit (loss) for period</td><td>40</td><td>20</td></tr><tr><td>Finance costs</td>')
  latest=parsed(html);prior=nse_annual(html,'TEST',ISIN,'url',year_offset=-1)
  result=apply_fallback(partial(),{'version':2,'nse':{'annual':[latest,prior]}})
  self.assertEqual(result['summary']['revenue_growth_yoy'],100)
  self.assertEqual(result['summary']['net_profit_growth_yoy'],100)
  self.assertEqual(prior['period'],'Mar 2025');self.assertAlmostEqual(prior['total_lease_liabilities'],.3)
 def test_nbfc_missing_debt_component_is_not_zero(self):
  html=fixture().replace('Borrowings, non-current','Debt Securities').replace('Borrowings, current','Borrowings (Other than Debt Securities)')
  self.assertIsNone(parsed(html)['total_borrowings'])
  html=html.replace('<td>Cash and cash equivalents</td>','<td>Subordinated Liabilities</td><td>0</td><td>0</td></tr><tr><td>Cash and cash equivalents</td>')
  self.assertEqual(parsed(html)['total_borrowings'],3)
  with self.assertRaises(ValueError):parsed(html.replace('<td>Subordinated Liabilities</td><td>0</td>','<td>Subordinated Liabilities</td><td>-1</td>'))
 def test_sector_requires_yahoo_symbol_identity(self):
  detail={'status':'PARTIAL','latest':parsed(),'missing_fields':['sector']}
  for symbol in ('OTHER',None):
   got=apply_fallback(copy.deepcopy(detail),{'symbol':symbol,'yahoo':{'symbol':'TEST.NS','sector':'Technology'}})
   self.assertEqual(got['status'],'PARTIAL')
  got=apply_fallback(detail,{'symbol':'TEST','yahoo':{'symbol':'TEST.NS','sector':'Technology'}})
  self.assertEqual(got['status'],'COMPLETE')
 def test_nse_evidence_does_not_require_unused_upstox_payloads(self):
  clock=datetime.now(timezone.utc)
  f={'selected_source':'nse_audited_filing','latest':{'observed_at':clock.isoformat()}}
  self.assertEqual(source_reasons(f,{},clock),[])
  f['latest']['observed_at']=(clock-timedelta(days=31)).isoformat()
  f['fallbacks']={'nse':{'observed_at':clock.isoformat()}}
  self.assertTrue(source_reasons(f,{},clock))
 def test_older_or_less_complete_statement_does_not_replace_good_row(self):
  old=partial();new=parsed();new['period']='Mar 2025'
  self.assertEqual(apply_fallback(copy.deepcopy(old),{'version':2,'nse':{'annual':[new]}})['latest'],old['latest'])
  new=parsed();old={'status':'COMPLETE','latest':new,'sector':'Test','missing_fields':[]}
  bad=copy.deepcopy(new);bad['equity']=None;bad['missing_fields']=['equity']
  self.assertEqual(apply_fallback(copy.deepcopy(old),{'version':2,'nse':{'annual':[bad]}})['latest'],new)
 def test_index_quarter_flag_does_not_reject_audited_annual(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);api=Mock();api.json.return_value=[{'symbol':'TEST','consolidated':'Standalone','audited':'Un-Audited','qe_Date':'31-Mar-2026','ixbrl':'https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_INDAS_test.html'}]
   doc=Mock();doc.text=fixture().replace('Consolidated','Standalone');client=Mock();client.yahoo.return_value={'status':'no_data'}
   with patch('marketdesk.financial_sources.ROOT',root),patch.object(AnnualSources,'client',return_value=client),patch('marketdesk.financial_sources.request',side_effect=[api,doc]):
    result=AnnualSources().fetch('TEST',ISIN)
   self.assertEqual(result['nse']['annual'][0]['statement_type'],'standalone')
   self.assertEqual(result['nse']['annual'][0]['missing_fields'],[])
 def test_cache_identity_is_checked(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);(root/'data/fallbacks').mkdir(parents=True)
   (root/'data/fallbacks/TEST.json').write_text(json.dumps({'symbol':'WRONG','isin':ISIN}))
   with patch('marketdesk.financial_sources.ROOT',root),self.assertRaisesRegex(ValueError,'identity'):AnnualSources().fetch('TEST',ISIN)
 def test_repair_counts_and_does_not_touch_paper_or_candles(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);path=root/'db';init(path);init_paper(path)
   with connect(path) as db:
    db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',('TEST','NSE_EQ|'+ISIN,ISIN,'Test','Test'))
    db.execute('INSERT INTO fundamentals VALUES(?,?,?)',('TEST',json.dumps(partial()),now()))
   before=portfolios(path);row=parsed();row['observed_at']=now()
   job=Mock();job.path=path;job.state={'started_at':now(),'complete_before':0};job.lock=threading.Lock();job.lock.acquire()
   with patch('marketdesk.fundamental_repair.ROOT',root),patch('marketdesk.fundamental_repair.Fallbacks.fetch',return_value={'version':2,'nse':{'annual':[row]},'yahoo':{'status':'available'}}):run_repair(job,targets(path))
   self.assertEqual(portfolios(path),before);self.assertFalse(job.lock.locked())
   with connect(path) as db:
    report=get_state(db,'fundamental_repair');self.assertEqual(report['newly_complete'],1);self.assertEqual(report['done'],1)
    self.assertEqual(db.execute('SELECT COUNT(*) FROM candles').fetchone()[0],0)
   self.assertEqual(json.loads((root/'data/fundamental-repair/latest.json').read_text())['results'][0]['status'],'complete')
   job.lock.acquire()
   original_rows=json.loads(next((root/'data/fundamental-repair').glob('*/before.json')).read_text())
   with patch('marketdesk.fundamental_repair.ROOT',root),patch('marketdesk.fundamental_repair.Fallbacks.fetch') as fetch:
    run_repair(job,original_rows);fetch.assert_not_called()
   with connect(path) as db:self.assertEqual(get_state(db,'fundamental_repair')['newly_complete'],1)
 def test_nse_using_upstox_growth_still_checks_income_timestamp(self):
  clock=datetime.now(timezone.utc)
  f={'selected_source':'nse_audited_filing','latest':{'observed_at':clock.isoformat()},'summary':{'revenue_growth_yoy':5}}
  self.assertTrue(source_reasons(f,{},clock))
  self.assertEqual(source_reasons(f,{'income_statement':clock.isoformat()},clock),[])
  f['summary'].update(growth_sources={'revenue':'nse_audited_filing'},growth_observed_at={'revenue':[clock.isoformat(),(clock-timedelta(days=31)).isoformat()]})
  self.assertTrue(source_reasons(f,{},clock))
 def test_unchanged_selected_row_keeps_original_evidence_age(self):
  old=parsed();old['missing_fields']=['total_lease_liabilities'];old['total_lease_liabilities']=None
  f={'selected_source':'nse_audited_filing','latest':old,'fallbacks':{'nse':{'observed_at':'2020-01-01T00:00:00+00:00'}}}
  apply_fallback(f,{'version':2,'nse':{'annual':[],'observed_at':now()}})
  self.assertEqual(f['latest']['observed_at'],'2020-01-01T00:00:00+00:00')

 def test_resume_keeps_original_refresh_checkpoint(self):
  from marketdesk.refresh import Refresh
  from marketdesk.storage import set_state
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);path=root/'db';init(path)
   original={'status':'interrupted','mode':'both','done':123,'completed_symbols':['OTHER']}
   with connect(path) as db:
    db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',('TEST','NSE_EQ|'+ISIN,ISIN,'Test','Test'))
    db.execute('INSERT INTO fundamentals VALUES(?,?,?)',('TEST',json.dumps(partial()),now()))
    set_state(db,'refresh',original)
   with patch('marketdesk.refresh.ROOT',root),patch('marketdesk.refresh.threading.Thread'):
    first=Refresh(path);first.start_fundamental_repair();first.lock.release()
    second=Refresh(path);second.start_fundamental_repair();second.lock.release()
   with connect(path) as db:self.assertEqual(get_state(db,'refresh_before_fundamental_repair'),original)
