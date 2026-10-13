"""Company annual-report lease evidence, matched to an existing audited annual row.

PDF parsing runs in isolated subprocesses. Only relevant pages are retained: full
reports are temporary, bounded downloads. This module never infers zero from a dash.
"""
import calendar,copy,hashlib,json,math,re,shutil,subprocess,sys,tempfile,threading
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit
from .storage import ROOT,now
from .intraday_data import atomic_json

REVISION=5
INCOME_REVISION=2
_pdf_slots=threading.BoundedSemaphore(4)
NUM=r'(?:\(?-?\d[\d,]*(?:\.\d+)?\)?|[-–—]|Nil|NIL)'
NOTE_REF=r'\d+[a-z]*(?:\([a-z0-9]+\)|\.\d+)*'
LEASE_ROLLFORWARD=r'(?:carrying amounts of (?:the )?lease liabilities|reconciliation of (?:the )?lease liabilities|movement in (?:the )?lease liabilities)'

def amount(token):
    token=token.strip().replace(',','')
    # A printed dash is ambiguous, not a reported numeric zero.
    if token.lower()=='nil':return 0.
    if token in ('-','–','—'):return None
    if token.startswith('(') and token.endswith(')'):token='-'+token[1:-1]
    try:
        n=float(token)
        return n if math.isfinite(n) else None
    except ValueError:return None

def caption(value):
    value=re.sub(r'^\s*(?:(?:\([ivxa-z0-9]+\)|[ivx]+[.)]|[a-z]\)|[0-9]+[.)]?)\s*)+','',value.lower())
    value=re.sub(r'\s*-\s*','-',value.replace('–','-').replace('‑','-'))
    return ' '.join(value.split()).strip(': ')

def statement_context(text,latest,kind='balance'):
    text='\n'.join(line.strip() for line in text.replace('\u00a0',' ').splitlines())
    text=re.sub(r'\blacs?\b','lakhs',text,flags=re.I)
    # Some PDF text layers concatenate a lower-case preposition with the title.
    text=re.sub(r'(?<![A-Z])(CONSOLIDATED|STANDALONE)(?![A-Z])',r' \1 ',text)
    low=text.lower();head=re.sub(r'[ \t]+',' ',low)[:1800]
    heading=r'\bbalance\s+sheet\b' if kind=='balance' else r'\bnotes\s+to\s+(?:the\s+)?(?:standalone\s+|consolidated\s+)?financial\s+statements\b' if kind=='lease_note' else r'\bstatement\s+of\s+(?:(?:standalone|consolidated)\s+)?profit\s+(?:and|or|&)\s+loss\b'
    if not re.search(heading,head):raise ValueError('No '+kind+' statement heading')
    if kind=='income' and re.search(r'quarter|three months|six months',head):raise ValueError('Quarterly income columns are not annual inputs')
    basis=latest.get('statement_type')
    if basis not in ('standalone','consolidated') or not re.search(r'\b'+basis+r'\b',head):raise ValueError('Explicit statement basis missing')
    if re.search(r'\b'+('consolidated' if basis=='standalone' else 'standalone')+r'\b',head):raise ValueError('Mixed statement basis')
    if latest.get('units_in')!='crore':raise ValueError('Baseline unit not crore')
    date=datetime.strptime(latest['period'],'%b %Y');year=date.year
    # Normalize explicit dates, including numeric headings, without deriving a
    # period from a cover or treating quarterly dates as an annual balance date.
    day=calendar.monthrange(year,date.month)[1];month=date.strftime('%b').lower();full=date.strftime('%B').lower()
    date_head=re.sub(r'\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b',lambda m: f'{int(m[1])} {calendar.month_abbr[int(m[2])].lower()} {m[3]}' if 1<=int(m[2])<=12 else m[0],head)
    date_head=re.sub(r'(?<=\w)-(?=\w)',' ',date_head)
    month_pattern='(?:'+month+'|'+full+')';day_pattern=str(day)+r'(?:st|nd|rd|th)?'
    date_pattern=r'(?:'+day_pattern+r'\s*'+month_pattern+'|'+month_pattern+r'\s*'+day_pattern+r')\s*,?\s*'
    if not re.search(date_pattern+str(year),date_head):raise ValueError('Annual balance date not matched')
    years=re.findall(r'\b20\d{2}\b',head)
    if str(year-1) not in years or not re.search(str(year)+r'\s+(?:as\s+at\s+)?(?:'+date_pattern+r')?\s*'+str(year-1),date_head):raise ValueError('Annual column order not verified')
    currency=r'(?:rs\.?|inr|₹|`|rupees?)'
    units=[(name,scale) for name,scale in [('crore',1),('lakh',.01),('million',.1),('thousand',.0001)] if re.search(r'(?:\bin\s+(?:'+currency+r'\s+)?|\(\s*'+currency+r'[^\n]{0,15})'+name+r's?\b',head)]
    if len(units)!=1:raise ValueError('PDF units not explicit or ambiguous')
    if re.search(r'\b(?:usd|eur|gbp|us dollars?)\b|us\$',head) or not re.search(r'(?:₹|`|\binr\b|\brs\.?|\brupees?)\s*[^\n]{0,35}\b(?:crore|lakh|million|thousand)s?\b',head):raise ValueError('PDF INR currency not explicit or ambiguous')
    return text,basis,units[0]

