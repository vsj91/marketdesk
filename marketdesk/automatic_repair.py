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
    # Insurers: audited NSE insurance filings, then the annual report's Form A-PL for
    # interest on subordinated debt (and growth for a first-year listing).
    if 'insurance' in (out.get('sector') or '').lower():
        from .insurer_inputs import insurer_refresh_due,fetch_insurer_record,insurer_record,fetch_insurer_report,apply_insurer_report
        try:
            if insurer_refresh_due(out):out=insurer_record(out,fetch_insurer_record(symbol,isin,out),symbol,isin)
            attempts.append({'source':'Audited NSE insurance filing','status':'verified'})
            reasons=financial_reasons(out,now(),datetime.now(timezone.utc))
            if out.get('insurer_record') and ('Annual financial inputs incomplete' in reasons or 'Comparable annual growth inputs missing' in reasons):
                out=apply_insurer_report(out,fetch_insurer_report(symbol,isin,out),symbol,isin)
                attempts.append({'source':'Insurer annual report Form A-PL','status':'verified'})
        except Exception as error:attempts.append({'source':'Audited insurance sources','status':'unresolved','reason':str(error)[:180]})
    from .reit_inputs import reit_refresh_due,fetch_reit_inputs,apply_reit_inputs
    if reit_refresh_due(symbol,out):
        try:
            out=apply_reit_inputs(out,fetch_reit_inputs(symbol,isin,out),symbol,isin)
            attempts.append({'source':'Audited NSE REIT filing','status':'verified'})
        except Exception as error:attempts.append({'source':'Audited NSE REIT filing','status':'unresolved','reason':str(error)[:180]})
    # No exchange or provider annual record at all: build one from the audited annual
    # report, anchored on independent same-period figures; refresh it before 30 days.
    record=out.get('report_record') or {}
    try:record_age=(datetime.now(timezone.utc)-datetime.fromisoformat(record['report']['observed_at'])).days
    except (KeyError,TypeError,ValueError):record_age=None
    if not (out.get('latest') or {}).get('period') or (record and (record_age is None or record_age>=20)):
        from .company_record import build_report_record
        try:
            out=build_report_record(symbol,isin,out)
            attempts.append({'source':'Audited company annual report with independent anchors','status':'verified'})
        except Exception as error:attempts.append({'source':'Audited company annual report with independent anchors','status':'unresolved','reason':str(error)[:180]})
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
