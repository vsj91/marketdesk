"""REIT finance costs, borrowings and leases from audited NSE REIT results filings.

Listed REITs file audited results in NSE's "Financial Results - REITs or InvITs"
(RESULTIR) iXBRL template, which prints finance costs, current and non-current
borrowings and lease liabilities, cash, unit capital and total equity. NSE's
filing search does not index REITs by symbol, so the audited filings are named
in REIT_FILINGS below: each is only a pointer. Symbol/ISIN identity, the audited
12-month column, basis, period and unit are verified from the filing itself.

Values fill only missing inputs of a record whose period, basis and units match,
and only when cash, equity and profit before tax agree on at least two anchors."""
import copy,hashlib,json,re
from datetime import datetime,timezone
from .bank_inputs import SCALES,_close,_age
from .storage import ROOT,now,number

SOURCE='nse_audited_reit_filing'
FILLABLE=('finance_costs','total_borrowings','total_lease_liabilities')
# Reviewed NSE archive URLs of audited FY2026 REIT results (found via NSE's archive).
REIT_FILINGS={
 'EMBASSY':['https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_RESULTIR_166646_10062026114220_iXBRL_WEB.html'],
 'BIRET':['https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_RESULTIR_156966_12052026182031_iXBRL_WEB.html',
          'https://nsearchives.nseindia.com/corporate/ixbrl/INTEGRATED_FILING_RESULTIR_156964_12052026181921_iXBRL_WEB.html'],
}