def parse_income(text,latest):
    """Finance costs require an annual income table and matching reported PBT."""
    text,basis,(unit,scale)=statement_context(text,latest,'income');found={};lines={}
    for line in text.splitlines():
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:continue
        label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:m.start()].strip(),flags=re.I)
        label=caption(label);label=re.sub(r'^[ivx]+[.)]?\s+','',label);label=re.sub(r'\s*\([ivx0-9+\-\s]+\)$','',label).strip()
        key='finance_costs' if label in ('finance costs','finance cost','finance cost (interest)','finance costs (interest)') else 'profit_before_tax' if label in ('profit before tax','profit/(loss) before tax','profit / (loss) before tax','profit (loss) before tax','profit/(loss) before taxation','profit before taxation') else None
        if key:
            if key in found:raise ValueError('Duplicate annual income caption')
            value=amount(m[1]);found[key]=value*scale if value is not None else None;lines[key]=line.strip()
    pbt=latest.get('profit_before_tax');reported=found.get('profit_before_tax');finance=found.get('finance_costs')
    if pbt is None or reported is None or abs(pbt-reported)>max(.015,abs(pbt)*.00002):raise ValueError('Annual income PBT anchor does not match')
    if finance is None or finance<0:raise ValueError('Explicit annual finance costs unavailable')
    return {'value':finance,'matched_profit_before_tax':reported,'source_units':unit,'conversion_to_crore':scale,'lines':lines}

def parse_lease_note(text,latest):
    """Read an explicit carrying-value closing balance, never a maturity total."""
    normalized='\n'.join(line.strip() for line in text.splitlines())
    start=re.search(LEASE_ROLLFORWARD,normalized,re.I)
    if not start:raise ValueError('Explicit lease carrying-value reconciliation unavailable')
    scope=re.split(r'\n\s*(?:\d{1,3}[.)]|[A-Z]\))\s+',normalized[start.start():3000+start.start()])[0]
    closing=re.search(r'^\s*(?:closing balance|balance at the end of (?:the )?year)\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',scope,re.I|re.M)
    if not closing:raise ValueError('Explicit lease closing balance unavailable')
    scope=scope[:closing.end()]
    if re.search(r'undiscounted|maturity analysis|contractual maturities',normalized[max(0,start.start()-100):start.start()]+scope,re.I):raise ValueError('Lease maturity amounts are not carrying balances')
    # Keep the actual note heading and its own table dates/units. Do not borrow
    # the balance sheet's basis or unit for an unlabelled note.
    head='\n'.join(normalized[:start.start()].splitlines()[:4])
    _,_,(unit,scale)=statement_context(head+'\n'+scope,latest,'lease_note')
    value=amount(closing[1]);value=value*scale if value is not None else None
    if value is None or value<0:raise ValueError('Lease closing balance is not numeric')
    known=[latest.get(k) for k in ('current_lease_liabilities','non_current_lease_liabilities')]
    if sum(v for v in known if isinstance(v,(int,float)) and v>=0)>value+.015:raise ValueError('Lease closing balance contradicts known components')
    return {'value':value,'source_units':unit,'conversion_to_crore':scale,'line':closing[0].strip(),'method':'explicit_carrying_value_closing_balance'}

