"""Bank finance cost and borrowings from audited NSE banking filings.

Indian banks report in the Third Schedule format of the Banking Regulation Act
(Indian GAAP; RBI has not moved banks to Ind AS). That format prints "Interest
expenses" -- a bank's finance cost -- and "Borrowings" separately from deposits.
It has no lease-liability line: lessee operating leases are expensed under AS 19.

Values only fill missing inputs of a record whose period, basis and units match
the audited filing, and only when cash, total assets and profit before tax agree
on at least two anchors. A lease line is recorded as not applicable, never zero,
and only when the audited balance sheet carries no lease caption at all."""
import copy,hashlib,json,re
from datetime import datetime,timezone
from urllib.parse import urlsplit
from .storage import ROOT,now,number

SOURCE='nse_audited_banking_filing'
FRAMEWORK='Banking Regulation Act, 1949 Third Schedule (Indian GAAP)'
SCALES={'Lakhs':.01,'Crores':1.,'Rupees':1e-7}
REFRESH_DAYS=20
FILLABLE=('finance_costs','total_borrowings')


def bank_annual(html,symbol,isin,url):
    """One audited 12-month column of an NSE BANKING iXBRL filing, in crore."""
    from .fallbacks import Tables
    if len(html.encode())>2_000_000:raise ValueError('NSE document exceeds parsing limit')
    parser=Tables();parser.feed(html);tables=parser.tables
    def matches(rows,label):return [row[i+1:] for row in rows for i,cell in enumerate(row) if cell.casefold()==label.casefold()]
    def values(rows,label):
        found=matches(rows,label)
        if len(found)!=1:raise ValueError('Missing/ambiguous NSE banking field: '+label)
        return found[0]
    def scalar(rows,label):
        v=values(rows,label)
        if len(v)!=1:raise ValueError('Ambiguous NSE banking metadata: '+label)
        return v[0]
    def choose(*labels):
        found=[t for t in tables if all(matches(t,k) for k in labels)]
        if len(found)!=1:raise ValueError('Unsupported or ambiguous NSE banking template')
        return found[0]
    meta=choose('NSE Symbol','ISIN','Name of bank')
    if scalar(meta,'NSE Symbol')!=symbol or scalar(meta,'ISIN')!=isin or scalar(meta,'Class of security')!='Equity':raise ValueError('NSE filing identity mismatch')
    currency=scalar(meta,'Description of presentation currency')
    # Older filings state "INR (in Actuals)" (rupees) and omit the rounding field.
    if currency=='INR (in Actuals)' and not matches(meta,'Level of rounding used in financial results'):currency,unit='INR','Rupees'
    else:unit=scalar(meta,'Level of rounding used in financial results')
    if currency!='INR' or unit not in SCALES:raise ValueError('NSE currency/scale not verified')
    scale=SCALES[unit]
    basis=scalar(meta,'Nature of report standalone or consolidated').lower()
    if basis not in ('standalone','consolidated'):raise ValueError('Unknown NSE statement basis')
    def day(value):
        for form in ('%d-%m-%Y','%d-%b-%Y'):
            try:return datetime.strptime(value,form).date()
            except ValueError:pass
        raise ValueError('NSE reporting date not verified')
    start=day(scalar(meta,'Date of start of financial year'));end=day(scalar(meta,'Date of end of financial year'))
    if not 364<=(end-start).days<=366 or end>datetime.now(timezone.utc).date():raise ValueError('NSE annual period not verified')
    if scalar(meta,'Whether results are audited or unaudited for the Year to date for current period ended/year ended')!='Audited':raise ValueError('NSE annual filing is not audited')
    income=choose('Interest expenses','Total profit (loss) from ordinary activities before tax')
    balance=choose('Deposits','Borrowings','Total Assets')
    def column(rows):
        starts=values(rows,'Date of start of reporting period');ends=values(rows,'Date of end of reporting period')
        audits=values(rows,'Whether results are audited or unaudited');bases=values(rows,'Nature of report standalone or consolidated')
        if len({len(starts),len(ends),len(audits),len(bases)})!=1:raise ValueError('NSE column dimensions mismatch')
        hits=[i for i,s in enumerate(starts) if day(s)==start and day(ends[i])==end and audits[i]=='Audited' and bases[i].lower()==basis]
        if len(hits)!=1:raise ValueError('NSE annual column not unique')
        return hits[0],len(starts)
    sources={}
    def get(rows,label,col,key,required=True):
        v=values(rows,label)
        if len(v)!=col[1]:raise ValueError('NSE value column mismatch: '+label)
        text=v[col[0]].replace(',','').strip()
        if re.fullmatch(r'\(\d+(?:\.\d+)?\)',text):text='-'+text[1:-1]
        value=number(text) if text else None
        if value is None:
            if required:raise ValueError('NSE banking value not numeric: '+label)
            return None
        sources[key]={'source':SOURCE,'source_url':url,'label':label,'reported_value':text,'source_units':unit,'conversion_to_crore':scale,
                      'rounding_quantum':scale*10**(-len(text.split('.')[1]) if '.' in text else 0),'period_end':end.isoformat(),'statement_type':basis}
        return value*scale
    ic,bc=column(income),column(balance)
    out={'interest_expenses':get(income,'Interest expenses',ic,'interest_expenses'),
         'profit_before_tax':get(income,'Total profit (loss) from ordinary activities before tax',ic,'profit_before_tax'),
         'total_income':get(income,'Total income',ic,'total_income',False),
         'net_profit':get(income,'Net profit (loss) for the period',ic,'net_profit',False)}
    for key,label in [('capital','Capital'),('reserves','Reserves and surplus'),('deposits','Deposits'),('borrowings','Borrowings'),
                      ('cash_with_rbi','Cash and Balances with Reserve Bank of India'),('bank_balances','Balances with Banks and money at call and short notice'),('total_assets','Total Assets')]:
        out[key]=get(balance,label,bc,key)
    # The audited cash-flow statement's closing cash, when printed, independently confirms balance-sheet cash.
    flows=[t for t in tables if matches(t,'Cash and cash equivalents cash flow statement at end of period')]
    out['cash_flow_closing']=None
    if len(flows)==1:
        # Optional confirmation: a cash-flow table for another period is simply not used.
        try:fc=column(flows[0])
        except ValueError:fc=None
        if fc:out['cash_flow_closing']=get(flows[0],'Cash and cash equivalents cash flow statement at end of period',fc,'cash_flow_closing',False)
    if any(out[k]<0 for k in ('interest_expenses','borrowings','cash_with_rbi','bank_balances','total_assets','deposits')):raise ValueError('Negative bank input requires review')
    captions=[cell for row in balance for cell in row if re.search(r'[a-z]',cell,re.I)]
    out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',sources=sources,
               balance_captions=captions,lease_caption_present=any(re.search(r'\blease',c,re.I) for c in captions),framework=FRAMEWORK)
    return out