def reit_annual(html,symbol,isin,url):
    from .fallbacks import Tables,filing_identity
    if len(html.encode())>3_000_000:raise ValueError('NSE document exceeds parsing limit')
    parser=Tables();parser.feed(html);tables=parser.tables
    def matches(rows,label):return [row[i+1:] for row in rows for i,cell in enumerate(row) if cell.casefold()==label.casefold()]
    def starts(rows,prefix):return [row[1:] for row in rows if row and row[0].startswith(prefix)]
    def one(found,label):
        if len(found)!=1:raise ValueError('Missing/ambiguous NSE REIT field: '+label)
        return found[0]
    meta=[t for t in tables if matches(t,'NSE Symbol') and matches(t,'Class of security - REITs or INVITs')]
    if len(meta)!=1:raise ValueError('Unsupported or ambiguous NSE REIT template')
    meta=meta[0];scalar=lambda label:one(matches(meta,label),label)[0]
    identity=filing_identity(scalar('NSE Symbol'),scalar('ISIN'),symbol,isin)
    if not identity or scalar('Class of security - REITs or INVITs')!='REITs':raise ValueError('NSE filing identity mismatch')
    unit=scalar('Level of rounding')
    if scalar('Description of presentation currency')!='INR' or unit not in SCALES:raise ValueError('NSE currency/scale not verified')
    scale=SCALES[unit];basis=scalar('Nature of report standalone or consolidated').lower()
    if basis not in ('standalone','consolidated'):raise ValueError('Unknown NSE statement basis')
    def day(value):
        for form in ('%d-%m-%Y','%d-%b-%Y'):
            try:return datetime.strptime(value,form).date()
            except ValueError:pass
        raise ValueError('NSE reporting date not verified')
    start=day(scalar('Date of start of financial year'));end=day(scalar('Date of end of financial year'))
    if not 364<=(end-start).days<=366 or end>datetime.now(timezone.utc).date():raise ValueError('NSE annual period not verified')
    if one(starts(meta,'Whether results are audited or unaudited for the Year to date'),'year-to-date audit')[0]!='Audited':raise ValueError('NSE annual filing is not audited')
    def column(rows):
        s=one(matches(rows,'Date of start of reporting period'),'start');e=one(matches(rows,'Date of end of reporting period'),'end')
        a=one(matches(rows,'Whether results are audited or unaudited'),'audit');b=one(matches(rows,'Nature of report standalone or consolidated'),'basis')
        if len({len(s),len(e),len(a),len(b)})!=1:raise ValueError('NSE column dimensions mismatch')
        hits=[i for i,x in enumerate(s) if day(x)==start and day(e[i])==end and a[i]=='Audited' and b[i].lower()==basis]
        if len(hits)!=1:raise ValueError('NSE annual column not unique')
        return hits[0],len(s)
    sources={}
    def get(rows,label,col,key,required=True):
        cells=one(matches(rows,label),label)
        if len(cells)!=col[1]:raise ValueError('NSE value column mismatch: '+label)
        text=cells[col[0]].replace(',','').strip()
        if re.fullmatch(r'\(\d+(?:\.\d+)?\)',text):text='-'+text[1:-1]
        value=number(text) if text else None
        if value is None:
            if required:raise ValueError('NSE REIT value not numeric: '+label)
            return None
        sources[key]={'source':SOURCE,'source_url':url,'label':label,'reported_value':text,'source_units':unit,'conversion_to_crore':scale,
                      'rounding_quantum':scale*10**(-len(text.split('.')[1]) if '.' in text else 0),'period_end':end.isoformat(),'statement_type':basis}
        return value*scale
    income=[t for t in tables if matches(t,'Finance costs') and matches(t,'Total profit/(loss) before tax')]
    balance=[t for t in tables if matches(t,'Borrowings, non-current') and matches(t,'Cash and cash equivalents')]
    if len(income)!=1 or len(balance)!=1:raise ValueError('Unsupported or ambiguous NSE REIT statements')
    ic,bc=column(income[0]),column(balance[0])
    out={'finance_costs':get(income[0],'Finance costs',ic,'finance_costs'),'profit_before_tax':get(income[0],'Total profit/(loss) before tax',ic,'profit_before_tax'),
         'cash':get(balance[0],'Cash and cash equivalents',bc,'cash'),'equity':get(balance[0],'Total Unit Capital',bc,'equity'),
         'total_assets':get(balance[0],'Total assets',bc,'total_assets'),'total_liabilities':get(balance[0],'Total liabilities',bc,'total_liabilities')}
    parts={}
    for key,label in (('borrowings_nc','Borrowings, non-current'),('borrowings_c','Borrowings, current'),('lease_nc','Lease liabilities, non-current'),('lease_c','Lease liabilities, current')):
        parts[key]=get(balance[0],label,bc,key,False)
    if any(v is not None and v<0 for v in list(parts.values())+[out['finance_costs'],out['cash']]):raise ValueError('Negative REIT input requires review')
    # Both components must be printed numerically; an unprinted part is unknown, not zero.
    out['borrowings']=parts['borrowings_nc']+parts['borrowings_c'] if None not in (parts['borrowings_nc'],parts['borrowings_c']) else None
    out['leases']=parts['lease_nc']+parts['lease_c'] if None not in (parts['lease_nc'],parts['lease_c']) else None
    out.update(parts=parts,period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',sources=sources,filing_identity=identity)
    return out


def apply_reit_inputs(details,proof,symbol,isin):
    if proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin):raise ValueError('REIT filing identity not verified')
    reit=proof['reit'];out=copy.deepcopy(details);latest=out.get('latest') or {}
    if (latest.get('period'),latest.get('statement_type'),latest.get('units_in'))!=(reit['period'],reit['statement_type'],'crore'):raise ValueError('REIT filing period, basis or units differ from the selected record')
    q=lambda k:reit['sources'][k]['rounding_quantum']
    matched=[name for name,field,key in (('cash_and_cash_equivalents','cash_and_cash_equivalents','cash'),('equity','equity','equity'),('profit_before_tax','profit_before_tax','profit_before_tax'))
             if _close(latest.get(field),reit[key],q(key))]
    # The provider derives equity as total assets - total liabilities; its own inputs are anchors too.
    inputs=(((latest.get('sources') or {}).get('equity') or {}).get('inputs') or {})
    for name,key in (('total_assets','_total_assets'),('total_liabilities','_total_liabilities')):
        if _close((inputs.get(key) or {}).get('value'),reit[name],q(name)):matched.append(name)
    if len(matched)<2:raise ValueError('Fewer than two REIT anchors match: '+','.join(matched))
    stamp={'source_url':proof['source_url'],'raw_sha256':proof['raw_sha256'],'observed_at':proof['observed_at'],'matched_anchors':matched}
    filled=[]
    for key,field in (('finance_costs','finance_costs'),('total_borrowings','borrowings'),('total_lease_liabilities','leases')):
        if reit.get(field) is None:continue
        if latest.get(key) is None or (latest.get('sources') or {}).get(key,{}).get('source')==SOURCE:
            latest[key]=reit[field];filled.append(key)
            label={'finance_costs':'Finance costs','borrowings':'Borrowings, non-current + Borrowings, current','leases':'Lease liabilities, non-current + Lease liabilities, current'}[field]
            latest.setdefault('sources',{})[key]={'source':SOURCE,**stamp,'label':label,'value':reit[field]}
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    for holder in (out,latest):
        if 'missing_fields' in holder:holder['missing_fields']=[k for k in holder['missing_fields'] if k not in filled]
    out['latest']=latest
    if out.get('sector') and all(latest.get(k) is not None for k in FUNDAMENTAL_REQUIRED_FIELDS):out['status']='COMPLETE'
    out['reit_inputs']={k:v for k,v in proof.items() if k!='reit'}|{'filled':filled,'matched_anchors':matched}
    return out


