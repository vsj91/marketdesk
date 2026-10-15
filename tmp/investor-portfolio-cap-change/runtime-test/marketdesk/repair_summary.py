"""Combine completed repair passes against the original stock-by-stock baseline.

This produces a report only. It never modifies financial inputs or paper ledgers.
Individual pass reports remain available as the audit trail.
"""
import copy
from collections import Counter
from .fundamental_repair import missing

def combine_reports(first,followup,current):
    if first.get('status')!='complete' or followup.get('status')!='complete':raise ValueError('Both repair passes must be complete')
    original={r['symbol']:r for r in first['results']};later={r['symbol']:r for r in followup['results']}
    if len(original)!=first['total'] or len(later)!=followup['total']:raise ValueError('Incomplete or duplicate result rows')
    if not set(later)<=set(original):raise ValueError('Follow-up contains stocks outside original repair')
    if not set(original)<=set(current):raise ValueError('Current records missing from original repair')
    report={k:copy.deepcopy(v) for k,v in first.items() if k!='results'}
    rows=[];providers=Counter();bse=company=verified=issues=0
    for symbol,initial in original.items():
        final=later.get(symbol,initial);details=current[symbol];annual=details.get('latest') or {}
        row=copy.deepcopy(final);remaining=missing(details);gained=sorted(set(initial['before_missing'])-set(remaining))
        complete=details.get('status')=='COMPLETE' and not remaining
        row.update(before_missing=initial['before_missing'],after_missing=remaining,recovered=gained,
                   status='complete' if complete else 'improved' if gained else 'error' if final.get('error') else 'unresolved',
                   period=annual.get('period'),basis=annual.get('statement_type'),source=details.get('selected_source','upstox'),
                   source_url=annual.get('source_url'),source_conflicts=len(details.get('source_conflicts',[])))
        history=[initial]+([final] if symbol in later else [])
        checks=[r['annual_report'] for r in history if r.get('annual_report')]
        accepted=[a for a in checks if a.get('status')=='verified']
        verified+=bool(accepted)
        for provider in {a.get('provider','NSE') for a in accepted}:providers[provider]+=1
        source_checks={c['provider']:c for a in checks for c in a.get('source_checks',[])}
        bse+='BSE' in source_checks;company+='Company website' in source_checks
        if accepted:
            row['annual_report']=copy.deepcopy(accepted[-1]);row['annual_report']['source_checks']=list(source_checks.values())
            if (final.get('annual_report') or {}).get('status')=='unresolved':row['annual_report']['followup_reason']=final['annual_report'].get('reason')
        row['annual_report_checks']=copy.deepcopy(checks)
        issues+=bool(any(r.get('source_issues') or r.get('yahoo_status') in ('unavailable','cooldown') for r in (initial,final)) or any(a.get('status')=='unresolved' for a in checks))
        rows.append(row)
    complete_now=sum(d.get('status')=='COMPLETE' and not missing(d) for d in current.values())
    newly=sum(r['status']=='complete' for r in rows)
    if complete_now!=first['complete_before']+newly:raise ValueError('Unrelated completeness changes prevent an accurate combined comparison')
    report.update(results=rows,done=len(rows),newly_complete=newly,improved_stocks=sum(bool(r['recovered']) for r in rows),
                  recovered_fields=sum(len(r['recovered']) for r in rows),complete_now=complete_now,incomplete_now=len(current)-complete_now,
                  errors=sum(r['status']=='error' for r in rows),annual_reports_verified=verified,source_issue_stocks=issues,
                  bse_checked=bse,company_sites_checked=company,report_provider_counts=dict(providers),quality=followup.get('quality'),
                  updated_at=followup['updated_at'],finished_at=followup['finished_at'],
                  passes=[{k:r[k] for k in ('started_at','finished_at','total','done','newly_complete','recovered_fields')} for r in (first,followup)])
    report['note']='Combined balance-sheet and annual income repair results, measured against the original incomplete-stock baseline. '+first.get('note','')
    return report
