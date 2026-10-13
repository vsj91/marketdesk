"""Resolve vendor discrepancies only after verifying the selected audited NSE filing."""
import copy,hashlib,json,math
from datetime import datetime,timezone
from urllib.parse import urlsplit
from .storage import ROOT,now
from .fallbacks import Fallbacks,nse_annual
from .financial_sources import request
from .intraday_data import atomic_json

FIELDS=('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents')
def context(details):
    latest=details.get('latest') or {}
    return {k:latest.get(k) for k in ('period','statement_type','units_in','raw_sha256',*FIELDS)}
def same(a,b):
    return all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (a,b)) and abs(a-b)<=max(1e-8,abs(b)*1e-10)
def validate_context(details,row):
    latest=details.get('latest') or {}
    if details.get('selected_source')!='nse_audited_filing':raise ValueError('Selected annual is not an audited NSE filing')
    if any(latest.get(k)!=row.get(k) for k in ('period','statement_type','units_in')):raise ValueError('NSE period, basis or units differ from selected annual')
    common=[k for k in FIELDS if latest.get(k) is not None and row.get(k) is not None]
    if len(common)<2 or any(not same(latest[k],row[k]) for k in common):raise ValueError('Fresh audited filing disagrees with selected annual values; review required')

def verify_nse(symbol,isin,details):
    latest=details.get('latest') or {}
    if details.get('selected_source')!='nse_audited_filing':
        raise ValueError('Selected annual is not an audited NSE filing')
    client=Fallbacks();index_path=ROOT/'data/fundamental-repair/sources'/symbol/'index.json'
    index=json.loads(index_path.read_text()) if index_path.exists() else {}
    try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(index['observed_at'])).total_seconds()
    except (KeyError,ValueError):age=1e10
    if not 0<=age<86400:
        response=request(client.session,'https://www.nseindia.com/api/integrated-filing-results',params={'symbol':symbol})
        index={'symbol':symbol,'observed_at':now(),'payload':response.json()};atomic_json(index_path,index)
    if index.get('symbol')!=symbol:raise ValueError('NSE index identity mismatch')
    rows=index['payload'];rows=rows if isinstance(rows,list) else rows.get('data',[])
    matched=[]
    for r in rows:
        try:period=datetime.strptime(r.get('qe_Date',''),'%d-%b-%Y').strftime('%b %Y')
        except ValueError:continue
        if r.get('symbol')==symbol and period==latest.get('period') and (r.get('consolidated') or '').lower()==latest.get('statement_type') and r.get('ixbrl'):matched.append(r['ixbrl'])
    if not matched:raise ValueError('No matching annual filing in the NSE index')
    # Legacy selected rows may lack a top-level URL. Recover it from the exact
    # symbol/period/basis index entry, then independently verify every shared value.
    url=matched[0];u=urlsplit(url)
    if u.scheme!='https' or u.hostname!='nsearchives.nseindia.com' or not u.path.startswith('/corporate/ixbrl/'):
        raise ValueError('NSE index filing URL is outside the allowed archive')
    response=request(client.session,url);raw=response.content
    row=nse_annual(raw.decode('utf-8'),symbol,isin,url);validate_context(details,row)
    digest=hashlib.sha256(raw).hexdigest();path=ROOT/'data/filings'/symbol/(digest+'.html');path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():path.write_bytes(raw)
    return {'symbol':symbol,'isin':isin,'source_url':url,'raw_sha256':digest,'evidence_path':str(path.relative_to(ROOT)),'observed_at':now(),'index_observed_at':index['observed_at'],'annual':row}

def reconcile(details,evidence):
    validate_context(details,evidence['annual']);out=copy.deepcopy(details);left=[];resolved=[]
    for conflict in details.get('source_conflicts',[]):
        field=conflict.get('field');latest=details['latest'];row=evidence['annual']
        eligible=(field in FIELDS and 'upstox' in conflict and 'nse' in conflict and 'annual_report' not in conflict and conflict.get('selected')=='nse_audited_filing' and conflict.get('period')==latest.get('period') and conflict.get('units')==latest.get('units_in') and same(conflict.get('nse'),latest.get(field)) and same(row.get(field),latest.get(field)))
        if not eligible:left.append(copy.deepcopy(conflict));continue
        record={'conflict':copy.deepcopy(conflict),'selected_context':context(details),'evidence':copy.deepcopy(evidence),'resolution':'Audited NSE filing reverified; selected official value retained over vendor value','resolved_at':now()}
        resolved.append(record)
    out['source_conflicts']=left
    history=out.setdefault('source_conflict_resolutions',[])
    for record in resolved:
        if not any(r['conflict']==record['conflict'] and r['selected_context']==record['selected_context'] for r in history):history.append(record)
    return out,resolved

def retain_resolutions(details,previous,symbol,isin):
    """A changed discrepancy, annual context or evidence requires re-verification."""
    out=copy.deepcopy(details)
    for record in previous.get('source_conflict_resolutions',[]):
        if record.get('selected_context')!=context(out) or record.get('conflict') not in out.get('source_conflicts',[]):continue
        evidence=record.get('evidence') or {}
        if (evidence.get('symbol'),evidence.get('isin'))!=(symbol,isin):continue
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(evidence['observed_at'])).total_seconds()
            if not 0<=age<=30*86400:continue
            raw=(ROOT/evidence['evidence_path']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=evidence['raw_sha256']:continue
            parsed=nse_annual(raw.decode('utf-8'),symbol,isin,evidence['source_url']);validate_context(out,parsed)
            # Restrict each retained resolution to its original exact discrepancy.
            probe=copy.deepcopy(out);probe['source_conflicts']=[record['conflict']]
            _,accepted=reconcile(probe,{**evidence,'annual':parsed})
            if not accepted:continue
        except (ValueError,KeyError,OSError):continue
        out['source_conflicts'].remove(record['conflict'])
        history=out.setdefault('source_conflict_resolutions',[])
        if record not in history:history.append(copy.deepcopy(record))
    # Preserve the audit trail if a normal refresh introduces no new conflict.
    for record in previous.get('source_conflict_resolutions',[]):
        if record not in out.setdefault('source_conflict_resolutions',[]):out['source_conflict_resolutions'].append(copy.deepcopy(record))
    return out
