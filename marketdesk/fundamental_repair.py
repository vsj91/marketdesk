from .working_universe import symbol_filter
"""Targeted local fundamental repair. Does not write candles or paper trading tables."""
import copy,json,threading
from .yahoo_crosscheck import apply_yahoo_crosscheck
from concurrent.futures import ThreadPoolExecutor,as_completed
from .storage import ROOT,connect,now,get_state,set_state
from .fallbacks import Fallbacks,apply_fallback
from .annual_reports import AnnualReportSources,apply_report
from .provider import FUNDAMENTAL_REQUIRED_FIELDS
from .intraday_data import atomic_json
from .quality import audit
from .automatic_repair import repair_extra


def missing(details):
    latest=details.get('latest') or {}
    return [key for key in FUNDAMENTAL_REQUIRED_FIELDS if latest.get(key) is None]+([] if details.get('sector') else ['sector'])


def targets(path):
    with connect(path) as db:
        rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,f.observed_at,q.report FROM instruments i LEFT JOIN fundamentals f ON i.symbol=f.symbol LEFT JOIN quality q ON i.symbol=q.symbol WHERE '+symbol_filter(db,'i.symbol')+' ORDER BY i.symbol')]
    from .bank_inputs import bank_refresh_due
    from .insurer_inputs import insurer_refresh_due
    # Bank and insurer filing evidence is refreshed before it reaches the 30-day freshness limit.
    from .reit_inputs import reit_refresh_due
    return [r for r in rows if (json.loads(r['details'] or '{}').get('status')!='COMPLETE' or json.loads(r['report'] or '{}').get('financial_reasons') or bank_refresh_due(json.loads(r['details'] or '{}')) or insurer_refresh_due(json.loads(r['details'] or '{}')) or reit_refresh_due(r['symbol'],json.loads(r['details'] or '{}')))]


