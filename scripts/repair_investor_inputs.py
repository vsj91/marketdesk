#!/usr/bin/env python3
"""Prioritize financial-only Investor blockers; publish audited live progress."""
import sys,json,copy,time,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,set_state
from marketdesk.annual_reports import AnnualReportSources,apply_report,save_excerpt,REVISION,pdf_worker
from marketdesk.annual_conflicts import reconcile_debt
from marketdesk.conflict_repair import verify_nse
from marketdesk.fundamental_repair import missing
from marketdesk.quality import audit
from marketdesk.intraday_data import atomic_json
apply='--apply' in sys.argv;limit=int(sys.argv[sys.argv.index('--limit')+1]) if '--limit' in sys.argv else 200
with connect(DEFAULT_DB) as db:
 quality=get_state(db,'quality_summary',{});allrows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol)')]
 complete_before=sum(not missing(json.loads(r['details'])) for r in allrows)
rows=[r for r in allrows if not json.loads(r['report'])['price_reasons'] and json.loads(r['report'])['financial_reasons']==['Annual financial inputs incomplete']]
def priority(r):
 reason=(json.loads(r['details']).get('annual_report_check') or {}).get('reason','')
 return (0 if r['symbol'] in ('TTKPRESTIG','ASHAPURMIN','BAJAJCON') else 1 if 'Duplicate PDF' in reason else 2 if 'Fewer than two matching' in reason else 3,r['symbol'])
rows=sorted(rows,key=priority)[:limit];started=now();folder=ROOT/'data/investor-inputs'/started.replace(':','-');atomic_json(folder/'before.json',rows)
blockers={'ineligible':sum(not json.loads(r['report'])['investor_ready'] for r in allrows),'financial_only':sum(not json.loads(r['report'])['price_reasons'] and not json.loads(r['report'])['investor_ready'] for r in allrows),'price_blocked':sum(bool(json.loads(r['report'])['price_reasons']) for r in allrows)}
report={'status':'running','started_at':started,'mode':'local_paper_only','total':len(rows),'done':0,'newly_complete':0,'improved_stocks':0,'recovered_fields':0,'annual_reports_verified':0,'bse_checked':0,'company_sites_checked':0,'report_provider_counts':{},'errors':0,'source_issue_stocks':0,'results':[],'quality_before':quality,'quality':quality,'complete_before':complete_before,'applied':apply,'run_path':str(folder.relative_to(ROOT)),'blocker_breakdown':blockers,'note':'Targets stocks whose only Investor blocker is incomplete annual inputs. Values require verified report context and matching anchors. Missing or dashed amounts are never replaced with zero. Both portfolios remain paper only.'}
last_audit=0

def publish(final=False):
 global last_audit
 if apply and (final or time.monotonic()-last_audit>30):
  report['quality']=audit(DEFAULT_DB);last_audit=time.monotonic()
 with connect(DEFAULT_DB) as db:
  report['complete_now']=db.execute("SELECT COUNT(*) FROM fundamentals WHERE json_extract(details,'$.status')='COMPLETE'").fetchone()[0];report['incomplete_now']=len(allrows)-report['complete_now']
  ready={r['symbol'] for r in db.execute('SELECT symbol,report FROM quality') if json.loads(r['report'])['investor_ready']}
  report['newly_eligible']=[r['symbol'] for r in rows if r['symbol'] in ready]
  report['updated_at']=now()
  if apply:set_state(db,'fundamental_repair',{k:v for k,v in report.items() if k!='results'})
 atomic_json(folder/'report.json',report)
 if apply:atomic_json(ROOT/'data/fundamental-repair/latest.json',report)

