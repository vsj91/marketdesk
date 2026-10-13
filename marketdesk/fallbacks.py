"""Supplemental annual sources. Never infer zero or mix an unknown statement basis."""
import calendar,json,time,re,threading,hashlib
from datetime import datetime,timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import requests
from .storage import ROOT,now,number
from .provider import FUNDAMENTAL_REQUIRED_FIELDS,_fundamental_period

class Tables(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.tables=[];self.table=None;self.row=None;self.cell=None;self.depth=0
    def handle_starttag(self,tag,attrs):
        if tag=='table':
            self.depth+=1
            if self.depth==1:self.table=[]
        if self.depth!=1:return
        if tag=='tr':self.row=[]
        if tag in ('td','th'):self.cell=[]
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if self.depth==1:
            if tag in ('td','th') and self.cell is not None:
                if self.row is not None:self.row.append(' '.join(''.join(self.cell).split()))
                self.cell=None
            if tag=='tr' and self.row is not None:self.table.append(self.row);self.row=None
            if tag=='table':self.tables.append(self.table);self.table=None
        if tag=='table':self.depth-=1

def lease_label(text):
    """Recognized filing captions only; no fuzzy match to rent/advance/ROU assets."""
    label=text.casefold().strip()
    label=re.sub(r'^(?:\([a-z0-9]+\)|[ivx]+\)|[-–])\s*','',label)
    label=re.sub(r'^(?:non[- ]current|current|long[- ]term|short[- ]term)\s+','',label)
    label=re.sub(r',\s*(?:non[- ]current|current)$','',label)
    label=re.sub(r'\s*\((?:non[- ]current|current)\)$','',label)
    return label in {'lease liabilities','lease liability','lease liabilites','lease liablities','lease liabilties',
                     'lease laibility','lease liablilities','lease liabilty','lease obligation','lease obligations',
                     'lease laibilities','lease liabiities','lease liabililty','lease liablility','lease liabitliy','lease libailities','lease liabliities','lease liab'}

def nse_annual(html,symbol,isin,url,year_offset=0,*,income_only=False):
    if len(html.encode())>2_000_000:raise ValueError('NSE document exceeds parsing limit')
    parser=Tables();parser.feed(html);tables=parser.tables
    def values(rows,label):
        matches=[]
        for row in rows:
            for i,cell in enumerate(row):
                if cell.casefold()==label.casefold():matches.append(row[i+1:])
        if len(matches)!=1:raise ValueError('Missing/ambiguous NSE field: '+label)
        return matches[0]
    def scalar(rows,label):
        v=values(rows,label)
        if len(v)!=1:raise ValueError('Ambiguous NSE metadata')
        return v[0]
    def has(rows,label):return any(any(c.casefold()==label.casefold() for c in r) for r in rows)
    def choose(*labels):
        found=[t for t in tables if all(has(t,k) for k in labels)]
        if len(found)!=1:raise ValueError('Unsupported or ambiguous NSE statement template')
        return found[0]
    meta=choose('NSE Symbol','ISIN')
    if scalar(meta,'NSE Symbol')!=symbol or scalar(meta,'ISIN')!=isin or scalar(meta,'Class of security')!='Equity':raise ValueError('NSE filing identity mismatch')
    currency=scalar(meta,'Description of presentation currency')
    if income_only and currency=='INR (in Actuals)' and not has(meta,'Level of rounding used in financial results'):
        currency,unit='INR','Rupees'
    else:unit=scalar(meta,'Level of rounding used in financial results')
    scales={'Lakhs':.01,'Crores':1.,'Rupees':1e-7}
    if currency!='INR' or unit not in scales:raise ValueError('NSE currency/scale not verified')
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
    if year_offset not in (0,-1):raise ValueError('Unsupported comparative year')
    if year_offset:
        start=start.replace(year=start.year+year_offset);end=end.replace(year=end.year+year_offset)
    income=choose('Total profit before tax','Finance costs');balance=[] if income_only else choose('Total equity','Cash and cash equivalents')
    def column(rows):
        starts=values(rows,'Date of start of reporting period');ends=values(rows,'Date of end of reporting period');audits=values(rows,'Whether results are audited or unaudited');bases=values(rows,'Nature of report standalone or consolidated')
        if len({len(starts),len(ends),len(audits),len(bases)})!=1:raise ValueError('NSE column dimensions mismatch')
        indices=[i for i,s in enumerate(starts) if day(s)==start and day(ends[i])==end and audits[i]=='Audited' and bases[i].lower()==basis]
        if len(indices)!=1:raise ValueError('NSE annual column not unique')
        return indices[0],len(starts)
    ic,bc=column(income),None if income_only else column(balance)
    out=dict.fromkeys(FUNDAMENTAL_REQUIRED_FIELDS);sources={}
    def get(rows,label,col,key):
        try:
            v=values(rows,label)
            if len(v)!=col[1]:raise ValueError('NSE value column mismatch')
            text=v[col[0]].replace(',','').strip()
            if re.fullmatch(r'\(\d+(?:\.\d+)?\)',text):text='-'+text[1:-1]
            val=number(text)
            if val is None:return None
            sources[key]={'source':'nse_audited_filing','source_url':url,'label':label,'source_units':unit,'conversion_to_crore':scales[unit],'period_end':end.isoformat(),'statement_type':basis}
            return val*scales[unit]
        except ValueError:return None
    for key,label in [('profit_before_tax','Total profit before tax'),('finance_costs','Finance costs')]:out[key]=get(income,label,ic,key)
    if income_only:
        # Comparative income columns are independently dated and audited. A
        # missing comparative balance column must not discard valid growth data.
        for key,labels in [('revenue',('Revenue from operations','Total Revenue from operations')),('net_profit',('Total profit (loss) for period',))]:
            found=[label for label in labels if has(income,label)]
            if len(found)==1:out[key]=get(income,found[0],ic,key)
        out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',reporting_frequency='annual',sources=sources,source='nse_audited_filing',income_only=True)
        return out
    for key,label in [('equity','Total equity'),('current_borrowings','Borrowings, current'),('non_current_borrowings','Borrowings, non-current'),('cash_and_cash_equivalents','Cash and cash equivalents')]:out[key]=get(balance,label,bc,key)
    # NBFC statements do not split borrowings into current/non-current sections.
    # Preserve components but require all explicit debt components before summing.
    if has(balance,'Borrowings (Other than Debt Securities)'):
        components={key:get(balance,label,bc,key) for key,label in [('debt_securities','Debt Securities'),('other_borrowings','Borrowings (Other than Debt Securities)'),('subordinated_liabilities','Subordinated Liabilities')]}
        out.update(components)
        if all(v is not None for v in components.values()):
            out['total_borrowings']=sum(components.values());sources['total_borrowings']={'source':'nse_audited_filing','source_url':url,'formula':'debt_securities + other_borrowings + subordinated_liabilities','inputs':components}
    non_current=next((i for i,r in enumerate(balance) if any(c=='Non-current liabilities' for c in r)),None)
    current=next((i for i,r in enumerate(balance) if any(c=='Current liabilities' for c in r)),None)
    if non_current is not None and current is not None and current>non_current:
        for key,section in [('current_lease_liabilities',balance[current:]),('non_current_lease_liabilities',balance[non_current:current])]:
            labels=[c for row in section for c in row if lease_label(c)]
            out[key]=get(section,labels[0],bc,key) if len(labels)==1 else None
    elif non_current is None and current is None:
        labels=[c for row in balance for c in row if lease_label(c) and not re.search(r'current|long[- ]term|short[- ]term',c,re.I)]
        if len(labels)==1:out['total_lease_liabilities']=get(balance,labels[0],bc,'total_lease_liabilities')
    for total,a,b in [('total_borrowings','current_borrowings','non_current_borrowings'),('total_lease_liabilities','current_lease_liabilities','non_current_lease_liabilities')]:
        if out.get(a) is not None and out.get(b) is not None:
            out[total]=out[a]+out[b];sources[total]={'source':'nse_audited_filing','source_url':url,'formula':a+' + '+b,'inputs':{a:out[a],b:out[b]}}
    if any(out.get(k) is not None and out[k]<0 for k in ['finance_costs','current_borrowings','non_current_borrowings','current_lease_liabilities','non_current_lease_liabilities','cash_and_cash_equivalents','debt_securities','other_borrowings','subordinated_liabilities','total_borrowings','total_lease_liabilities']):raise ValueError('NSE negative debt/cash inputs require review')
    for key,labels in [('revenue',('Revenue from operations','Total Revenue from operations')),('net_profit',('Total profit (loss) for period',))]:
        found=[label for label in labels if has(income,label)]
        if len(found)==1:out[key]=get(income,found[0],ic,key)
    out.update(period=end.strftime('%b %Y'),period_end=end.isoformat(),statement_type=basis,units_in='crore',reporting_frequency='annual',sources=sources,source='nse_audited_filing',missing_fields=[k for k in FUNDAMENTAL_REQUIRED_FIELDS if out.get(k) is None])
    return out

class Fallbacks:
    def __init__(self):
        self.session=requests.Session();self.session.headers.update({'User-Agent':'Mozilla/5.0','Accept':'application/json,text/html,*/*','Referer':'https://www.nseindia.com/companies-listing/corporate-integrated-filing'})
        self.nse_until=0;self.yahoo_until=0;self.nse_warmed=False
    def nse(self,symbol,isin):
        if time.monotonic()<self.nse_until:return {'status':'cooldown','error':'NSE temporarily unavailable; backing off.'}
        try:
            if not self.nse_warmed:
                r=self.session.get('https://www.nseindia.com',timeout=(5,10));self.nse_warmed=True
                # The landing page and public data API have separate access policies; try the normal API once.
            response=self.session.get('https://www.nseindia.com/api/integrated-filing-results',params={'symbol':symbol},timeout=(5,15))
            if response.status_code in (401,403,429):self.nse_until=time.monotonic()+1800
            response.raise_for_status();payload=response.json()
            records=payload if isinstance(payload,list) else payload.get('data',[])
            candidates=[]
            for row in records:
                if row.get('symbol')!=symbol or row.get('consolidated') not in ('Consolidated','Standalone'):continue
                url=row.get('ixbrl');u=urlsplit(url or '')
                if u.scheme=='https' and u.hostname=='nsearchives.nseindia.com' and u.path.startswith('/corporate/ixbrl/'):
                    try:filing_date=datetime.strptime(row.get('qe_Date',''),'%d-%b-%Y').date().isoformat()
                    except ValueError:continue
                    candidates.append((filing_date,url))
            errors=[]
            # Filing date order is not trusted as an annual statement selection.
            for _,url in sorted(candidates,reverse=True)[:6]:
                r=self.session.get(url,timeout=(5,15));r.raise_for_status()
                try:annual=nse_annual(r.text,symbol,isin,url)
                except ValueError as e:errors.append(str(e));continue
                path=ROOT/'data'/'filings'/symbol;path.mkdir(parents=True,exist_ok=True)
                digest=hashlib.sha256(r.content).hexdigest();(path/(digest+'.html')).write_bytes(r.content)
                return {'status':'available','source':'nse_audited_filing','annual':[annual],'observed_at':now(),'source_url':url,'sha256':digest}
            return {'status':'no_qualified_annual','errors':errors[:6]}
        except Exception as e:
            self.nse_until=time.monotonic()+300
            return {'status':'unavailable','error':type(e).__name__}
    def yahoo(self,symbol):
        if time.monotonic()<self.yahoo_until:return {'status':'cooldown','error':'Yahoo temporarily unavailable; backing off.'}
        try:
            import yfinance as yf
            yf.set_tz_cache_location(str(ROOT/'data'/'yahoo-cache'))
            ticker=yf.Ticker(symbol+'.NS')
            info=ticker.get_info();currency=info.get('financialCurrency')
            if info.get('symbol')!=symbol+'.NS':return {'status':'identity_unverified','error':'Yahoo returned a different symbol.'}
            annual={}
            # Yahoo does not expose a dependable consolidated/standalone flag.
            # Its rows remain supplemental and are never mixed into an Upstox basis.
            specs=[('income',ticker.get_income_stmt,{'PretaxIncome':'profit_before_tax','InterestExpense':'reported_interest_expense','TotalRevenue':'revenue','NetIncome':'net_profit'}),
                   ('balance',ticker.get_balance_sheet,{'StockholdersEquity':'equity','CashAndCashEquivalents':'cash_and_cash_equivalents','CurrentDebt':'current_borrowings','LongTermDebt':'non_current_borrowings','TotalDebt':'reported_total_debt','CurrentCapitalLeaseObligation':'current_lease_liabilities','LongTermCapitalLeaseObligation':'non_current_lease_liabilities','CapitalLeaseObligations':'reported_capital_lease_obligations'}),
                   ('cashflow',ticker.get_cashflow,{'OperatingCashFlow':'operating_cash_flow','FreeCashFlow':'free_cash_flow'})]
            for endpoint,fetch,mapping in specs:
                frame=fetch(freq='yearly')
                for stamp in frame.columns:
                    date=stamp.date()
                    if date>datetime.now(timezone.utc).date():continue
                    key=date.isoformat();row=annual.setdefault(key,{'period':date.strftime('%b %Y'),'period_end':key,'statement_type':'unspecified','currency':currency,'units_in':'INR' if currency=='INR' else currency,'source':'yahoo_finance','sources':{}})
                    for label,metric in mapping.items():
                        if label not in frame.index:continue
                        val=number(frame.loc[label,stamp])
                        if val is not None:row[metric]=val;row['sources'][metric]={'source':'yahoo_finance','field':label,'endpoint':endpoint,'source_url':f'https://finance.yahoo.com/quote/{symbol}.NS/financials/','period_end':key,'currency':currency}
            return {'status':'available' if annual else 'no_data','source':'yahoo_finance','symbol':info.get('symbol'),'sector':info.get('sector'),'observed_at':now(),'annual':sorted(annual.values(),key=lambda x:x['period_end'],reverse=True),'note':'Supplemental research only: statement basis is unspecified. Interest expense is not silently substituted for finance costs; total debt is not silently substituted for borrowings.'}
        except Exception as e:
            self.yahoo_until=time.monotonic()+300
            return {'status':'unavailable','error':type(e).__name__}
    def fetch(self,symbol,isin,sector_missing=False,reuse_recent=False):
        from .financial_sources import AnnualSources
        from .intraday_data import atomic_json
        import fcntl
        path=ROOT/'data/fallbacks';path.mkdir(parents=True,exist_ok=True)
        cache=path/(symbol+'.json')
        with (path/(symbol+'.lock')).open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if cache.exists():
                old=json.loads(cache.read_text())
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(old['observed_at'])).total_seconds()
                if (old.get('symbol'),old.get('isin'))!=(symbol,isin):raise ValueError('Fallback cache identity mismatch')
                if old.get('version')==2 and old.get('parser_revision')==5 and 0<=age<86400 and (reuse_recent or not old.get('nse',{}).get('errors')) and not (sector_missing and not old.get('yahoo',{}).get('sector')):return old
            if not hasattr(self,'annual_sources'):self.annual_sources=AnnualSources()
            result=self.annual_sources.fetch(symbol,isin,sector_missing)
            atomic_json(cache,result)
            return result