def fetch_reit_inputs(symbol,isin,details,client=None):
    from .financial_sources import request
    from .fallbacks import Fallbacks
    from .intraday_data import atomic_json
    urls=REIT_FILINGS.get(symbol)
    if not urls:raise ValueError('No reviewed audited REIT filing')
    client=client or Fallbacks();latest=details.get('latest') or {};errors=[]
    folder=ROOT/'data/fundamental-repair/sources'/symbol;folder.mkdir(parents=True,exist_ok=True)
    for url in urls:
        cache=folder/(hashlib.sha256(url.encode()).hexdigest()[:20]+'.json')
        record=json.loads(cache.read_text()) if cache.exists() else {}
        if record.get('source_url')!=url or not 0<=_age(record.get('observed_at'))<7*86400:
            response=request(client.session,url);record={'source_url':url,'observed_at':now(),'html':response.text};atomic_json(cache,record)
        try:reit=reit_annual(record['html'],symbol,isin,url)
        except ValueError as error:errors.append(str(error));continue
        if (reit['period'],reit['statement_type'])!=(latest.get('period'),latest.get('statement_type')):errors.append('Filing period or basis differs');continue
        content=record['html'].encode('utf-8');digest=hashlib.sha256(content).hexdigest()
        raw=ROOT/'data/filings'/symbol/(digest+'.html');raw.parent.mkdir(parents=True,exist_ok=True)
        if not raw.exists():raw.write_bytes(content)
        return {'status':'verified','symbol':symbol,'isin':isin,'source_url':url,'raw_sha256':digest,'observed_at':record['observed_at'],'reit':reit}
    raise ValueError('; '.join(dict.fromkeys(errors)) or 'No matching audited REIT filing')


def retain_reit_inputs(details,previous,symbol,isin):
    proof=(previous or {}).get('reit_inputs')
    if not proof or proof.get('status')!='verified':return details
    try:
        raw=(ROOT/'data/filings'/symbol/(proof['raw_sha256']+'.html')).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=proof['raw_sha256']:return details
        return apply_reit_inputs(details,{**proof,'reit':reit_annual(raw.decode('utf-8'),symbol,isin,proof['source_url'])},symbol,isin)
    except (ValueError,KeyError,OSError,TypeError):return details


def reit_refresh_due(symbol,details):
    if symbol not in REIT_FILINGS:return False
    latest=(details or {}).get('latest') or {}
    if any(latest.get(k) is None for k in FILLABLE):return True
    stamp=((details or {}).get('reit_inputs') or {}).get('observed_at')
    try:return (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).days>=20
    except (TypeError,ValueError):return False
