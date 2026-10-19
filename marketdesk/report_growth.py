"""Comparable annual growth from company-report income tables, with PDF evidence."""
import copy,hashlib,json,re,math,tempfile,unicodedata
from pathlib import Path
from datetime import datetime,timezone
import pymupdf
from .storage import ROOT,now
from .annual_reports import balance_text,statement_context,caption,amount,NUM,NOTE_REF,FORMULA_REF,save_excerpt,sole_entity_basis,SOLE_ENTITY_CLAUSE,BASIS_CONTEXT_PAGES
from .financial_sources import request
from .fallbacks import Fallbacks
from .growth_repair import checked_pair
from .intraday_data import atomic_json


def parse_growth(text,latest):
 text=unicodedata.normalize('NFKC',text)
 text,basis,(units,scale)=statement_context(text,latest,'income');found={};lines={};quanta={}
 labels={
  'revenue':{'revenue from operations','total revenue from operations','revenue from operations (net)','revenue from contracts with customers'},
  'net_profit':{'profit for the year','profit for the period','profit/(loss) for the year','profit/(loss) for the period','profit / (loss) for the year','profit / (loss) for the period','profit (loss) for the year','profit (loss) for the period','profit after tax','net profit after tax','net profit/(loss) after tax','profit/(loss) after tax','profit for the year after tax','net profit for the year after tax','profit for the period/year','profit for the year/period'},
  'profit_before_tax':{'profit before tax','profit/(loss) before tax','profit / (loss) before tax','profit (loss) before tax','profit before taxation','profit for the year before tax'},
  'finance_costs':{'finance costs','finance cost','finance costs (interest)','finance cost (interest)'}}
 for line in text.splitlines():
  m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
  if not m:continue
  label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:m.start()].strip(),flags=re.I);label=caption(label)
  label=re.sub(r'^[ivx]+[.)]?\s+','',label);label=re.sub(FORMULA_REF,'',label).strip()
  # A trailing single-letter row marker, e.g. "Profit / (Loss) for the year (A)".
  label=re.sub(r'\s+\([a-z]\)$','',label)
  keys=[k for k,options in labels.items() if label in options]
  if not keys:continue
  key=keys[0]
  if key in found:raise ValueError('Ambiguous repeated income row: '+key)
  vals=[amount(m[i]) for i in (1,2)]
  if any(v is None for v in vals):raise ValueError('Missing explicit comparative income: '+key)
  found[key]=[v*scale for v in vals];lines[key]=line.strip()
  token=m[1].replace(',','').strip('()-');quanta[key]=scale*10**(-len(token.split('.')[1]) if '.' in token else 0)
 if set(found)!=set(labels):raise ValueError('Explicit annual income rows missing: '+','.join(sorted(set(labels)-set(found))))
 current={k:v[0] for k,v in found.items()};prior={k:v[1] for k,v in found.items()}
 end=datetime.strptime(latest['period'],'%b %Y');import calendar
 day=calendar.monthrange(end.year,end.month)[1];date=end.replace(day=day).date()
 for row,delta in [(current,0),(prior,-1)]:
  period=date.replace(year=date.year+delta);row.update(period=period.strftime('%b %Y'),period_end=period.isoformat(),statement_type=basis,units_in='crore')
 for key in ('revenue','net_profit'):
  if latest.get(key) is not None and abs(current[key]-latest[key])>max(.015,abs(latest[key])*.00002,quanta[key]/2+1e-9):raise ValueError('Selected annual '+key+' differs from report')
 current['rounding_quanta']=quanta
 # checked_pair independently validates PBT, finance costs, annual context and
 # positive prior-year denominators. Observation times are supplied by caller.
 for row in (current,prior):row['observed_at']=now()
 checked_pair({'latest':latest},current,prior)
 return {'current':current,'prior':prior,'lines':lines,'source_units':units,'conversion_to_crore':scale}