def apply_fallback(details,result):
    # Replacing the lookup report must not make an unchanged older row look fresh.
    selected=details.get('latest') or {}
    if details.get('selected_source')=='nse_audited_filing' and not selected.get('observed_at'):
        selected['observed_at']=(details.get('fallbacks') or {}).get('nse',{}).get('observed_at')
    details['fallbacks']=result
    nse=result.get('nse',{})
    latest=details.get('latest') or {};_,order=_fundamental_period(latest.get('period'))
    for row in nse.get('annual',[]):
        _,candidate_order=_fundamental_period(row.get('period'))
        whole_statement=result.get('version')==2
        if candidate_order and (whole_statement or not latest.get('statement_type') or latest.get('statement_type')==row.get('statement_type')) and (not order or candidate_order>=order) and len(row.get('missing_fields',[]))<len(latest.get('missing_fields',FUNDAMENTAL_REQUIRED_FIELDS)):
            if latest.get('statement_type')!=row.get('statement_type') and details.get('source_conflicts'):
                details['prior_source_conflicts']=details.pop('source_conflicts')
            if order==candidate_order and latest.get('statement_type')==row.get('statement_type') and latest.get('units_in')==row.get('units_in'):
                conflicts=[]
                for field in FUNDAMENTAL_REQUIRED_FIELDS:
                    a,b=latest.get(field),row.get(field)
                    if a is not None and b is not None and abs(a-b)>max(.01,abs(b)*.00001):conflicts.append({'field':field,'upstox':a,'nse':b,'selected':'nse_audited_filing','period':row['period'],'units':row['units_in']})
                if conflicts:details['source_conflicts']=conflicts
            details.setdefault('annual',[]).append(row);details['latest']=row;details['missing_fields']=list(row['missing_fields'])+([] if details.get('sector') else ['sector']);details['status']='COMPLETE' if not details['missing_fields'] else 'PARTIAL';details['selected_source']='nse_audited_filing'
            if whole_statement:
                # Growth comes from the same source, basis and annual period, never
                # leftover consolidated ratios after selecting a standalone row.
                prior=next((r for r in nse.get('annual',[]) if r.get('statement_type')==row['statement_type'] and _fundamental_period(r.get('period'))[1]==(candidate_order[0]-1,candidate_order[1])),None)
                summary={'statement_type':row['statement_type'],'units':row['units_in'],'frequency':'annual','growth_sources':{},'growth_observed_at':{}}
                for key in ('revenue','net_profit'):
                    value=row.get(key);previous=(prior or {}).get(key)
                    summary[key]=value;summary[key+'_period']=row['period']
                    summary[key+'_growth_yoy']=(value/previous-1)*100 if value is not None and previous is not None and previous>0 else None
                    if summary[key+'_growth_yoy'] is not None:
                        summary['growth_sources'][key]='nse_audited_filing'
                        summary['growth_observed_at'][key]=[row.get('observed_at'),prior.get('observed_at')]
                # A matching provider summary remains usable if NSE does not disclose
                # the comparative annual columns; exact period/basis/unit must agree.
                old=details.get('summary',{})
                for key in ('revenue','net_profit'):
                    if summary[key+'_growth_yoy'] is None and all(old.get(k)==summary.get(k) for k in ('statement_type','units')) and old.get(key+'_period')==row['period'] and old.get('frequency') in ('annual','yearly'):
                        summary[key+'_growth_yoy']=old.get(key+'_growth_yoy')
                        summary['growth_sources'][key]=old.get('growth_sources',{}).get(key,'upstox')
                        if key in old.get('growth_observed_at',{}):summary['growth_observed_at'][key]=old['growth_observed_at'][key]
                details['summary']=summary
            latest=row;order=candidate_order
    sector=result.get('yahoo',{}).get('sector')
    if not details.get('sector') and sector and result.get('yahoo',{}).get('symbol')==str(result.get('symbol',''))+'.NS':
        details['sector']=sector;details['sector_source']='yahoo_finance'
        details['missing_fields']=[f for f in details.get('missing_fields',[]) if f!='sector']
        if not details['missing_fields'] and details.get('latest'):details['status']='COMPLETE'
    return details