def parse_balance(text,latest,allow_existing=False):
    """Two-column annual balance only; period, basis, scale and 2 anchors required."""
    text,basis,(unit,scale)=statement_context(text,latest)
    section=None;found={};evidence={}
    def record(key,value,line):
        if key in found:raise ValueError('Duplicate PDF balance caption: '+key)
        found[key]=None if value is None else value*scale;evidence[key]=line.strip()
    for line in text.splitlines():
        # Stop at the next financial statement; don't read cash-flow lease payments.
        if re.search(r'^(?:standalone |consolidated )?statement of (?:profit|cash|changes)',line.strip().lower()):break
        clean=caption(line)
        if re.fullmatch(r'non[- ]current liabilities',clean):section='non_current'
        elif re.fullmatch(r'current liabilities',clean):section='current'
        m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
        if not m:continue
        label=line[:m.start()].strip()
        # Drop a separate note-number column, never a value column.
        label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',label,flags=re.I)
        label=caption(label);value=amount(m.group(1))
        if label=='cash and cash equivalents':record('cash',value,line)
        if label in ('total equity','total equity (a)','total equity (1)'):record('equity',value,line)
        if label in ('equity attributable to owners of the company','equity attributable to owners of the parent'):record('owners_equity',value,line)
        if label in ('equity share capital','share capital'):record('share_capital',value,line)
        if label=='other equity':record('other_equity',value,line)
        if label in ('non controlling interest','non-controlling interest','non-controlling interests'):record('nci',value,line)
        if label in ('borrowings','borrowings, current','borrowings, non-current') and section:record(section+'_borrowings',value,line)
        if label in ('lease liabilities','lease liability','lease obligations'):
            record((section+'_lease') if section else 'total_lease',value,line)
    matched=[]
    def same(a,b):return a is not None and b is not None and abs(a-b)<=max(.015,abs(b)*.00002)
    if same(found.get('cash'),latest.get('cash_and_cash_equivalents')):matched.append('cash_and_cash_equivalents')
    equity=found.get('equity')
    derived_equity=equity is None
    if derived_equity:
        # Explicit components can substantiate the equity anchor when a printed
        # subtotal has no caption. Consolidated totals must include explicit NCI.
        owners=found.get('owners_equity')
        if owners is None and all(found.get(k) is not None for k in ('share_capital','other_equity')):owners=found['share_capital']+found['other_equity']
        if owners is not None and (basis=='standalone' or found.get('nci') is not None):equity=owners+(found.get('nci') or 0)
    if same(equity,latest.get('equity')) or (not derived_equity and equity is not None and found.get('nci') is not None and same(equity+found['nci'],latest.get('equity'))):matched.append('equity')
    borrow=[found.get(k+'_borrowings') for k in ('current','non_current')]
    if all(v is not None for v in borrow) and same(sum(borrow),latest.get('total_borrowings')):matched.append('total_borrowings')
    if len(matched)<2:raise ValueError('Fewer than two matching balance-sheet anchors')
    parts=[found.get(k+'_lease') for k in ('current','non_current')]
    if all(v is not None and v>=0 for v in parts):value=sum(parts)
    elif not any(k.endswith('_lease') and k!='total_lease' for k in found) and found.get('total_lease') is not None and found['total_lease']>=0:value=found['total_lease']
    else:value=None
    values={}
    if value is not None:values['total_lease_liabilities']=value
    if all(v is not None and v>=0 for v in borrow):values['total_borrowings']=sum(borrow)
    if found.get('cash') is not None and found['cash']>=0:values['cash_and_cash_equivalents']=found['cash']
    if not allow_existing and not any(latest.get(k) is None for k in values):raise ValueError('No missing balance input safely recovered; explicit lease components may be unavailable')
    return {'value':value,'values':values,'period':latest['period'],'statement_type':basis,'units_in':'crore','source_units':unit,
            'conversion_to_crore':scale,'source_currency':'INR','matched_fields':matched,'components':{k:v for k,v in found.items() if k.endswith('_lease')},'lines':evidence}