def _close(a,b,quantum):
    return a is not None and b is not None and abs(a-b)<=max(.015,abs(b)*.00002,quantum/2+1e-9)


def anchors(latest,bank):
    """Independent agreement between the selected record and the audited filing."""
    q=lambda k:bank['sources'][k]['rounding_quantum']
    matched=[]
    if _close(latest.get('cash_and_cash_equivalents'),bank['cash_with_rbi']+bank['bank_balances'],q('cash_with_rbi')+q('bank_balances')):matched.append('cash_and_cash_equivalents')
    if _close(latest.get('profit_before_tax'),bank['profit_before_tax'],q('profit_before_tax')):matched.append('profit_before_tax')
    assets=(((latest.get('sources') or {}).get('equity') or {}).get('inputs') or {}).get('_total_assets',{}).get('value')
    if _close(assets,bank['total_assets'],q('total_assets')):matched.append('total_assets')
    return matched


def not_applicable(details):
    """Inputs a verified bank filing shows the reporting framework does not carry."""
    latest=(details or {}).get('latest') or {};out=set()
    for key,entry in (latest.get('not_applicable') or {}).items():
        if isinstance(entry,dict) and entry.get('source')==SOURCE and entry.get('framework')==FRAMEWORK and entry.get('period')==latest.get('period') and entry.get('statement_type')==latest.get('statement_type'):out.add(key)
    return out


