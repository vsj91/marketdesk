"""Evidence-based financial repair: sources are tried, never invented or zero-filled."""
import copy
from .quality import financial_reasons
from .storage import now
from datetime import datetime,timezone


def repair_extra(symbol,isin,details):
    """Recover like-for-like annual growth and reverify eligible source conflicts.

    Primary order: audited NSE comparative filings, then company annual-report
    income tables from NSE, reviewed company websites and ISIN-matched BSE
    archives. AnnualReportSources handles the same balance-sheet alternatives.
    Yahoo's unspecified statement basis stays supplemental.
    """
    from .growth_repair import recover,checked_pair
    from .report_growth import recover_report_growth,apply_growth
    from .conflict_repair import verify_nse,reconcile
    out=copy.deepcopy(details);attempts=[]
    # Banks: audited NSE banking filings supply interest expenses and borrowings, or the
    # whole record when the provider has no current annual period.
    if 'bank' in (out.get('sector') or '').lower():
        from .bank_inputs import bank_refresh_due,fetch_bank_inputs,apply_bank_inputs,fetch_bank_record,bank_record
        try:
            stale=not (out.get('latest') or {}).get('period') or bool(out.get('bank_record'))
            if not stale:
                try:stale=(datetime.now(timezone.utc).date()-datetime.strptime(out['latest']['period'],'%b %Y').date()).days>400
                except ValueError:stale=True
            if stale:
                try:out=bank_record(out,fetch_bank_record(symbol,isin,out),symbol,isin)
                except ValueError as error:
                    if 'not older' not in str(error):raise
                    out=apply_bank_inputs(out,fetch_bank_inputs(symbol,isin,out),symbol,isin)
                attempts.append({'source':'Audited NSE banking filing','status':'verified'})
            elif bank_refresh_due(out):
                out=apply_bank_inputs(out,fetch_bank_inputs(symbol,isin,out),symbol,isin)
                attempts.append({'source':'Audited NSE banking filing','status':'verified'})
        except Exception as error:attempts.append({'source':'Audited NSE banking filing','status':'unresolved','reason':str(error)[:180]})
    latest=out.get('latest') or {}
    if not latest.get('period') or latest.get('statement_type') not in ('consolidated','standalone'):
        out['automatic_repair']={'checked_at':now(),'attempts':attempts+[{'source':'Audited annual context','status':'unresolved','reason':'A verified annual period and statement basis are required before growth repair'}]}
        return out
    reasons=financial_reasons(out,now(),datetime.now(timezone.utc))
    if any(r in reasons for r in ('Comparable annual growth inputs missing','Growth inputs do not match annual period, basis and units')):
        try:
            evidence=recover(symbol,isin,out)
            out['summary']=checked_pair(out,evidence['current'],evidence['prior']);out['growth_repair']=evidence
            attempts.append({'source':'NSE audited comparative','status':'verified'})
        except Exception as error:
            attempts.append({'source':'NSE audited comparative','status':'unresolved','reason':str(error)[:180]})
            try:
                proof=recover_report_growth(symbol,isin,out);out=apply_growth(out,proof,symbol,isin)
                attempts.append({'source':'Company annual report','status':'verified'})
            except Exception as error:attempts.append({'source':'Company annual report','status':'unresolved','reason':str(error)[:180]})
    if out.get('source_conflicts'):
        try:
            proof=verify_nse(symbol,isin,out);out,resolved=reconcile(out,proof)
            attempts.append({'source':'Audited NSE conflict verification','status':'verified' if resolved else 'unresolved'})
        except Exception as error:attempts.append({'source':'Audited NSE conflict verification','status':'unresolved','reason':str(error)[:180]})
    out['automatic_repair']={'checked_at':now(),'attempts':attempts}
    return out