def balance_text(page,clip=None):
    """Resolve only documented rupee-font glyphs next to an explicit unit label."""
    kwargs={'clip':clip} if clip is not None else {}
    text=page.get_text(sort=True,**kwargs)
    for block in page.get_text('dict',**kwargs)['blocks']:
        for line in block.get('lines',[]):
            spans=line['spans'];joined=''.join(span.get('text','') for span in spans)
            if not re.search(r'in\s+(?:lakh|crore|million|thousand)s?',joined,re.I):continue
            for span in spans:
                glyph=span.get('text','').strip()
                if span.get('font')=='ITFRupee' and glyph in ('H','C','I'):
                    text=re.sub(r'(?<!\w)'+glyph+r'\s+(?=in\s+(?:lakh|crore|million|thousand)s?)','₹ ',text,flags=re.I)
    return text

def pdf_worker(pdf_path,context_path,output_path):
    import pymupdf
    context=json.loads(Path(context_path).read_text());latest=context['latest'];doc=pymupdf.open(pdf_path)
    if len(doc)>1200:raise ValueError('Annual report exceeds page limit')
    if context.get('require_pdf_isin') and not any(context['isin'] in re.sub(r'\s+','',p.get_text()) for p in doc):
        Path(output_path).write_text(json.dumps({'status':'unresolved','reason':'Company PDF does not contain the expected ISIN'}));return
    candidates=[];issues=[];diagnostics=[];basis_context=None;lease_notes=[]
    plain_pages=[p.get_text() for p in doc]
    if latest.get('total_lease_liabilities') is None:
        for j,plain in enumerate(plain_pages):
            if not re.search(LEASE_ROLLFORWARD,' '.join(plain.split()),re.I):continue
            page=doc[j];clips=[None]
            if page.rect.width>page.rect.height*1.15:
                w,h=page.rect.width,page.rect.height;clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
            for clip in clips:
                try:
                    note=parse_lease_note(balance_text(page,clip),latest);note['pages']=[j+1];lease_notes.append((note,j))
                except ValueError:pass
    for i,page in enumerate(doc):
        plain=plain_pages[i];low=' '.join(plain.lower().split())
        audit=re.findall(r'report on the audit of (?:the )?(standalone|consolidated) financial statements',low[:6000])
        if len(set(audit))==1:basis_context=(audit[0],i)
        # Cheap text pass; expensive layout text only for actual statement pages.
        if not re.search(r'\bbalance\s+sheet\b',low) or not ('cash and cash equivalents' in low or 'equity and liabilities' in low):continue
        clips=[None]
        # Printed two-page spreads must not interleave balance and income columns.
        if page.rect.width>page.rect.height*1.15:
            w,h=page.rect.width,page.rect.height
            clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
        for clip in clips:
            selected=[i];text=balance_text(page,clip);compact=' '.join(text.lower().split())
            if not re.search(r'\bbalance\s+sheet\b',compact[:1000]):continue
            # A landscape sheet can contain two consecutive printed balance
            # pages, rather than a balance sheet beside an income statement.
            joined_spread=False
            if clip is not None and clip.x0==0:
                right=balance_text(page,pymupdf.Rect(page.rect.width/2,0,page.rect.width,page.rect.height))
                right_head=' '.join(right.lower().split())[:700]
                if re.search(r'balance\s+sheet\s*\(?cont',right_head) and not re.search(r'total\s+(?:equity and liabilities|liabilities and equity)',compact):
                    # Both halves must state the same annual financial context.
                    try:
                        statement_context(text,latest);statement_context(right,latest)
                        text+='\n'+right;compact=' '.join(text.lower().split());joined_spread=True
                    except ValueError:pass
            if i+1<len(doc) and not re.search(r'total equity and liabilities|total liabilities and equity',compact):
                following=balance_text(doc[i+1],clip)
                if not re.search(r'statement of (?:profit|cash|changes)', ' '.join(following.lower().split())[:700]):text+='\n'+following;selected.append(i+1)
            contextual=False
            if not re.search(r'\b(?:standalone|consolidated)\b',compact[:1800]) and basis_context and 0<i-basis_context[1]<=12:
                text=basis_context[0]+'\n'+text;contextual=True
            try:
                income_needed=latest.get('finance_costs') is None and latest.get('profit_before_tax') is not None
                evidence=parse_balance(text,latest,allow_existing=income_needed or bool(lease_notes));evidence['pages']=[n+1 for n in selected]
                if 'total_lease_liabilities' not in evidence['values'] and lease_notes and len({round(v['value'],6) for v,_ in lease_notes})==1 and sum(v for v in evidence['components'].values() if v is not None)<=lease_notes[0][0]['value']+.015:
                    note,j=lease_notes[0];evidence['values']['total_lease_liabilities']=note['value'];evidence['value']=note['value'];evidence['lease_note_evidence']=note
                    if j not in selected:selected.append(j);evidence['pages'].append(j+1)
                if income_needed:
                    incomes=[];income_issues=[]
                    for j in range(i,min(i+3,len(doc))):
                        income_page=doc[j];income_clips=[None]
                        if income_page.rect.width>income_page.rect.height*1.15:
                            w,h=income_page.rect.width,income_page.rect.height
                            income_clips.extend([pymupdf.Rect(0,0,w/2,h),pymupdf.Rect(w/2,0,w,h)])
                        for income_clip in income_clips:
                            income_text=balance_text(income_page,income_clip)
                            if not re.search(r'statement\s+of\s+(?:(?:standalone|consolidated)\s+)?profit',' '.join(income_text.lower().split())[:1000]):continue
                            try:
                                income=parse_income(income_text,latest)
                                income['pages']=[j+1];incomes.append((income,j))
                            except ValueError as error:income_issues.append({'page':j+1,'reason':str(error)})
                    evidence['income_diagnostics']=income_issues
                    if incomes and len({round(v['value'],6) for v,_ in incomes})==1:
                        income,j=incomes[0];evidence['values']['finance_costs']=income['value'];evidence['income_evidence']=income
                        if j not in selected:selected.append(j);evidence['pages'].append(j+1)
                if not any(latest.get(k) is None for k in evidence['values']):
                    reasons='; '.join(dict.fromkeys(x['reason'] for x in evidence.get('income_diagnostics',[])))
                    raise ValueError('No missing annual input safely recovered'+(': '+reasons if reasons else ''))
                if contextual:
                    evidence['basis_evidence_page']=basis_context[1]+1
                    evidence['basis_evidence']='Report on the audit of the '+basis_context[0]+' financial statements'
                    selected.append(basis_context[1])
                evidence['layout']='joined_balance_spread' if joined_spread else 'full_page' if clip is None else 'left_half' if clip.x0==0 else 'right_half'
                candidates.append((evidence,selected))
            except ValueError as error:
                issues.append(str(error));diagnostics.append({'page':i+1,'reason':str(error),'layout':'full_page' if clip is None else 'half_page'})
    if not candidates:result={'status':'unresolved','reason':'; '.join(dict.fromkeys(issues))[:400] or 'No supported balance sheet with explicit lease amounts'}
    elif len({json.dumps(e['values'],sort_keys=True) for e,_ in candidates})!=1:result={'status':'unresolved','reason':'Annual report has conflicting matching balance sheets'}
    else:
        evidence,selected=candidates[0];excerpt=pymupdf.open()
        for n in selected:excerpt.insert_pdf(doc,from_page=n,to_page=n)
        evidence_path=Path(output_path).with_suffix('.pdf');excerpt.save(evidence_path,garbage=4,deflate=True,no_new_id=True)
        result={'status':'verified','evidence':evidence,'excerpt':str(evidence_path)}
    result['diagnostics']=diagnostics[:60]
    Path(output_path).write_text(json.dumps(result))

