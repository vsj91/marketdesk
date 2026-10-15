"""Recover comparable annual income from independently audited NSE filings."""
import copy,hashlib,json,math
from datetime import datetime,timezone
from urllib.parse import urlsplit
from .storage import ROOT,now
from .fallbacks import Fallbacks,nse_annual
from .financial_sources import request
from .intraday_data import atomic_json


def checked_pair(details,current,prior):
    latest=details.get('latest') or {}
    for key in ('period','statement_type','units_in'):
        if current.get(key)!=latest.get(key):raise ValueError('Current income context does not match selected annual')
    for key in ('profit_before_tax','finance_costs'):
        a,b=current.get(key),latest.get(key)
        if a is None or b is None or abs(a-b)>max(.015,abs(b)*.00002):raise ValueError('Current income anchors do not match')
    end=datetime.fromisoformat(current['period_end']).date()
    if prior.get('period_end')!=end.replace(year=end.year-1).isoformat():raise ValueError('Prior income is not the preceding annual period')
    if any(prior.get(k)!=current.get(k) for k in ('statement_type','units_in')):raise ValueError('Prior income basis or units differ')
    summary=copy.deepcopy(details.get('summary') or {})
    summary.update(statement_type=current['statement_type'],units=current['units_in'],frequency='annual')
    for key in ('revenue','net_profit'):
        a,b=current.get(key),prior.get(key)
        if any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in (a,b)) or b<=0:raise ValueError('Growth needs numeric income and a positive prior-year denominator')
        summary[key]=a;summary[key+'_period']=current['period'];summary[key+'_growth_yoy']=(a/b-1)*100
        summary.setdefault('growth_sources',{})[key]='nse_audited_filing'
        summary.setdefault('growth_observed_at',{})[key]=[current['observed_at'],prior['observed_at']]
    return summary


def extract_saved(symbol,isin,record):
    path=ROOT/'data/filings'/symbol/(record['raw_sha256']+'.html')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['raw_sha256']:raise ValueError('Saved income evidence hash mismatch')
    offset=record.get('year_offset',0)
    row=nse_annual(raw.decode('utf-8'),symbol,isin,record['source_url'],year_offset=offset,income_only=True)
    row.update({k:record[k] for k in ('raw_sha256','source_url','observed_at')})
    row['year_offset']=offset
    return row


def recover(symbol,isin,details):
    latest=details.get('latest') or {}
    if details.get('selected_source')!='nse_audited_filing':raise ValueError('Selected annual is not a verified NSE filing')
    if not all(latest.get(k) for k in ('raw_sha256','source_url','observed_at')):
        # Legacy complete rows omitted file metadata. Recover only after an
        # independent identity/context check that preserves every selected value.
        from .conflict_repair import verify_nse
        proof=verify_nse(symbol,isin,details)
        latest={**latest,**{k:proof[k] for k in ('raw_sha256','source_url','observed_at')}}
    current=extract_saved(symbol,isin,latest)
    # Prefer the audited comparative column in the same filing. Its own date,
    # audit status and basis must match; retain its original observation time.
    try:comparative=extract_saved(symbol,isin,{**latest,'year_offset':-1})
    except ValueError:comparative=None
    if comparative is not None and all(comparative.get(k) is not None for k in ('revenue','net_profit')):
        summary=checked_pair(details,current,comparative)
        return {'symbol':symbol,'isin':isin,'current':current,'prior':comparative,'summary':summary,'checked_at':now(),'status':'verified','method':'same_filing_audited_comparative'}
    prior_end=datetime.fromisoformat(current['period_end']).date().replace(year=datetime.fromisoformat(current['period_end']).year-1).isoformat()
    index=json.loads((ROOT/'data/fundamental-repair/sources'/symbol/'index.json').read_text())
    if index.get('symbol')!=symbol:raise ValueError('NSE index identity mismatch')
    rows=index['payload'];rows=rows if isinstance(rows,list) else rows.get('data',[])
    candidate=None
    for row in rows:
        try:day=datetime.strptime(row.get('qe_Date',''),'%d-%b-%Y').date().isoformat()
        except ValueError:continue
        url=row.get('ixbrl') or '';u=urlsplit(url)
        if row.get('symbol')==symbol and day==prior_end and (row.get('consolidated') or '').lower()==current['statement_type'] and u.scheme=='https' and u.hostname=='nsearchives.nseindia.com' and u.path.startswith('/corporate/ixbrl/'):
            candidate=url;break
    if not candidate:raise ValueError('Matching prior-year filing absent from saved NSE index')
    cache=ROOT/'data/investor-growth/sources'/symbol/(hashlib.sha256(candidate.encode()).hexdigest()+'.json')
    saved=json.loads(cache.read_text()) if cache.exists() else {}
    if not saved or saved.get('source_url')!=candidate or not 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['observed_at'])).total_seconds()<86400:
        response=request(Fallbacks().session,candidate);raw=response.content;digest=hashlib.sha256(raw).hexdigest()
        # Validate identity, audited annual context and bounded size before saving.
        nse_annual(raw.decode('utf-8'),symbol,isin,candidate,income_only=True)
        path=ROOT/'data/filings'/symbol/(digest+'.html');path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():path.write_bytes(raw)
        saved={'raw_sha256':digest,'source_url':candidate,'observed_at':now()};atomic_json(cache,saved)
    prior=extract_saved(symbol,isin,saved);summary=checked_pair(details,current,prior)
    return {'symbol':symbol,'isin':isin,'current':current,'prior':prior,'summary':summary,'checked_at':now(),'status':'verified'}


def retain_growth(details,previous,symbol,isin):
    if details.get('selected_source')!='nse_audited_filing':return details
    report=previous.get('growth_repair') or {}
    if report.get('status')!='verified' or (report.get('symbol'),report.get('isin'))!=(symbol,isin):return details
    try:
        current=extract_saved(symbol,isin,report['current']);prior=extract_saved(symbol,isin,report['prior'])
        summary=checked_pair(details,current,prior)
    except (ValueError,KeyError,OSError):return details
    details['summary']=summary;details['growth_repair']=copy.deepcopy(report)
    return details