def apply_bank_inputs(details,proof,symbol,isin):
    """Fill missing bank finance cost / borrowings; mark an absent lease line not applicable."""
    if proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin):raise ValueError('Bank filing identity not verified')
    bank=proof['bank'];out=copy.deepcopy(details);latest=out.get('latest') or {}
    if (latest.get('period'),latest.get('statement_type'),latest.get('units_in'))!=(bank['period'],bank['statement_type'],'crore'):raise ValueError('Bank filing period, basis or units differ from the selected record')
    matched=anchors(latest,bank)
    if len(matched)<2:raise ValueError('Fewer than two bank anchors match: '+','.join(matched))
    stamp={'source_url':proof['source_url'],'raw_sha256':proof['raw_sha256'],'observed_at':proof['observed_at'],'matched_anchors':matched}
    filled=[]
    # Selected cash that contradicts audited cash is corrected only when the balance
    # sheet and the cash-flow closing balance of the same filing agree; else refuse.
    audited_cash=bank['cash_with_rbi']+bank['bank_balances'];q=bank['sources']['cash_with_rbi']['rounding_quantum']+bank['sources']['bank_balances']['rounding_quantum']
    if 'cash_and_cash_equivalents' not in matched:
        if not _close(bank.get('cash_flow_closing'),audited_cash,q):raise ValueError('Selected cash differs from audited cash and the filing does not confirm it twice')
        previous=latest.get('cash_and_cash_equivalents');prior_source=(latest.get('sources') or {}).get('cash_and_cash_equivalents')
        if (prior_source or {}).get('source')==SOURCE:previous,prior_source=prior_source.get('corrected_from'),prior_source.get('corrected_from_source')
        latest['cash_and_cash_equivalents']=audited_cash;filled.append('cash_and_cash_equivalents')
        latest.setdefault('sources',{})['cash_and_cash_equivalents']={'source':SOURCE,**stamp,'value':audited_cash,'label':'Cash and Balances with Reserve Bank of India + Balances with Banks and money at call and short notice',
            'confirmed_by':'Cash and cash equivalents cash flow statement at end of period','corrected_from':previous,'corrected_from_source':prior_source}
    for key,field in (('finance_costs','interest_expenses'),('total_borrowings','borrowings')):
        # Refill values this source supplied earlier so their evidence age stays current.
        if latest.get(key) is None or (latest.get('sources') or {}).get(key,{}).get('source')==SOURCE:
            latest[key]=bank[field];filled.append(key)
            latest.setdefault('sources',{})[key]={**bank['sources'][field],**stamp,'value':bank[field],
                'meaning':'Bank finance cost is interest expended' if key=='finance_costs' else 'Borrowings excluding deposits, as printed'}
    if latest.get('total_lease_liabilities') is None:
        if bank['lease_caption_present']:raise ValueError('Bank balance sheet prints a lease caption; lease cannot be treated as not applicable')
        latest.setdefault('not_applicable',{})['total_lease_liabilities']={'source':SOURCE,'framework':FRAMEWORK,'period':bank['period'],'statement_type':bank['statement_type'],
            'reason':'The audited Third Schedule balance sheet has no lease-liability line; lessee operating leases are expensed under AS 19.',
            'balance_captions':bank['balance_captions'],**stamp}
    na=not_applicable(out)
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    for holder in (out,latest):
        if 'missing_fields' in holder:holder['missing_fields']=[k for k in holder['missing_fields'] if k not in filled and k not in na]
    out['latest']=latest
    if out.get('sector') and all(latest.get(k) is not None or k in na for k in FUNDAMENTAL_REQUIRED_FIELDS):out['status']='COMPLETE'
    out['bank_inputs']={k:v for k,v in proof.items() if k!='bank'}|{'filled':filled,'matched_anchors':matched}
    out['bank_inputs']['bank']={k:v for k,v in bank.items() if k!='balance_captions'}
    return out


def saved_proof(symbol,isin,proof):
    """Re-read the hash-bound saved filing; never trust stored values."""
    raw=(ROOT/'data/filings'/symbol/(proof['raw_sha256']+'.html')).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=proof['raw_sha256']:raise ValueError('Saved bank filing hash mismatch')
    return {**{k:v for k,v in proof.items() if k!='bank'},'bank':bank_annual(raw.decode('utf-8'),symbol,isin,proof['source_url'])}


def retain_bank_inputs(details,previous,symbol,isin):
    """Re-apply verified bank evidence after a provider re-import, from saved filings."""
    previous=previous or {}
    try:
        record=previous.get('bank_record')
        if record:
            proofs={'current':saved_proof(symbol,isin,record['current']),'prior':saved_proof(symbol,isin,record['prior']) if record.get('prior') else None}
            try:return bank_record(details,proofs,symbol,isin)
            # A provider record that is current again keeps only the missing-line fill.
            except ValueError:return apply_bank_inputs(details,proofs['current'],symbol,isin)
        proof=previous.get('bank_inputs')
        if proof and proof.get('status')=='verified':return apply_bank_inputs(details,saved_proof(symbol,isin,proof),symbol,isin)
    except (ValueError,KeyError,OSError,TypeError):pass
    return details