def extract_pdf(path,latest,expected_isin=None):
 candidates=[];diagnostics=[];basis_context=None
 with pymupdf.open(path) as doc:
  if len(doc)>1200:raise ValueError("Annual report exceeds page limit")
  if expected_isin and not any(expected_isin in re.sub(r"\s+","",p.get_text()) for p in doc):raise ValueError("Company PDF does not contain the expected ISIN")
  sole=sole_entity_basis([p.get_text() for p in doc]) if latest.get('statement_type')=='standalone' else None
  for i,p in enumerate(doc):
   low=' '.join(unicodedata.normalize('NFKC',p.get_text()).lower().split())
   audit=re.findall(r'report on the audit of (?:the )?(standalone|consolidated) financial statements',low[:6000])
   if audit:basis_context=(audit[0],i) if len(set(audit))==1 else None
   elif sole and i==sole[0]:basis_context=('standalone',i)
   if not re.search(r'statement of (?:standalone |consolidated )?profit',low):continue
   clips=[('full_page',None)]
   if p.rect.width>p.rect.height*1.15:clips.extend([('left_half',pymupdf.Rect(0,0,p.rect.width/2,p.rect.height)),('right_half',pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height))])
   for layout,clip in clips:
    text=balance_text(p,clip);contextual=False
    if not re.search(r'\b(?:standalone|consolidated)\b',' '.join(text.lower().split())[:1800]) and basis_context and 0<i-basis_context[1]<=BASIS_CONTEXT_PAGES:
     text=basis_context[0]+'\n'+text;contextual=True
    try:
     parsed=parse_growth(text,latest);parsed.update(pages=[i+1],layout=layout)
     if contextual:
      parsed.update(basis_evidence_page=basis_context[1]+1,basis_evidence=basis_context[0])
      if sole and basis_context[1]==sole[0]:parsed.update(basis_method='sole_entity_no_subsidiary',basis_clause=sole[1])
     candidates.append(parsed)
    except ValueError as e:
     if re.search(r'statement of (?:standalone |consolidated )?profit',' '.join(text.lower().split())[:800]):diagnostics.append({'page':i+1,'reason':str(e)})
  if not candidates:raise ValueError('; '.join(dict.fromkeys(r['reason'] for r in diagnostics))[:600] or 'No supported annual income table')
  unique={json.dumps({k:v for k,v in p['current'].items() if k!='observed_at'},sort_keys=True)+json.dumps({k:v for k,v in p['prior'].items() if k!='observed_at'},sort_keys=True) for p in candidates}
  if len(unique)!=1:raise ValueError('Conflicting matching income tables')
  parsed=candidates[0];excerpt=pymupdf.open()
  for n in parsed['pages']+([parsed['basis_evidence_page']] if parsed.get('basis_evidence_page') else []):excerpt.insert_pdf(doc,from_page=n-1,to_page=n-1,widgets=False)
  output=Path(path).with_name('income-evidence.pdf');excerpt.save(output,garbage=4,deflate=True,no_new_id=True);excerpt.close()
  return parsed,output


