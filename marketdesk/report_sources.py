"""Public BSE and explicitly identified company annual-report discovery.

Security matching is by ISIN, never company-name similarity. Source failures are
returned as evidence in the repair report; they are not silently filled with zero.
"""
import hashlib,json,re,threading,time
from datetime import datetime,timezone
from urllib.parse import urlsplit,urljoin,unquote
import requests
from bs4 import BeautifulSoup
from .storage import ROOT,now
from .intraday_data import atomic_json

MASTER_URL='https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w?Group=&Scripcode=&industry=&segment=Equity&status=Active'
_master_lock=threading.Lock();_pace_lock=threading.Lock();_last={};_blocked={}
# Reviewed official investor pages. Company PDFs must also contain the exact ISIN.
COMPANY_PAGES={
 'INE451C01013':'https://acglgoa.com/investors/annual-reports/',
 'INE757C01021':'https://www.addiindustries.com/annual-report/',
 'INE767A01016':'https://www.aartidrugs.com/reports-fillings-annual-reports',
 'INE021A01026':'https://www.asianpaints.com/AnnualReports.html',
 'INE105C01023':'https://3pland.com/',
 'INE495A01022':'https://www.dalmiasugar.com/investors/',
 'INE626Z01029':'https://www.hardwyn.com/annual-report/',
 'INE505Y01010':'https://vineetlabs.co.in/annual-reports.html',
 'INE550H01011':'https://www.supremeinfra.com/Supreme-Infrastructure-India-Limited/Investors/Annual-Reports',
 'INE248A01017':'https://www.itiltd.in/investors.php',
}

def download_report(requester,session,url,path,limit=80_000_000):
    """Retry interrupted public PDF streams from byte zero, at most three times.

    Each attempt uses the provider's existing pacing and cooldown. Never retry
    an HTTP/access rejection, oversized response, or an invalid PDF. Partial
    files are removed; only a complete attempt contributes to the source hash.
    The 80 MB cap accommodates large primary reports (for example Lenskart's
    61 MB annual filing); worker, free-space and page-count bounds still apply.
    """
    from pathlib import Path
    path=Path(path)
    for attempt in range(3):
        try:
            size=0;digest=hashlib.sha256();prefix=b''
            with requester(session,url,stream=True) as response,path.open('wb') as out:
                for chunk in response.iter_content(131072):
                    if not chunk:continue
                    size+=len(chunk)
                    if size>limit:raise ValueError('Annual report exceeds download limit')
                    if len(prefix)<1024:prefix=(prefix+chunk)[:1024]
                    digest.update(chunk);out.write(chunk)
                length=response.headers.get('Content-Length')
                if length and not response.headers.get('Content-Encoding') and size!=int(length):
                    raise requests.exceptions.ChunkedEncodingError('Incomplete annual report response')
            if not prefix.lstrip().startswith(b'%PDF-'):raise ValueError('Annual report response is not a PDF')
            return digest.hexdigest(),size
        except (requests.exceptions.ConnectionError,requests.exceptions.Timeout,requests.exceptions.ChunkedEncodingError):
            path.unlink(missing_ok=True)
            if attempt==2:raise
            time.sleep(attempt+1)
        except Exception:
            path.unlink(missing_ok=True)
            raise

def public_request(session,url,**kwargs):
    u=urlsplit(url);host=u.hostname or ''
    if u.scheme!='https' or u.username or u.password:raise ValueError('Public source requires HTTPS')
    group='BSE' if host in ('api.bseindia.com','www.bseindia.com') else host
    with _pace_lock:
        if time.monotonic()<_blocked.get(group,0):raise ValueError(group+' cooldown after access/rate-limit response')
        delay=.6-(time.monotonic()-_last.get(group,0))
        if delay>0:time.sleep(delay)
        _last[group]=time.monotonic()
    r=session.get(url,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bseindia.com/' if group=='BSE' else url},timeout=(5,20),allow_redirects=False,**kwargs)
    if r.status_code in (401,403,429):
        with _pace_lock:_blocked[group]=time.monotonic()+900
    if r.status_code!=200:
        status=r.status_code;r.close();raise ValueError(group+' HTTP '+str(status))
    return r

def fresh(record,seconds=86400):
    try:return 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(record['observed_at'])).total_seconds()<seconds
    except (ValueError,KeyError,TypeError):return False

