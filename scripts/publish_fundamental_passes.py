#!/usr/bin/env python3
"""Combine explicit completed repair runs into one local UI report.

Counts unique stocks and compares their earliest before images with a fresh audit;
it never adds intermediate pass counts together as newly eligible stocks.
"""
import sys,json,copy,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,set_state
from marketdesk.fundamental_repair import missing
from marketdesk.intraday_data import atomic_json
from marketdesk.quality import audit
paths=[Path(p) for p in sys.argv[1:]]
if not paths:raise SystemExit('Pass completed report.json paths explicitly')
runs=[json.loads(p.read_text()) for p in paths]
assert all(r['status']=='complete' for r in runs),'Wait for every selected run to complete'
assert all(r.get('applied') is not False for r in runs),'Exclude dry-run reports'
paired=sorted(zip(runs,paths),key=lambda item:item[0]['started_at'])
base={};last={};checks=0
for r,p in paired:
 for row in json.loads((p.parent/'before.json').read_text()):base.setdefault(row['symbol'],row)
 for row in r['results']:last[row['symbol']]=row
 checks+=len(r['results'])
quality=audit(DEFAULT_DB);started=paired[0][0]['started_at'];folder=ROOT/'data/investor-max'/now().replace(':','-')
report={'status':'complete','mode':'local_paper_only','started_at':started,'finished_at':now(),'updated_at':now(),'run_path':str(folder.relative_to(ROOT)),'quality_before':paired[0][0]['quality_before'],'quality':quality,'total':len(base),'done':len(base),'errors':0,'results':[],'newly_eligible':[],'newly_complete':0,'improved_stocks':0,'recovered_fields':0,'annual_reports_verified':0,'bse_checked':0,'company_sites_checked':0,'report_provider_counts':{},'source_issue_stocks':0}
growth_reasons={'Comparable annual growth inputs missing','Growth inputs do not match annual period, basis and units'}
with connect(DEFAULT_DB) as db:
 records={r['symbol']:(json.loads(r['details']),json.loads(r['report'])) for r in db.execute('SELECT symbol,details,report FROM fundamentals JOIN quality USING(symbol)')}
 for s,b in sorted(base.items()):
  before=json.loads(b['details']);bq=json.loads(b['report']);after,q=records[s];latest=after.get('latest') or {};out=copy.deepcopy(last.get(s,{}));gained=sorted(set(missing(before))-set(missing(after)))
  old_growth=bool(growth_reasons & set(bq['financial_reasons']));new_growth=bool(growth_reasons & set(q['financial_reasons']))
  if old_growth and not new_growth:gained+=['revenue_growth_yoy','net_profit_growth_yoy']
  corrections=[k for k in ('total_borrowings','total_lease_liabilities') if before.get('latest',{}).get(k) is not None and latest.get(k)!=before['latest'][k]]
  before_missing=missing(before)+(['comparable_annual_growth'] if old_growth else [])+(['financial_source_conflicts'] if before.get('source_conflicts') else [])
  after_missing=missing(after)+(['comparable_annual_growth'] if new_growth else [])+(['financial_source_conflicts'] if after.get('source_conflicts') else [])
  proof=after.get('annual_report_check') or {};source=latest.get('source_url')
  out.update(symbol=s,status='complete' if not after_missing and (gained or corrections) else 'improved' if gained or corrections else 'unresolved',before_missing=before_missing,after_missing=after_missing,recovered=gained,corrected_fields=corrections,source=after.get('selected_source'),source_url=source,period=latest.get('period'),basis=latest.get('statement_type'),source_conflicts=len(after.get('source_conflicts',[])),nse_status=out.get('nse_status','checked'),yahoo_status='not_used')
  if proof:out['annual_report']=proof
  elif out.get('reason'):out['annual_report']={'status':'unresolved','reason':out['reason']}
  report['results'].append(out)
  report['newly_complete']+=bool(missing(before)) and not missing(after)
  report['improved_stocks']+=bool(gained or corrections);report['recovered_fields']+=len(gained)
  if not bq['investor_ready'] and q['investor_ready']:report['newly_eligible'].append(s)
  report['annual_reports_verified']+=proof.get('status')=='verified'
  report['source_issue_stocks']+=out['status']=='unresolved' or bool(after.get('source_conflicts'))
  for provider,key in [('BSE','bse_checked'),('Company website','company_sites_checked')]:report[key]+=any(c.get('provider')==provider for c in proof.get('source_checks',[]))
  if proof.get('status')=='verified':report['report_provider_counts'][proof.get('provider','NSE')]=report['report_provider_counts'].get(proof.get('provider','NSE'),0)+1
 report['complete_now']=sum(not missing(d) for d,q in records.values());report['incomplete_now']=len(records)-report['complete_now']
 report['complete_before']=report['complete_now']-report['newly_complete']
 report['blocker_breakdown']={'ineligible':sum(not q['investor_ready'] for d,q in records.values()),'financial_only':sum(not q['price_reasons'] and not q['investor_ready'] for d,q in records.values()),'price_blocked':sum(bool(q['price_reasons']) for d,q in records.values())}
 report['passes']=[{'path':str(p.relative_to(ROOT) if p.is_absolute() else p),'checks':len(r['results']),'started_at':r['started_at'],'finished_at':r['finished_at']} for r,p in paired]
 report['note']=f"{len(base)} unique stocks checked across {len(paired)} completed passes ({checks} checks including retries). Investor eligibility is measured against the {report['quality_before']['investor_ready']}-stock baseline. Recovered growth inputs alone do not imply eligibility. Remaining price and financial blockers are retained; both portfolios remain local and paper only."
 report['net_eligible_change']=quality['investor_ready']-report['quality_before']['investor_ready']
 report['eligibility_reconciliation']={'newly_eligible':len(report['newly_eligible']),'net_change':report['net_eligible_change'],'other_changes':report['net_eligible_change']-len(report['newly_eligible'])}
 remaining=[{'symbol':s,'price_reasons':q['price_reasons'],'financial_reasons':q['financial_reasons'],'missing_fields':missing(d),'period':d.get('latest',{}).get('period'),'annual_report_reason':(d.get('annual_report_check') or {}).get('reason')} for s,(d,q) in records.items() if not q['investor_ready']]
 atomic_json(folder/'remaining-blockers.json',{'checked_at':quality['checked_at'],'counts':report['blocker_breakdown'],'stocks':remaining})
 set_state(db,'fundamental_repair',{k:v for k,v in report.items() if k!='results'})
atomic_json(folder/'before.json',list(base.values()));atomic_json(folder/'report.json',report);atomic_json(ROOT/'data/fundamental-repair/latest.json',report)
print(json.dumps({k:report[k] for k in ('run_path','total','newly_complete','improved_stocks','recovered_fields','newly_eligible','net_eligible_change','blocker_breakdown','eligibility_reconciliation')},indent=2))