def save_excerpt(folder,source,raw_hash):
    excerpt_hash=hashlib.sha256(Path(source).read_bytes()).hexdigest()
    target=folder/(raw_hash[:16]+'-'+excerpt_hash[:16]+'.pdf')
    if not target.exists():shutil.copy2(source,target)
    if hashlib.sha256(target.read_bytes()).hexdigest()!=excerpt_hash:raise ValueError('Saved evidence hash mismatch')
    return target,excerpt_hash

class AnnualReportSources:
    def fetch(self,symbol,isin,details):
        from .financial_sources import request
        from .fallbacks import Fallbacks
        from .report_sources import bse_reports,company_reports,public_request,fresh,COMPANY_PAGES
        latest=details.get('latest') or {}
        income_needed=latest.get('finance_costs') is None and latest.get('profit_before_tax') is not None
        folder=ROOT/'data/annual-reports'/symbol;folder.mkdir(parents=True,exist_ok=True)
        key=hashlib.sha256(json.dumps(latest,sort_keys=True).encode()).hexdigest()[:16]
        cache=folder/(key+'.json')
        if cache.exists():
            old=json.loads(cache.read_text())
            if old.get('revision')==REVISION and (not income_needed or old.get('income_revision')==INCOME_REVISION) and old.get('symbol')==symbol and old.get('isin')==isin and fresh(old):
                evidence=ROOT/old.get('evidence_path','missing')
                if old.get('status')!='verified' or (evidence.is_file() and hashlib.sha256(evidence.read_bytes()).hexdigest()==old.get('excerpt_sha256')):return old
        result={'revision':REVISION,'income_revision':INCOME_REVISION,'symbol':symbol,'isin':isin,'observed_at':now(),'status':'unresolved','source_checks':[],'attempts':[]}
        client=Fallbacks();year=None;seen_hashes=set()
        try:year=datetime.strptime(latest.get('period',''),'%b %Y').year
        except ValueError:pass
        def discover(provider):
            check={'provider':provider};result['source_checks'].append(check)
            try:
                if provider=='BSE':choices=bse_reports(client.session,isin,year,folder)
                elif provider=='Company website':choices=company_reports(client.session,isin,year,folder)
                else:
                    path=folder/'index.json';index=json.loads(path.read_text()) if path.exists() else {}
                    if index.get('symbol')!=symbol or not fresh(index):
                        response=request(client.session,'https://www.nseindia.com/api/annual-reports',params={'index':'equities','symbol':symbol})
                        try:index={'symbol':symbol,'observed_at':now(),'payload':response.json()}
                        finally:response.close()
                        atomic_json(path,index)
                    choices=[]
                    for row in index.get('payload',{}).get('data',[]):
                        url=row.get('fileName','');u=urlsplit(url)
                        if year and str(row.get('toYr'))!=str(year):continue
                        if u.scheme!='https' or u.hostname!='nsearchives.nseindia.com' or not u.path.startswith('/annual_reports/') or not u.path.lower().endswith('.pdf') or '_'+symbol+'_' not in u.path:continue
                        choices.append({**row,'provider':'NSE'})
                        if len(choices)>=2:break
                check.update(status='available' if choices else 'unavailable',reports=len(choices))
                return choices
            except Exception as error:
                check.update(status='unavailable',reason=str(error)[:220] if isinstance(error,ValueError) else type(error).__name__)
                return []
        def parse(row):
            url=row['fileName'];attempt={'provider':row['provider'],'source_url':url};result['attempts'].append(attempt)
            try:
                with _pdf_slots:
                    if shutil.disk_usage(ROOT).free<1_000_000_000:raise ValueError('Less than 1 GB free; PDF fetch deferred')
                    with tempfile.TemporaryDirectory(prefix='report-',dir=ROOT/'data/annual-reports') as tmp:
                        tmp=Path(tmp);pdf=tmp/'report.pdf';size=0;digest=hashlib.sha256()
                        response=(request if row['provider']=='NSE' else public_request)(client.session,url,stream=True)
                        try:
                            with pdf.open('wb') as out:
                                for chunk in response.iter_content(131072):
                                    size+=len(chunk)
                                    if size>40_000_000:raise ValueError('Annual report exceeds 40 MB download limit')
                                    out.write(chunk);digest.update(chunk)
                        finally:response.close()
                        raw_hash=digest.hexdigest();attempt['raw_sha256']=raw_hash
                        if raw_hash in seen_hashes:raise ValueError('Identical report already parsed from another source')
                        seen_hashes.add(raw_hash)
                        context=tmp/'context.json';context.write_text(json.dumps({'latest':latest,'symbol':symbol,'isin':isin,'require_pdf_isin':row.get('require_pdf_isin',False)}))
                        output=tmp/'result.json'
                        subprocess.run([sys.executable,'-m','marketdesk.annual_reports',str(pdf),str(context),str(output)],timeout=90,check=True,capture_output=True)
                        parsed=json.loads(output.read_text());attempt.update({k:v for k,v in parsed.items() if k not in ('excerpt','evidence')})
                        if parsed['status']=='verified':
                            target,excerpt_hash=save_excerpt(folder,parsed['excerpt'],raw_hash)
                            result.update({k:v for k,v in parsed.items() if k!='excerpt'},source_url=url,provider=row['provider'],
                                index_url=row.get('index_url'),identity=row.get('identity'),raw_sha256=raw_hash,download_bytes=size,
                                evidence_path=str(target.relative_to(ROOT)),excerpt_sha256=excerpt_hash)
                            return True
            except Exception as error:attempt.update(status='unresolved',reason=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__)
            return False
        providers=['NSE']+(['Company website'] if isin in COMPANY_PAGES else [])+['BSE']
        for provider in providers:
            choices=discover(provider)
            # Discover alternate sources even for records lacking a safe baseline,
            # but don't merge figures whose annual period or basis is unknown.
            if not year or not latest.get('statement_type'):continue
            if all(latest.get(k) is not None for k in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs')):continue
            if any(parse(row) for row in choices):break
        if result['status']!='verified':
            if not year or not latest.get('statement_type'):reason='No verified baseline annual period/basis; alternate report sources checked'
            elif all(latest.get(k) is not None for k in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs')):reason='Supported annual inputs present; other fields remain missing'
            elif result['attempts']:reason='; '.join(dict.fromkeys(a.get('reason','Unsupported report') for a in result['attempts']))
            else:reason='Matching annual-report PDF unavailable from checked sources'
            result['reason']=reason[:650]
        atomic_json(cache,result);return result

def apply_report(details,report):
    previous=details.get('annual_report_check') or {}
    if report.get('status')!='verified':
        details['annual_report_attempt']=report
        if previous.get('status')!='verified':details['annual_report_check']=report
        return details
    evidence=report.get('evidence',{});latest=details.get('latest') or {}
    if any(latest.get(k)!=evidence.get(k) for k in ('period','statement_type','units_in')):return details
    if len(set(evidence.get('matched_fields',[])))<2:return details
    if previous.get('status')=='verified' and previous.get('excerpt_sha256')!=report.get('excerpt_sha256'):
        history=details.setdefault('annual_report_history',[])
        if not any(r.get('excerpt_sha256')==previous.get('excerpt_sha256') for r in history):history.append(copy.deepcopy(previous))
    details['annual_report_check']=report
    latest=copy.deepcopy(latest);filled=[]
    values=evidence.get('values',{'total_lease_liabilities':evidence.get('value')})
    for key,value in values.items():
        if key not in ('total_lease_liabilities','total_borrowings','cash_and_cash_equivalents','finance_costs'):continue
        if not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:continue
        if key=='finance_costs':
            anchor=evidence.get('income_evidence',{}).get('matched_profit_before_tax');pbt=latest.get('profit_before_tax')
            if anchor is None or pbt is None or abs(anchor-pbt)>max(.015,abs(pbt)*.00002):continue
            if evidence['income_evidence'].get('value')!=value:continue
        if latest.get(key) is not None:
            existing=latest[key]
            if abs(existing-value)>max(.015,abs(existing)*.00002):
                conflict={'field':key,'selected_value':existing,'annual_report':value,'selected':details.get('selected_source','upstox'),
                          'period':latest['period'],'units':'crore','source_url':report['source_url']}
                conflicts=details.setdefault('source_conflicts',[])
                if conflict not in conflicts:conflicts.append(conflict)
            continue
        latest[key]=value;filled.append(key)
        latest.setdefault('sources',{})[key]={'source':'company_annual_report','source_url':report['source_url'],
           'observed_at':report['observed_at'],'raw_sha256':report['raw_sha256'],'evidence_path':report['evidence_path'],**evidence,'value':value}
        if key=='finance_costs':latest['sources'][key].update(evidence['income_evidence'])
        if key=='total_lease_liabilities' and evidence.get('lease_note_evidence'):latest['sources'][key].update(evidence['lease_note_evidence'])
    latest['missing_fields']=[k for k in latest.get('missing_fields',[]) if k not in filled];details['latest']=latest
    details['missing_fields']=[k for k in details.get('missing_fields',[]) if k not in filled]
    from .provider import FUNDAMENTAL_REQUIRED_FIELDS
    details['status']='COMPLETE' if details.get('sector') and all(latest.get(k) is not None for k in FUNDAMENTAL_REQUIRED_FIELDS) else 'PARTIAL'
    return details

def retain_report(details,previous,symbol,isin):
    """Keep verified supplements across raw imports, without refreshing their age.

    A changed financial context or changed anchors requires a fresh source check.
    The saved excerpt must still match its recorded SHA-256.
    """
    reports=previous.get('annual_report_history',[])+[previous.get('annual_report_check') or {}]
    for report in reports:details=_retain_one_report(details,previous,report,symbol,isin)
    return details

def _retain_one_report(details,previous,report,symbol,isin):
    if report.get('status')!='verified' or (report.get('symbol'),report.get('isin'))!=(symbol,isin):return details
    latest=details.get('latest') or {};old=previous.get('latest') or {};evidence=report.get('evidence') or {}
    if any(latest.get(k)!=evidence.get(k) for k in ('period','statement_type','units_in')):return details
    anchors=evidence.get('matched_fields',[])
    same=[k for k in set(anchors) if k in ('cash_and_cash_equivalents','equity','total_borrowings') and
          isinstance(latest.get(k),(int,float)) and isinstance(old.get(k),(int,float)) and
          abs(latest[k]-old[k])<=max(.015,abs(old[k])*.00002)]
    if len(same)<2:return details
    path=ROOT/report.get('evidence_path','missing')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=report.get('excerpt_sha256'):return details
    # Preserve relevant unresolved conflicts too; an import is not reconciliation.
    for conflict in previous.get('source_conflicts',[]):
        conflicts=details.setdefault('source_conflicts',[])
        if conflict not in conflicts:conflicts.append(copy.deepcopy(conflict))
    return apply_report(details,copy.deepcopy(report))

if __name__=='__main__':pdf_worker(*sys.argv[1:])