def recover_report_growth(symbol,isin,details):
 """NSE, reviewed company sites, then ISIN-matched BSE reports.

 Provider failure or an unsupported PDF advances to the next source. Every
 accepted source still needs annual context and matching PBT/finance anchors.
 """
 from .report_sources import fresh,bse_reports,company_reports,public_request,COMPANY_PAGES,download_report
 from urllib.parse import urlsplit
 folder=ROOT/'data/annual-reports'/symbol;folder.mkdir(parents=True,exist_ok=True)
 path=folder/'index.json';client=Fallbacks();latest=details['latest']
 year=datetime.strptime(latest['period'],'%b %Y').year
 checks=[];errors=[];seen_hashes=set()
 providers=['NSE']+(['Company website'] if isin in COMPANY_PAGES else [])+['BSE']
 for provider in providers:
  check={'provider':provider};checks.append(check)
  try:
   if provider=='BSE':choices=bse_reports(client.session,isin,year,folder)
   elif provider=='Company website':choices=company_reports(client.session,isin,year,folder)
   else:
    index=json.loads(path.read_text()) if path.exists() else {}
    if index.get('symbol')!=symbol or not fresh(index):
     with request(client.session,'https://www.nseindia.com/api/annual-reports',params={'index':'equities','symbol':symbol}) as r:index={'symbol':symbol,'observed_at':now(),'payload':r.json()}
     atomic_json(path,index)
    choices=[]
    for row in index.get('payload',{}).get('data',[]):
     url=row.get('fileName','');u=urlsplit(url)
     if str(row.get('toYr'))==str(year) and u.scheme=='https' and u.hostname=='nsearchives.nseindia.com' and u.path.startswith('/annual_reports/') and u.path.lower().endswith('.pdf') and '_'+symbol+'_' in u.path:
      choices.append({**row,'provider':'NSE'})
   check.update(status='available' if choices else 'unavailable',reports=len(choices))
  except Exception as error:
   check.update(status='unavailable',reason=str(error)[:180]);errors.append(provider+': '+str(error)[:180]);continue
  for row in choices[:2]:
   url=row['fileName']
   try:
    with tempfile.TemporaryDirectory(dir=folder,prefix='income-') as tmp:
     pdf=Path(tmp)/'report.pdf'
     sha,size=download_report(request if provider=='NSE' else public_request,client.session,url,pdf)
     if sha in seen_hashes:raise ValueError('Identical report already checked')
     seen_hashes.add(sha)
     parsed,excerpt=extract_pdf(pdf,latest,expected_isin=isin if row.get('require_pdf_isin') else None)
     target,excerpt_hash=save_excerpt(folder,excerpt,sha);stamp=now()
     for values in (parsed['current'],parsed['prior']):values['observed_at']=stamp
     return {'symbol':symbol,'isin':isin,'status':'verified','source':'company_annual_report','provider':provider,'source_checks':checks,
             'source_url':url,'index_url':row.get('index_url'),'identity':row.get('identity'),'raw_sha256':sha,
             'evidence_path':str(target.relative_to(ROOT)),'excerpt_sha256':excerpt_hash,'observed_at':stamp,'evidence':parsed}
   except Exception as error:errors.append(provider+': '+str(error)[:220])
 raise ValueError('; '.join(errors) or 'Matching annual report absent from checked sources: '+', '.join(providers))


def apply_growth(details,proof,symbol,isin):
 if proof.get('status')!='verified' or (proof.get('symbol'),proof.get('isin'))!=(symbol,isin):raise ValueError('Income-report identity mismatch')
 raw=(ROOT/proof['evidence_path']).read_bytes()
 if hashlib.sha256(raw).hexdigest()!=proof['excerpt_sha256']:raise ValueError('Income PDF hash mismatch')
 texts=[];e=proof['evidence']
 with pymupdf.open(stream=raw,filetype='pdf') as doc:
  for p in list(doc)[:len(e['pages'])]:
   clip=None
   if e['layout']=='left_half':clip=pymupdf.Rect(0,0,p.rect.width/2,p.rect.height)
   elif e['layout']=='right_half':clip=pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height)
   elif e['layout']!='full_page':raise ValueError('Unsupported income layout')
   texts.append(balance_text(p,clip))
  if e.get('basis_evidence_page'):
   audit=' '.join(doc[-1].get_text().lower().split());bases=re.findall(r'report on the audit of (?:the )?(standalone|consolidated) financial statements',audit[:6000])
   if e.get('basis_method')=='sole_entity_no_subsidiary':
    if e['basis_evidence']!='standalone' or bases or not re.search(SOLE_ENTITY_CLAUSE,audit):raise ValueError('Auditor no-subsidiary statement changed')
   elif set(bases)!={e['basis_evidence']}:raise ValueError('Auditor basis context changed')
   texts.insert(0,e['basis_evidence'])
 parsed=parse_growth('\n'.join(texts),details['latest'])
 for name in ('current','prior'):
  parsed[name]['observed_at']=proof['observed_at']
  if parsed[name]!=e[name]:raise ValueError('Income evidence changed')
 summary=checked_pair(details,parsed['current'],parsed['prior'])
 summary['growth_sources']={k:'company_annual_report' for k in ('revenue','net_profit') if summary.get(k+'_growth_yoy') is not None}
 for entry in (summary.get('growth_inputs') or {}).values():entry['source']='company_annual_report'
 out=copy.deepcopy(details);out['summary']=summary;out['company_growth_repair']=copy.deepcopy(proof);return out

def retain_company_growth(details,previous,symbol,isin):
 proof=previous.get('company_growth_repair')
 if not proof:return details
 try:return apply_growth(details,proof,symbol,isin)
 except (ValueError,KeyError,OSError):return details