def fetch(row):
 symbol=row['symbol'];d=json.loads(row['details']);cached=ROOT/'tmp/pdfs'/symbol
 if (cached/'report.pdf').exists():
  # Reuse downloaded bytes only if they match a known primary archive response.
  old=d.get('annual_report_check') or {};raw=(cached/'report.pdf').read_bytes();sha=hashlib.sha256(raw).hexdigest()
  known=next((x for x in old.get('attempts',[]) if x.get('raw_sha256')==sha and x.get('provider')=='NSE'),None)
  if known:
   (cached/'context.json').write_text(json.dumps({'latest':d['latest']}));pdf_worker(cached/'report.pdf',cached/'context.json',cached/'updated.json');parsed=json.loads((cached/'updated.json').read_text())
   if parsed['status']=='verified':
    evidence,digest=save_excerpt(ROOT/'data/annual-reports'/symbol,parsed['excerpt'],sha)
    return {'revision':REVISION,'status':'verified','symbol':symbol,'isin':row['isin'],'observed_at':old['observed_at'],'provider':'NSE','source_url':known['source_url'],'raw_sha256':sha,'excerpt_sha256':digest,'evidence_path':str(evidence.relative_to(ROOT)),'evidence':parsed['evidence'],'source_checks':[{'provider':'NSE','status':'available','reports':1}]}
 return AnnualReportSources().fetch(symbol,row['isin'],d)
publish()
with ThreadPoolExecutor(max_workers=6) as pool:
 jobs={pool.submit(fetch,row):row for row in rows}
 for future in as_completed(jobs):
  row=jobs[future];symbol=row['symbol'];base=json.loads(row['details']);entry={'symbol':symbol,'before_missing':missing(base)}
  try:
   proof=future.result()
   with connect(DEFAULT_DB) as db:current=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()[0])
   updated=apply_report(copy.deepcopy(current),proof)
   if updated.get('source_conflicts') and proof.get('status')=='verified':
    try:
     if not all(updated['latest'].get(k) for k in ('raw_sha256','source_url','observed_at')):
      official=verify_nse(symbol,row['isin'],current);updated['latest'].update({k:official[k] for k in ('raw_sha256','source_url','observed_at')})
     updated,_=reconcile_debt(updated,proof,symbol,row['isin'])
    except (ValueError,KeyError,OSError):pass
   recovered=sorted(set(missing(current))-set(missing(updated)))
   with connect(DEFAULT_DB) as db:
    live=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()[0])
    if live!=current:raise ValueError('Concurrent financial update; record left for retry')
    if apply:db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(updated,allow_nan=False),symbol))
   entry.update(after_missing=missing(updated),recovered=recovered,status='complete' if not missing(updated) else 'improved' if recovered else 'unresolved',annual_report=proof,source=updated.get('selected_source'),source_url=updated['latest'].get('source_url'),period=updated['latest'].get('period'),basis=updated['latest'].get('statement_type'),source_conflicts=len(updated.get('source_conflicts',[])),nse_status='verified' if proof.get('status')=='verified' else 'checked',yahoo_status='not_used')
   report['newly_complete']+=not missing(updated);report['improved_stocks']+=bool(recovered);report['recovered_fields']+=len(recovered);report['annual_reports_verified']+=proof.get('status')=='verified';report['source_issue_stocks']+=proof.get('status')!='verified'
   for provider,key in [('BSE','bse_checked'),('Company website','company_sites_checked')]:report[key]+=any(c['provider']==provider for c in proof.get('source_checks',[]))
   if proof.get('status')=='verified':
    provider=proof.get('provider','NSE');report['report_provider_counts'][provider]=report['report_provider_counts'].get(provider,0)+1
  except Exception as e:entry.update(after_missing=entry['before_missing'],status='error',error=str(e));report['errors']+=1
  report['results'].append(entry);report['done']+=1
  if entry.get('recovered') or report['done']%10==0:
   publish();print(json.dumps({'done':report['done'],'total':report['total'],'newly_complete':report['newly_complete'],'investor':report['quality']['investor_ready'],'symbol':symbol,'status':entry['status']}),flush=True)
report.update(status='complete',finished_at=now());publish(final=True)
print(json.dumps({k:report[k] for k in ('status','done','newly_complete','recovered_fields','errors','newly_eligible','run_path')},indent=2),flush=True)
