#!/usr/bin/env python3
"""Reparse every applied PDF correction and verify allowed changes and retention."""
import sys,json,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now
from marketdesk.annual_conflicts import retain_annual_resolutions,reconcile_debt,reconcile_reviewed
from marketdesk.liability_reviews import reconcile_liability_review
from marketdesk.intraday_data import atomic_json
report=json.loads((ROOT/'data/source-conflicts/latest.json').read_text());assert report['status']=='complete' and report['method']=='company_annual_reports'
folder=ROOT/report['run_path'];baseline=json.loads((folder/'before.json').read_text());eligible=[];checked=[]
with connect(DEFAULT_DB) as db:
 for row in baseline:
  s=row['symbol'];before=json.loads(row['details']);current=db.execute('SELECT f.details,q.report FROM fundamentals f JOIN quality q USING(symbol) WHERE symbol=?',(s,)).fetchone();after=json.loads(current['details']);quality=json.loads(current['report'])
  records=after.get('annual_conflict_resolutions',[])
  if not records:continue
  record=records[-1];resolver=reconcile_debt if record['kind']=='lease_classification' else reconcile_liability_review if record['kind']=='reviewed_liability_classification' else reconcile_reviewed
  expected,_=resolver(before,record['report'],s,row['isin'])
  assert after['latest']==expected['latest'],s
  assert after.get('summary')==before.get('summary'),s
  assert after['source_conflicts']==expected['source_conflicts'],s
  changed={k for k in before['latest'] if before['latest'][k]!=after['latest'].get(k)}
  assert changed<=set(record['changes'])|{'sources'},(s,changed)
  retained=retain_annual_resolutions(before,after,s,row['isin']);assert retained['latest']==after['latest'] and retained['source_conflicts']==after['source_conflicts'],s
  from marketdesk.provider import extract_fundamental_details
  from marketdesk.importer import summary_metrics
  from marketdesk.fallbacks import apply_fallback
  from marketdesk.annual_reports import retain_report
  from marketdesk.growth_repair import retain_growth
  from marketdesk.conflict_repair import retain_resolutions
  from marketdesk.report_growth import retain_company_growth
  payloads={p['endpoint']:json.loads(p['payload']) for p in db.execute('SELECT endpoint,payload FROM payloads WHERE symbol=? AND http_status=200 AND (error IS NULL OR error="")',(s,))}
  raw=extract_fundamental_details(payloads);raw['summary']=summary_metrics(payloads);raw=apply_fallback(raw,json.loads((ROOT/'data/fallbacks'/(s+'.json')).read_text()))
  for retain in (retain_resolutions,retain_report,retain_growth,retain_resolutions,retain_annual_resolutions,retain_company_growth):raw=retain(raw,after,s,row['isin'])
  assert not raw.get('source_conflicts') and not raw.get('annual_conflict_recheck_required'),s
  assert all(raw['latest'].get(k)==after['latest'].get(k) for k in record['changes']),s
  changed_before=copy.deepcopy(before);changed_before['latest']['equity']+=1
  assert retain_annual_resolutions(changed_before,after,s,row['isin']).get('annual_conflict_recheck_required'),s
  if quality['investor_ready'] and not json.loads(row['report'])['investor_ready']:eligible.append(s)
  checked.append(s)
 assert set(eligible)==set(report['newly_eligible'])
 assert len(checked)==report['cleared_stocks']
result={'status':'passed','checked_at':now(),'stocks_verified':len(checked),'newly_eligible_verified':len(eligible),'only_documented_fields_changed':True,'growth_values_unchanged':True,'reimport_retention_verified':True,'changed_context_fails_closed':True,'evidence_reparsed':True}
atomic_json(folder/'verification.json',result);print(json.dumps(result,indent=2))