def run_repair(job,rows):
    started=job.state['started_at'];folder=ROOT/'data/fundamental-repair'/started.replace(':','-')
    folder.mkdir(parents=True,exist_ok=True)
    if not (folder/'before.json').exists():atomic_json(folder/'before.json',rows)
    report={'status':'running','started_at':started,'total':len(rows),'done':0,'newly_complete':0,
            'improved_stocks':0,'recovered_fields':0,'errors':0,'source_issue_stocks':0,'annual_reports_verified':0,'bse_checked':0,'company_sites_checked':0,'report_provider_counts':{},'results':[],
            'complete_before':job.state['complete_before'],'incomplete_before':len(rows),'mode':'local_paper_only','working_universe':job.state.get('working_universe','all'),
            'note':'Verified annual fields only. Missing is not zero. Yahoo figures with unspecified statement basis remain supplemental. Complete inputs do not imply Investor eligibility or a validated strategy.'}
    checkpoint=folder/'report.json'
    if checkpoint.exists():
        previous=json.loads(checkpoint.read_text())
        if previous.get('started_at')!=started or previous.get('total')!=len(rows):raise ValueError('Repair checkpoint identity mismatch')
        report=previous;report['status']='running';report.pop('error',None);report.pop('finished_at',None)
    if 'quality_before' not in report:
        with connect(job.path) as db:report['quality_before']=get_state(db,'quality_summary',{})
    completed={r['symbol'] for r in report['results']}
    local=threading.local()
    def fetch(row):
        if not hasattr(local,'client'):local.client=Fallbacks()
        before=json.loads(row['details'] or '{}')
        # Reuse today's unchanged NSE parser result, including recorded gaps. The
        # report parser has its own revision and still checks alternate sources.
        result=local.client.fetch(row['symbol'],row['isin'],sector_missing=not before.get('sector'),reuse_recent=True)
        proposed=apply_fallback(copy.deepcopy(before),result)
        if missing(proposed):result['annual_report']=AnnualReportSources().fetch(row['symbol'],row['isin'],proposed)
        if result.get('annual_report'):proposed=apply_report(proposed,result['annual_report'])
        proposed=apply_yahoo_crosscheck(proposed,result.get('yahoo',{}))
        result['extra_repaired']=repair_extra(row['symbol'],row['isin'],proposed)
        result['extra_baseline']=proposed
        return result
    def publish():
        report['updated_at']=now()
        if report['done']:
            report['quality']=audit(job.path)
        with connect(job.path) as db:
            report['complete_now']=db.execute("SELECT COUNT(*) FROM fundamentals WHERE "+symbol_filter(db)+" AND json_extract(details,'$.status')='COMPLETE'").fetchone()[0]
            report['incomplete_now']=db.execute('SELECT COUNT(*) FROM instruments WHERE '+symbol_filter(db)).fetchone()[0]-report['complete_now']
            set_state(db,'fundamental_repair',{k:v for k,v in report.items() if k!='results'})
        atomic_json(folder/'report.json',report)
        atomic_json(ROOT/'data/fundamental-repair/latest.json',report)
        job.state.update({k:report[k] for k in ('status','done','errors','newly_complete','improved_stocks','recovered_fields')})
        job.save()
    try:
        publish()
        # Public NSE requests share a global rate limiter; each worker owns its session.
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending={pool.submit(fetch,row):row for row in rows if row['symbol'] not in completed}
            for future in as_completed(pending):
                row=pending[future];symbol=row['symbol'];before=json.loads(row['details'] or '{}');old_missing=missing(before)
                entry={'symbol':symbol,'before_missing':old_missing,'after_missing':old_missing,'status':'unresolved'}
                try:
                    result=future.result()
                    with connect(job.path) as db:
                        current=db.execute('SELECT details,observed_at FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
                        details=json.loads(current['details']) if current else {}
                        details=apply_fallback(copy.deepcopy(details),result)
                        if result.get('annual_report'):details=apply_report(details,result['annual_report'])
                        details=apply_yahoo_crosscheck(details,result.get('yahoo',{}))
                        # Reapply only while the source context still matches the worker snapshot.
                        if details==result.get('extra_baseline'):details=result['extra_repaired']
                        selected=details.get('latest') or {}
                        observed=selected.get('observed_at') if details.get('selected_source') in ('nse_audited_filing','nse_audited_banking_filing','nse_audited_insurance_filing') else None
                        observed=observed or (current['observed_at'] if current else None) or row.get('observed_at') or started
                        db.execute('INSERT INTO fundamentals VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET details=excluded.details,observed_at=excluded.observed_at',(symbol,json.dumps(details,allow_nan=False),observed))
                    remaining=missing(details);gained=sorted(set(old_missing)-set(remaining))
                    complete=details.get('status')=='COMPLETE' and not remaining
                    nse=result.get('nse',{});yahoo=result.get('yahoo',{})
                    issues=nse.get('errors',[])
                    entry.update(after_missing=remaining,recovered=gained,status='complete' if complete else 'improved' if gained else 'unresolved',
                                 source=details.get('selected_source','upstox'),period=selected.get('period'),basis=selected.get('statement_type'),
                                 nse_status=nse.get('status'),yahoo_status=yahoo.get('status'),source_issues=issues,
                                 source_conflicts=len(details.get('source_conflicts',[])),source_url=selected.get('source_url'),annual_report=result.get('annual_report'))
                    report['annual_reports_verified']=report.get('annual_reports_verified',0)+int(result.get('annual_report',{}).get('status')=='verified')
                    ar=result.get('annual_report',{});checks=ar.get('source_checks',[])
                    for provider,key in [('BSE','bse_checked'),('Company website','company_sites_checked')]:
                        report[key]=report.get(key,0)+int(any(c.get('provider')==provider for c in checks))
                    if ar.get('status')=='verified':
                        providers=report.setdefault('report_provider_counts',{});provider=ar.get('provider','NSE')
                        providers[provider]=providers.get(provider,0)+1
                    report['newly_complete']+=int(complete and before.get('status')!='COMPLETE');report['improved_stocks']+=int(bool(gained));report['recovered_fields']+=len(gained)
                    report['source_issue_stocks']+=int(bool(issues) or yahoo.get('status') in ('unavailable','cooldown') or result.get('annual_report',{}).get('status')=='unresolved')
                except Exception as error:
                    report['errors']+=1;entry['error']=type(error).__name__;entry['status']='error'
                report['results'].append(entry);report['done']+=1;job.state['current']=symbol
                if report['done']%10==0 or report['done']==len(rows):publish()
        job.state['stage']='Final data quality audit';job.save()
        report['quality']=audit(job.path)
        report.update(status='complete',finished_at=now())
        job.state.update(finished_at=report['finished_at'],stage='Annual source repair finished')
    except Exception as error:
        report.update(status='failed',error=type(error).__name__,finished_at=now())
        job.state.update(error=type(error).__name__,finished_at=report['finished_at'])
    finally:
        try:publish()
        finally:job.lock.release()
