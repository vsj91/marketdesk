#!/usr/bin/env python3
"""Apply audited bank-filing inputs and verified two-year growth inputs to Investor-blocked stocks.

Dry run unless --apply. Uses the same repair path as maintenance (automatic_repair.repair_extra),
plus the provider's own two-year income history when a prior-year base is not positive."""
import sys,json,copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.working_universe import symbol_filter
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,number
from marketdesk.automatic_repair import repair_extra
from marketdesk.importer import summary_metrics
from marketdesk.quality import audit,financial_reasons,source_reasons
from marketdesk.intraday_data import atomic_json
apply='--apply' in sys.argv
only=set(json.loads(Path(sys.argv[sys.argv.index('--symbols-file')+1]).read_text())) if '--symbols-file' in sys.argv else None
with connect(DEFAULT_DB) as db:
    before=get_state(db,'quality_summary',{})
    rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,f.observed_at,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol) WHERE '+symbol_filter(db,'i.symbol'))]
    payloads={}
    for r in db.execute('SELECT symbol,endpoint,payload FROM payloads WHERE '+symbol_filter(db)):payloads.setdefault(r[0],{})[r[1]]=json.loads(r[2])
rows=[r for r in rows if not json.loads(r['report'])['investor_ready'] and (only is None or r['symbol'] in only)]

def provider_growth_inputs(details,payloads):
    """Two-year provider history for the selected summary's own period, basis and units."""
    out=copy.deepcopy(details);summary=out.get('summary') or {}
    computed=summary_metrics(payloads)
    for key,entry in (computed.get('growth_inputs') or {}).items():
        if number(summary.get(key+'_growth_yoy')) is not None or key in (summary.get('growth_inputs') or {}):continue
        if computed.get('statement_type')!=summary.get('statement_type') or computed.get('units')!=summary.get('units') or computed.get(key+'_period')!=summary.get(key+'_period'):continue
        a,b=number(entry['current']),number(summary.get(key))
        if a is None or b is None or abs(a-b)>max(.015,abs(b)*.00002):continue
        summary.setdefault('growth_inputs',{})[key]=entry
    out['summary']=summary
    return out

def work(row):
    d=json.loads(row['details']);symbol=row['symbol']
    proposed=provider_growth_inputs(d,payloads.get(symbol,{}))
    proposed=repair_extra(symbol,row['isin'],proposed)
    observed=proposed['latest'].get('observed_at') if proposed.get('selected_source') in ('nse_audited_filing','nse_audited_banking_filing','nse_audited_insurance_filing','company_annual_report') and proposed['latest'].get('observed_at') else row['observed_at']
    return d,proposed,observed

started=now();folder=ROOT/'data/readiness-inputs'/started.replace(':','-');atomic_json(folder/'before.json',rows)
results=[]
with ThreadPoolExecutor(max_workers=4) as pool:
    jobs={pool.submit(work,r):r for r in rows}
    for job in as_completed(jobs):
        row=jobs[job];entry={'symbol':row['symbol'],'before':json.loads(row['report'])['financial_reasons']}
        try:
            d,proposed,observed=job.result()
            with connect(DEFAULT_DB) as db:
                live=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(row['symbol'],)).fetchone()[0])
                if live!=d:raise ValueError('Concurrent financial update; left for retry')
                times={r[0]:r[1] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(row['symbol'],))}
                clock=datetime.now(timezone.utc)
                entry['after']=financial_reasons(proposed,observed,clock)+source_reasons(proposed,times,clock)
                entry['attempts']=(proposed.get('automatic_repair') or {}).get('attempts')
                changed=proposed!=d
                if apply and changed:db.execute('UPDATE fundamentals SET details=?,observed_at=? WHERE symbol=?',(json.dumps(proposed,allow_nan=False),observed,row['symbol']))
            entry['status']='changed' if changed else 'unchanged'
        except Exception as error:entry.update(status='error',error=str(error)[:300])
        results.append(entry);print(entry['symbol'],entry['status'],entry.get('after',entry.get('error')),flush=True)
report={'started_at':started,'finished_at':now(),'applied':apply,'total':len(rows),'quality_before':before,'results':sorted(results,key=lambda e:e['symbol'])}
if apply:report['quality_after']=audit(DEFAULT_DB)
atomic_json(folder/'report.json',report)
print(json.dumps({k:report.get(k) for k in ('applied','total')}|{'investor_before':before.get('investor_ready'),'investor_after':(report.get('quality_after') or {}).get('investor_ready')}))