def bse_security(rows,isin):
    matches=[r for r in rows if r.get('ISIN_NUMBER')==isin and r.get('Segment')=='Equity' and re.fullmatch(r'\d{6}',str(r.get('SCRIP_CD','')))]
    if len(matches)!=1:raise ValueError('BSE ISIN match unavailable or ambiguous')
    return matches[0]

def bse_pdf_url(code,filename):
    name=filename.lstrip('\\')
    # BSE appends .pdf twice to newer UUID attachments in its index.
    m=re.fullmatch(r'([a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})\.pdf(?:\.pdf)?',name)
    if m:return 'https://www.bseindia.com/xml-data/corpfiling/AttachHis/'+m[1]+'.pdf'
    if re.fullmatch(r'\d+\.pdf',name) and re.fullmatch(r'\d{6}',str(code)):
        return 'https://www.bseindia.com/bseplus/AnnualReport/'+str(code)+'/'+name
    raise ValueError('Unsupported BSE annual-report attachment name')

def bse_reports(session,isin,year,folder):
    with _master_lock:
        path=ROOT/'data/annual-reports/bse-securities.json'
        master=json.loads(path.read_text()) if path.exists() else {}
        if not fresh(master,7*86400):
            response=public_request(session,MASTER_URL)
            try:rows=response.json()
            finally:response.close()
            if not isinstance(rows,list):raise ValueError('BSE security list unavailable')
            master={'observed_at':now(),'source_url':MASTER_URL,'rows':rows};atomic_json(path,master)
        security=bse_security(master['rows'],isin)
    code=security['SCRIP_CD'];url='https://api.bseindia.com/BseIndiaAPI/api/AnnualReport/w?scripcode='+code
    path=folder/'bse-index.json';index=json.loads(path.read_text()) if path.exists() else {}
    if not fresh(index) or index.get('isin')!=isin or index.get('scrip_code')!=code:
        response=public_request(session,url)
        try:payload=response.json()
        finally:response.close()
        index={'observed_at':now(),'isin':isin,'scrip_code':code,'source_url':url,'payload':payload};atomic_json(path,index)
    choices=[]
    for row in index['payload'].get('Table',[]):
        if year is not None and str(row.get('year'))!=str(year):continue
        try:pdf=bse_pdf_url(code,row.get('file_name',''))
        except ValueError:continue
        choices.append({'fileName':pdf,'provider':'BSE','companyName':security.get('Scrip_Name'),'identity':{'isin':isin,'scrip_code':code,'source_url':MASTER_URL},'index_url':url})
        if len(choices)>=2:break
    return choices

def company_reports(session,isin,year,folder):
    url=COMPANY_PAGES.get(isin)
    if not url:return []
    path=folder/'company-index.json';cached=json.loads(path.read_text()) if path.exists() else {}
    if not fresh(cached) or cached.get('source_url')!=url:
        response=public_request(session,url)
        try:
            if len(response.content)>3_000_000:raise ValueError('Company index exceeds size limit')
            html=response.text
        finally:response.close()
        cached={'observed_at':now(),'source_url':url,'html':html};atomic_json(path,cached)
    soup=BeautifulSoup(cached['html'],'html.parser');choices=[];seen=set()
    for a in soup.find_all('a',href=True):
        href=urljoin(url,a['href']);u=urlsplit(href)
        # Old same-host links on an HTTPS investor page may still spell http.
        # Upgrade those links; never downgrade TLS or follow an unreviewed host.
        if u.scheme=='http' and u.hostname==urlsplit(url).hostname:
            href=u._replace(scheme='https').geturl();u=urlsplit(href)
        if u.scheme!='https' or u.hostname!=urlsplit(url).hostname or not u.path.lower().endswith('.pdf'):continue
        label=a.get_text(' ',strip=True)
        # Malformed legacy HTML can nest hundreds of links in one anchor.
        if len(label)>300:label=' '.join(str(t).strip() for t in a.find_all(string=True,recursive=False))
        label+=' '+unquote(u.path)
        if not re.search(r'annual[\s_-]*report',label,re.I):continue
        periods=[int(b) if len(b)==4 else int(a)//100*100+int(b) for a,b in re.findall(r'\b(20\d{2})\s*[-_/]\s*(20\d{2}|\d{2})\b',label) if (int(b) if len(b)==4 else int(a)//100*100+int(b))==int(a)+1]
        if year and ((periods and year not in periods) or (not periods and not re.search(r'\b'+str(year)+r'\b',label))):continue
        if href in seen:continue
        seen.add(href);choices.append({'fileName':href,'provider':'Company website','index_url':url,'require_pdf_isin':True})
    return choices[:2]
