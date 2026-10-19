"""A whole annual record from the company's audited annual report, when no exchange
filing or provider annual record exists at all (for example a company whose results
are filed only on BSE).

Every value comes from the audited report itself. An independent same-period source
(Yahoo's annual statements) supplies only anchors: the report's cash and equity must
match it to the paisa-level tolerance used elsewhere, and its profit before tax must
match before the P&L is read. Growth comes from the report's own comparative column."""
import copy,json
from datetime import datetime,timezone
from .storage import ROOT,now,number

SOURCE='company_annual_report'
CRORE=1e7


def yahoo_anchors(symbol):
    """Latest INR annual Yahoo row (cached fallback, at most 30 days old) as crore anchors."""
    path=ROOT/'data/fallbacks'/(symbol+'.json')
    cached=json.loads(path.read_text()) if path.exists() else {}
    yahoo=cached.get('yahoo') or {}
    try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(yahoo['observed_at'])).days
    except (KeyError,TypeError,ValueError):age=999
    if yahoo.get('status')!='available' or age>30 or yahoo.get('symbol')!=symbol+'.NS':
        from .fallbacks import Fallbacks
        yahoo=Fallbacks().yahoo(symbol)
    if yahoo.get('status')!='available' or yahoo.get('symbol')!=symbol+'.NS':raise ValueError('Independent annual anchors unavailable')
    rows=[r for r in yahoo.get('annual',[]) if r.get('currency')=='INR' and all(number(r.get(k)) is not None for k in ('profit_before_tax','equity','cash_and_cash_equivalents'))]
    if not rows:raise ValueError('Independent annual anchors incomplete')
    row=max(rows,key=lambda r:r['period_end'])
    if not 0<=(datetime.now(timezone.utc).date()-datetime.fromisoformat(row['period_end']).date()).days<=550:raise ValueError('Independent annual anchors are stale')
    return {'period':row['period'],'observed_at':yahoo.get('observed_at'),'source':'yahoo_finance','values':{k:row[k]/CRORE for k in ('profit_before_tax','equity','cash_and_cash_equivalents')}}


def build_report_record(symbol,isin,details):
    from .annual_reports import AnnualReportSources
    from .report_growth import recover_report_growth,apply_growth
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    if (details.get('latest') or {}).get('period') and not details.get('report_record'):raise ValueError('An exchange or provider annual record exists')
    anchors=yahoo_anchors(symbol);errors=[]
    for basis in ('standalone','consolidated'):
        probe={'period':anchors['period'],'statement_type':basis,'units_in':'crore','finance_costs':None,'total_borrowings':None,'total_lease_liabilities':None,'sources':{},**anchors['values']}
        report=AnnualReportSources().fetch(symbol,isin,{'latest':probe,'sector':details.get('sector')})
        e=report.get('evidence') or {}
        if report.get('status')!='verified':errors.append(basis+': '+str(report.get('reason'))[:160]);continue
        if set(e.get('matched_fields',[]))<{'cash_and_cash_equivalents','equity'} or not (e.get('income_evidence') or {}).get('matched_profit_before_tax'):
            errors.append(basis+': report does not match independent cash, equity and PBT');continue
        break
    else:raise ValueError('; '.join(errors)[:400])
    stamp={'source':SOURCE,'source_url':report['source_url'],'observed_at':report['observed_at'],'raw_sha256':report['raw_sha256'],'evidence_path':report['evidence_path'],'excerpt_sha256':report['excerpt_sha256']}
    values={'profit_before_tax':e['income_evidence']['matched_profit_before_tax'],'finance_costs':e['values'].get('finance_costs'),'equity':e['balance_equity'],
            'total_borrowings':e['values'].get('total_borrowings'),'total_lease_liabilities':e['values'].get('total_lease_liabilities'),'cash_and_cash_equivalents':e['values'].get('cash_and_cash_equivalents')}
    latest={**values,'period':e['period'],'period_end':None,'statement_type':e['statement_type'],'units_in':'crore','reporting_frequency':'annual','source':SOURCE,
            'observed_at':report['observed_at'],'missing_fields':[k for k,v in values.items() if v is None],
            'sources':{k:{**stamp,'value':v,'pages':e.get('pages')} for k,v in values.items() if v is not None},
            'independent_anchors':{**anchors,'matched':['cash_and_cash_equivalents','equity','profit_before_tax']}}
    out=copy.deepcopy(details)
    out.update(latest=latest,selected_source=SOURCE,missing_fields=latest['missing_fields']+([] if out.get('sector') else ['sector']))
    out['status']='COMPLETE' if not out['missing_fields'] else 'PARTIAL'
    out['summary']={'statement_type':latest['statement_type'],'units':'crore','frequency':'annual'}
    try:
        growth=recover_report_growth(symbol,isin,out);out=apply_growth(out,growth,symbol,isin)
    except ValueError as error:out['report_record_growth_error']=str(error)[:300]
    out['report_record']={'report':{k:v for k,v in report.items()},'anchors':anchors,'built_at':now()}
    return out


def retain_report_record(details,previous,symbol,isin):
    """Keep a report-built record across empty provider re-imports while its evidence is intact."""
    import hashlib
    record=(previous or {}).get('report_record')
    if not record or (details.get('latest') or {}).get('period'):return details
    try:
        path=ROOT/record['report']['evidence_path']
        if hashlib.sha256(path.read_bytes()).hexdigest()!=record['report']['excerpt_sha256']:return details
    except (KeyError,OSError):return details
    out=copy.deepcopy(details)
    for key in ('latest','summary','selected_source','missing_fields','status','report_record','company_growth_repair'):
        if key in previous:out[key]=copy.deepcopy(previous[key])
    return out
