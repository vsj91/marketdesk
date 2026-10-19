#!/usr/bin/env python3
"""Run the fundamental-repair source sequence for named stocks now (dry run unless --apply).

Same steps as maintenance's fundamental repair: NSE audited fallback, company annual
reports, Yahoo two-anchor cross-check, then automatic repair (growth, conflicts, bank and
insurer filings). Usage: refresh_fundamental_sources.py --symbols-file FILE [--apply]"""
import sys,json,copy
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state
from marketdesk.fallbacks import Fallbacks,apply_fallback
from marketdesk.annual_reports import AnnualReportSources,apply_report
from marketdesk.yahoo_crosscheck import apply_yahoo_crosscheck
from marketdesk.automatic_repair import repair_extra
from marketdesk.fundamental_repair import missing
from marketdesk.quality import audit,financial_reasons,source_reasons
from marketdesk.intraday_data import atomic_json
apply='--apply' in sys.argv
symbols=json.loads(Path(sys.argv[sys.argv.index('--symbols-file')+1]).read_text())
client=Fallbacks();started=now();folder=ROOT/'data/source-refresh'/started.replace(':','-');results=[]
with connect(DEFAULT_DB) as db:before=get_state(db,'quality_summary',{})
for symbol in symbols:
    entry={'symbol':symbol}
    try:
        with connect(DEFAULT_DB) as db:
            row=db.execute('SELECT i.isin,f.details,f.observed_at FROM instruments i JOIN fundamentals f USING(symbol) WHERE symbol=?',(symbol,)).fetchone()
        isin,original,observed=row[0],json.loads(row[1]),row[2];atomic_json(folder/'before'/(symbol+'.json'),{'details':original,'observed_at':observed})
        result=client.fetch(symbol,isin,sector_missing=not original.get('sector'))
        details=apply_fallback(copy.deepcopy(original),result)
        if missing(details):details=apply_report(details,AnnualReportSources().fetch(symbol,isin,details))
        details=apply_yahoo_crosscheck(details,result.get('yahoo',{}))
        details=repair_extra(symbol,isin,details)
        latest=details.get('latest') or {}
        if details.get('selected_source') in ('nse_audited_filing','nse_audited_banking_filing','nse_audited_insurance_filing','company_annual_report') and latest.get('observed_at'):observed=latest['observed_at']
        with connect(DEFAULT_DB) as db:
            live=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()[0])
            if live!=original:raise ValueError('Concurrent financial update; left for retry')
            times={r[0]:r[1] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(symbol,))}
            clock=datetime.now(timezone.utc)
            entry.update(before=financial_reasons(original,row[2],clock)+source_reasons(original,times,clock),after=financial_reasons(details,observed,clock)+source_reasons(details,times,clock),
                         missing_before=missing(original),missing_after=missing(details))
            if apply and details!=original:db.execute('UPDATE fundamentals SET details=?,observed_at=? WHERE symbol=?',(json.dumps(details,allow_nan=False),observed,symbol))
    except Exception as error:entry['error']=str(error)[:300]
    results.append(entry);print(json.dumps(entry),flush=True)
report={'started_at':started,'applied':apply,'quality_before':before,'results':results}
if apply:report['quality_after']=audit(DEFAULT_DB)
atomic_json(folder/'report.json',report)
print(json.dumps({'investor_before':before.get('investor_ready'),'investor_after':(report.get('quality_after') or {}).get('investor_ready')}))
