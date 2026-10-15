"""Bounded NSE annual filing repair with raw-response caching and explicit provenance."""
import hashlib,json,threading,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
import requests
from .fallbacks import nse_annual,Fallbacks,PARSER_REVISION
from .storage import ROOT,now
from .intraday_data import atomic_json

VERSION=2
_lock=threading.Lock();_last=0.;_blocked_until=0.


def request(session,url,**kwargs):
    global _last,_blocked_until
    with _lock:
        if time.monotonic()<_blocked_until:raise ValueError('NSE provider cooldown after access/rate-limit response')
        delay=.4-(time.monotonic()-_last)
        if delay>0:time.sleep(delay)
        _last=time.monotonic()
    response=session.get(url,timeout=(5,18),allow_redirects=False,**kwargs)
    if response.status_code in (401,403,429):
        with _lock:_blocked_until=time.monotonic()+900
    if response.status_code!=200:raise ValueError('NSE HTTP '+str(response.status_code))
    return response


class AnnualSources:
    def __init__(self):
        self.local=threading.local()
    def client(self):
        if not hasattr(self.local,'client'):self.local.client=Fallbacks()
        return self.local.client
    def fetch(self,symbol,isin,sector_missing=False):
        folder=ROOT/'data/fundamental-repair/sources'/symbol;folder.mkdir(parents=True,exist_ok=True)
        old_file=ROOT/'data/fallbacks'/(symbol+'.json')
        old=json.loads(old_file.read_text()) if old_file.exists() else {}
        if old and (old.get('symbol'),old.get('isin'))!=(symbol,isin):raise ValueError('Fallback cache identity mismatch')
        client=self.client();annual=[];errors=[];documents=[];seen=set()
        def parse(content,url,stamp):
            try:
                row=nse_annual(content.decode('utf-8'),symbol,isin,url)
                digest=hashlib.sha256(content).hexdigest()
                raw=ROOT/'data/filings'/symbol/(digest+'.html');raw.parent.mkdir(parents=True,exist_ok=True)
                if not raw.exists():raw.write_bytes(content)
                row['observed_at']=stamp;row['raw_sha256']=digest;row['source_url']=url
                try:
                    prior=nse_annual(content.decode('utf-8'),symbol,isin,url,year_offset=-1)
                    prior.update(observed_at=stamp,raw_sha256=digest,source_url=url)
                    annual.append(prior)
                except ValueError:pass
                documents.append({'source_url':url,'sha256':digest,'observed_at':stamp})
                annual.append(row);seen.add(url)
                return row
            except (ValueError,UnicodeError) as error:
                errors.append({'source_url':url,'reason':str(error)[:160]});seen.add(url)
        # Reparse successful older extraction with the repaired parser, preserving its age.
        for doc in old.get('nse',{}).get('documents',[]) or [old.get('nse',{})]:
            if not doc.get('sha256') or not doc.get('source_url'):continue
            raw=ROOT/'data/filings'/symbol/(doc['sha256']+'.html')
            if raw.exists() and hashlib.sha256(raw.read_bytes()).hexdigest()==doc['sha256']:
                parse(raw.read_bytes(),doc['source_url'],doc.get('observed_at') or old.get('nse',{}).get('observed_at'))
        latest_period=max((a['period_end'] for a in annual),default='')
        if not any(a['period_end']==latest_period and not a['missing_fields'] and 0<=(datetime.now(timezone.utc).date()-datetime.fromisoformat(a['period_end']).date()).days<=550 for a in annual):
            try:
                index_file=folder/'index.json'
                index=json.loads(index_file.read_text()) if index_file.exists() else {}
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(index['observed_at'])).total_seconds() if index.get('observed_at') else 1e10
                if not 0<=age<86400:
                    response=request(client.session,'https://www.nseindia.com/api/integrated-filing-results',params={'symbol':symbol})
                    index={'symbol':symbol,'observed_at':now(),'payload':response.json()};atomic_json(index_file,index)
                if index.get('symbol',symbol)!=symbol:raise ValueError('NSE index identity mismatch')
                rows=index['payload'];rows=rows if isinstance(rows,list) else rows.get('data',[])
                candidates={}
                for row in rows:
                    url=row.get('ixbrl');u=urlsplit(url or '')
                    if row.get('symbol')!=symbol or row.get('consolidated') not in ('Standalone','Consolidated'):continue
                    if u.scheme!='https' or u.hostname!='nsearchives.nseindia.com' or not u.path.startswith('/corporate/ixbrl/'):continue
                    if not any(tag in u.path for tag in ('INDAS','FINANCIAL')):continue
                    try:day=datetime.strptime(row.get('qe_Date',''),'%d-%b-%Y').date()
                    except ValueError:continue
                    if not 0<=(datetime.now(timezone.utc).date()-day).days<=550:continue
                    # Keep newest filing/revision for a basis and period. NSE lists newest first.
                    candidates.setdefault((day.isoformat(),row['consolidated']),url)
                # Try March annual periods first, then other fiscal-year ends. The HTML
                # must independently prove a full audited year; index 'audited' is quarterly.
                ordered=sorted(candidates,key=lambda k:(k[0][5:7]=='03',k[0],k[1]=='Consolidated'),reverse=True)
                attempted=0
                for key in ordered:
                    url=candidates[key]
                    # Once a full year is verified for a basis, a later quarter
                    # less than a year away cannot supply another full-year balance.
                    known=[a for a in annual if a['statement_type']==key[1].lower()]
                    candidate_day=datetime.fromisoformat(key[0]).date()
                    if any(0<abs((candidate_day-datetime.fromisoformat(a['period_end']).date()).days)<364 for a in known):continue
                    if url in seen:continue
                    if attempted>=4:break
                    attempted+=1
                    cache=folder/(hashlib.sha256(url.encode()).hexdigest()[:20]+'.json')
                    if cache.exists():record=json.loads(cache.read_text())
                    else:
                        response=request(client.session,url)
                        record={'source_url':url,'observed_at':now(),'html':response.text};atomic_json(cache,record)
                    if record['source_url']!=url:raise ValueError('Document cache identity mismatch')
                    found=parse(record['html'].encode('utf-8'),url,record['observed_at'])
                    if found and not found['missing_fields']:break
            except (ValueError,requests.RequestException) as error:errors.append({'reason':str(error)[:160] if isinstance(error,ValueError) else type(error).__name__})
        # Prefer completeness among the latest annual period, then consolidated basis.
        annual.sort(key=lambda a:(a['period_end'],-len(a['missing_fields']),a['statement_type']=='consolidated'),reverse=True)
        # apply_fallback can also use older matching-basis years for growth.
        nse={'status':'available' if annual else 'no_qualified_annual','source':'nse_audited_filing',
             'annual':annual,'documents':documents,'errors':errors,'observed_at':annual[0]['observed_at'] if annual else index.get('observed_at') if 'index' in locals() else now()}
        yahoo=old.get('yahoo',{})
        if yahoo.get('status')!='available' or (sector_missing and not yahoo.get('sector')):
            fresh=client.yahoo(symbol)
            if fresh.get('status')=='available' or yahoo.get('status')!='available':yahoo=fresh
            else:yahoo={**yahoo,'last_attempt':{'observed_at':now(),'status':fresh.get('status'),'error':fresh.get('error')}}
        return {'version':VERSION,'parser_revision':PARSER_REVISION,'symbol':symbol,'isin':isin,'observed_at':now(),'nse':nse,'yahoo':yahoo,
                'note':'Yahoo unspecified-basis figures are retained as supplemental evidence, not merged into verified NSE/Upstox annual fields.'}