def _age(stamp):
    try:return (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError,ValueError):return 1e12


def _index(symbol,client):
    from .financial_sources import request
    from .intraday_data import atomic_json
    folder=ROOT/'data/fundamental-repair/sources'/symbol;folder.mkdir(parents=True,exist_ok=True)
    index_file=folder/'index.json';index=json.loads(index_file.read_text()) if index_file.exists() else {}
    if index.get('symbol',symbol)!=symbol or not 0<=_age(index.get('observed_at'))<86400:
        response=request(client.session,'https://www.nseindia.com/api/integrated-filing-results',params={'symbol':symbol})
        index={'symbol':symbol,'observed_at':now(),'payload':response.json()};atomic_json(index_file,index)
    rows=index['payload'];rows=rows if isinstance(rows,list) else rows.get('data',[])
    out=[]
    for row in rows:
        url=row.get('ixbrl') or '';u=urlsplit(url)
        if row.get('symbol')!=symbol or row.get('audited')!='Audited' or (row.get('consolidated') or '').lower() not in ('standalone','consolidated'):continue
        if u.scheme!='https' or u.hostname!='nsearchives.nseindia.com' or not u.path.startswith('/corporate/ixbrl/') or 'BANKING' not in u.path:continue
        try:day=datetime.strptime(row.get('qe_Date',''),'%d-%b-%Y').date()
        except ValueError:continue
        out.append({'url':url,'day':day,'basis':row['consolidated'].lower()})
    return out


def _filing(symbol,isin,url,client):
    from .financial_sources import request
    from .intraday_data import atomic_json
    folder=ROOT/'data/fundamental-repair/sources'/symbol
    cache=folder/(hashlib.sha256(url.encode()).hexdigest()[:20]+'.json')
    record=json.loads(cache.read_text()) if cache.exists() else {}
    if record.get('source_url')!=url or not 0<=_age(record.get('observed_at'))<7*86400:
        response=request(client.session,url);record={'source_url':url,'observed_at':now(),'html':response.text};atomic_json(cache,record)
    bank=bank_annual(record['html'],symbol,isin,url)
    content=record['html'].encode('utf-8');digest=hashlib.sha256(content).hexdigest()
    raw=ROOT/'data/filings'/symbol/(digest+'.html');raw.parent.mkdir(parents=True,exist_ok=True)
    if not raw.exists():raw.write_bytes(content)
    return {'status':'verified','symbol':symbol,'isin':isin,'source_url':url,'raw_sha256':digest,'observed_at':record['observed_at'],'bank':bank}


def fetch_bank_inputs(symbol,isin,details,client=None):
    """Newest audited NSE BANKING filing for the selected period and basis."""
    from .fallbacks import Fallbacks
    client=client or Fallbacks();latest=details.get('latest') or {}
    period=datetime.strptime(latest['period'],'%b %Y').strftime('%b %Y');basis=latest.get('statement_type')
    errors=[]
    for row in _index(symbol,client):
        if row['basis']!=basis or row['day'].strftime('%b %Y')!=period:continue
        try:return _filing(symbol,isin,row['url'],client)
        except ValueError as error:errors.append(str(error))
    raise ValueError('; '.join(dict.fromkeys(errors)) or 'No audited NSE banking filing for the selected period and basis')


def fetch_bank_record(symbol,isin,details,client=None):
    """Newest audited annual bank filing and the preceding year's, same basis."""
    from .fallbacks import Fallbacks
    client=client or Fallbacks();rows=[r for r in _index(symbol,client) if r['day'].month==3 and r['day'].day==31]
    if not rows:raise ValueError('No audited NSE banking annual filing')
    newest=max(r['day'] for r in rows);bases={r['basis'] for r in rows if r['day']==newest}
    current_basis=(details.get('latest') or {}).get('statement_type')
    basis=current_basis if current_basis in bases else 'consolidated' if 'consolidated' in bases else 'standalone'
    def one(day):
        errors=[]
        for row in rows:
            if row['day']==day and row['basis']==basis:
                try:return _filing(symbol,isin,row['url'],client)
                except ValueError as error:errors.append(str(error))
        raise ValueError('; '.join(errors) or 'No audited '+basis+' banking filing for '+day.isoformat())
    current=one(newest)
    try:prior=one(newest.replace(year=newest.year-1))
    except ValueError:prior=None
    return {'current':current,'prior':prior}


def bank_record(details,proofs,symbol,isin):
    """A whole annual record from audited bank filings, for a stale or missing provider record."""
    current=proofs['current'];prior=proofs.get('prior')
    for proof in (current,prior):
        if proof and (proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin)):raise ValueError('Bank filing identity not verified')
    bank=current['bank'];out=copy.deepcopy(details)
    old=out.get('latest') or {}
    try:newer=datetime.strptime(old.get('period') or '','%b %Y')>=datetime.strptime(bank['period'],'%b %Y')
    except ValueError:newer=False
    if newer:raise ValueError('Provider record is not older than the audited bank filing')
    stamp={'source_url':current['source_url'],'raw_sha256':current['raw_sha256'],'observed_at':current['observed_at']}
    audited_cash=bank['cash_with_rbi']+bank['bank_balances']
    values={'profit_before_tax':bank['profit_before_tax'],'finance_costs':bank['interest_expenses'],'equity':bank['capital']+bank['reserves'],
            'total_borrowings':bank['borrowings'],'cash_and_cash_equivalents':audited_cash}
    labels={'profit_before_tax':'Total profit (loss) from ordinary activities before tax','finance_costs':'Interest expenses','equity':'Capital + Reserves and surplus',
            'total_borrowings':'Borrowings','cash_and_cash_equivalents':'Cash and Balances with Reserve Bank of India + Balances with Banks and money at call and short notice'}
    if bank['lease_caption_present']:raise ValueError('Bank balance sheet prints a lease caption; lease cannot be treated as not applicable')
    latest={**values,'total_lease_liabilities':None,'period':bank['period'],'period_end':bank['period_end'],'statement_type':bank['statement_type'],'units_in':'crore',
            'reporting_frequency':'annual','source':SOURCE,**stamp,'missing_fields':[],
            'sources':{k:{'source':SOURCE,**stamp,'label':labels[k],'value':v} for k,v in values.items()},
            'not_applicable':{'total_lease_liabilities':{'source':SOURCE,'framework':FRAMEWORK,'period':bank['period'],'statement_type':bank['statement_type'],
                'reason':'The audited Third Schedule balance sheet has no lease-liability line; lessee operating leases are expensed under AS 19.','balance_captions':bank['balance_captions'],**stamp}}}
    summary={'statement_type':bank['statement_type'],'units':'crore','frequency':'annual','growth_sources':{},'growth_observed_at':{}}
    previous=(prior or {}).get('bank') or {}
    if previous and (previous.get('statement_type')!=bank['statement_type'] or datetime.fromisoformat(previous['period_end']).replace(year=datetime.fromisoformat(previous['period_end']).year+1)!=datetime.fromisoformat(bank['period_end'])):previous={}
    for key,field in (('revenue','total_income'),('net_profit','net_profit')):
        a,b=bank.get(field),previous.get(field)
        summary[key]=a;summary[key+'_period']=bank['period']
        summary[key+'_growth_yoy']=(a/b-1)*100 if a is not None and b is not None and b>0 else None
        if b is not None:
            summary['growth_sources'][key]=SOURCE;summary['growth_observed_at'][key]=[current['observed_at'],prior['observed_at']]
            summary.setdefault('growth_inputs',{})[key]={'current':a,'prior':b,'prior_period':previous['period'],'source':SOURCE,'source_urls':[current['source_url'],prior['source_url']]}
    out.update(latest=latest,summary=summary,selected_source=SOURCE,missing_fields=[] if out.get('sector') else ['sector'])
    out['status']='COMPLETE' if out.get('sector') else 'PARTIAL'
    out['bank_record']={'current':{k:v for k,v in current.items() if k!='bank'},'prior':{k:v for k,v in prior.items() if k!='bank'} if prior else None,'replaced_provider_period':old.get('period')}
    return out


def bank_refresh_due(details):
    """Missing bank inputs, or bank evidence near the 30-day freshness limit."""
    latest=(details or {}).get('latest') or {}
    if 'bank' not in (details.get('sector') or '').lower():return False
    if any(latest.get(k) is None for k in FILLABLE) or (latest.get('total_lease_liabilities') is None and 'total_lease_liabilities' not in not_applicable(details)):return True
    stamp=(details.get('bank_inputs') or {}).get('observed_at')
    try:return (datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).days>=REFRESH_DAYS
    except (TypeError,ValueError):return False
