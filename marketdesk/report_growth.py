"""Comparable annual growth from company-report income tables, with PDF evidence."""
import copy,hashlib,json,re,math,tempfile,unicodedata
from pathlib import Path
from datetime import datetime,timezone
import pymupdf
from .storage import ROOT,now
from .annual_reports import balance_text,statement_context,caption,amount,NUM,NOTE_REF,save_excerpt
from .financial_sources import request
from .fallbacks import Fallbacks
from .growth_repair import checked_pair
from .intraday_data import atomic_json


def parse_growth(text,latest):
 text=unicodedata.normalize('NFKC',text)
 text,basis,(units,scale)=statement_context(text,latest,'income');found={};lines={}
 labels={
  'revenue':{'revenue from operations','total revenue from operations','revenue from operations (net)'},
  'net_profit':{'profit for the year','profit for the period','profit/(loss) for the year','profit/(loss) for the period','profit / (loss) for the year','profit / (loss) for the period','profit (loss) for the year','profit (loss) for the period','profit after tax','net profit after tax','net profit/(loss) after tax','profit/(loss) after tax','profit for the year after tax','profit for the period/year','profit for the year/period'},
  'profit_before_tax':{'profit before tax','profit/(loss) before tax','profit / (loss) before tax','profit (loss) before tax','profit before taxation'},
  'finance_costs':{'finance costs','finance cost','finance costs (interest)','finance cost (interest)'}}
 for line in text.splitlines():
  m=re.search(r'\s{2,}('+NUM+r')\s{2,}('+NUM+r')\s*$',line)
  if not m:continue
  label=re.sub(r'\s{2,}'+NOTE_REF+r'\s*$','',line[:m.start()].strip(),flags=re.I);label=caption(label)
  label=re.sub(r'^[ivx]+[.)]?\s+','',label);label=re.sub(r'\s*\([ivx0-9+\-\s]+\)$','',label).strip()
  keys=[k for k,options in labels.items() if label in options]
  if not keys:continue
  key=keys[0]
  if key in found:raise ValueError('Ambiguous repeated income row: '+key)
  vals=[amount(m[i]) for i in (1,2)]
  if any(v is None for v in vals):raise ValueError('Missing explicit comparative income: '+key)
  found[key]=[v*scale for v in vals];lines[key]=line.strip()
 if set(found)!=set(labels):raise ValueError('Explicit annual income rows missing: '+','.join(sorted(set(labels)-set(found))))
 current={k:v[0] for k,v in found.items()};prior={k:v[1] for k,v in found.items()}
 end=datetime.strptime(latest['period'],'%b %Y');import calendar
 day=calendar.monthrange(end.year,end.month)[1];date=end.replace(day=day).date()
 for row,delta in [(current,0),(prior,-1)]:
  period=date.replace(year=date.year+delta);row.update(period=period.strftime('%b %Y'),period_end=period.isoformat(),statement_type=basis,units_in='crore')
 for key in ('revenue','net_profit'):
  if latest.get(key) is not None and abs(current[key]-latest[key])>max(.015,abs(latest[key])*.00002):raise ValueError('Selected annual '+key+' differs from report')
 # checked_pair independently validates PBT, finance costs, annual context and
 # positive prior-year denominators. Observation times are supplied by caller.
 for row in (current,prior):row['observed_at']=now()
 checked_pair({'latest':latest},current,prior)
 return {'current':current,'prior':prior,'lines':lines,'source_units':units,'conversion_to_crore':scale}


def extract_pdf(path,latest):
 candidates=[];diagnostics=[];basis_context=None
 with pymupdf.open(path) as doc:
  for i,p in enumerate(doc):
   low=' '.join(unicodedata.normalize('NFKC',p.get_text()).lower().split())
   audit=re.findall(r'report on the audit of (?:the )?(standalone|consolidated) financial statements',low[:6000])
   if len(set(audit))==1:basis_context=(audit[0],i)
   if not re.search(r'statement of (?:standalone |consolidated )?profit',low):continue
   clips=[('full_page',None)]
   if p.rect.width>p.rect.height*1.15:clips.extend([('left_half',pymupdf.Rect(0,0,p.rect.width/2,p.rect.height)),('right_half',pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height))])
   for layout,clip in clips:
    text=balance_text(p,clip);contextual=False
    if not re.search(r'\b(?:standalone|consolidated)\b',' '.join(text.lower().split())[:1800]) and basis_context and 0<i-basis_context[1]<=12:
     text=basis_context[0]+'\n'+text;contextual=True
    try:
     parsed=parse_growth(text,latest);parsed.update(pages=[i+1],layout=layout)
     if contextual:parsed.update(basis_evidence_page=basis_context[1]+1,basis_evidence=basis_context[0])
     candidates.append(parsed)
    except ValueError as e:
     if re.search(r'statement of (?:standalone |consolidated )?profit',' '.join(text.lower().split())[:800]):diagnostics.append({'page':i+1,'reason':str(e)})
  if not candidates:raise ValueError('; '.join(dict.fromkeys(r['reason'] for r in diagnostics))[:600] or 'No supported annual income table')
  unique={json.dumps({k:v for k,v in p['current'].items() if k!='observed_at'},sort_keys=True)+json.dumps({k:v for k,v in p['prior'].items() if k!='observed_at'},sort_keys=True) for p in candidates}
  if len(unique)!=1:raise ValueError('Conflicting matching income tables')
  parsed=candidates[0];excerpt=pymupdf.open()
  for n in parsed['pages']+([parsed['basis_evidence_page']] if parsed.get('basis_evidence_page') else []):excerpt.insert_pdf(doc,from_page=n-1,to_page=n-1)
  output=Path(path).with_name('income-evidence.pdf');excerpt.save(output,garbage=4,deflate=True,no_new_id=True);excerpt.close()
  return parsed,output


def recover_report_growth(symbol,isin,details):
 from .report_sources import fresh
 from urllib.parse import urlsplit
 folder=ROOT/'data/annual-reports'/symbol;folder.mkdir(parents=True,exist_ok=True);path=folder/'index.json';index=json.loads(path.read_text()) if path.exists() else {};client=Fallbacks();latest=details['latest']
 if index.get('symbol')!=symbol or not fresh(index):
  with request(client.session,'https://www.nseindia.com/api/annual-reports',params={'index':'equities','symbol':symbol}) as r:index={'symbol':symbol,'observed_at':now(),'payload':r.json()}
  atomic_json(path,index)
 year=datetime.strptime(latest['period'],'%b %Y').year
 choices=[r['fileName'] for r in index.get('payload',{}).get('data',[]) if str(r.get('toYr'))==str(year) and urlsplit(r.get('fileName','')).scheme=='https' and urlsplit(r.get('fileName','')).hostname=='nsearchives.nseindia.com' and '/annual_reports/' in r['fileName'] and '_'+symbol+'_' in r['fileName']]
 if not choices:raise ValueError('Matching company annual report absent from NSE index')
 errors=[]
 for url in choices[:2]:
  try:
   with tempfile.TemporaryDirectory(dir=folder,prefix='income-') as tmp:
    pdf=Path(tmp)/'report.pdf';digest=hashlib.sha256();size=0
    with request(client.session,url,stream=True) as r,pdf.open('wb') as out:
     for chunk in r.iter_content(131072):
      size+=len(chunk)
      if size>40_000_000:raise ValueError('Report exceeds 40 MB')
      digest.update(chunk);out.write(chunk)
    parsed,excerpt=extract_pdf(pdf,latest);sha=digest.hexdigest();target,excerpt_hash=save_excerpt(folder,excerpt,sha);stamp=now()
    for row in (parsed['current'],parsed['prior']):row['observed_at']=stamp
    return {'symbol':symbol,'isin':isin,'status':'verified','source':'company_annual_report','source_url':url,'raw_sha256':sha,'evidence_path':str(target.relative_to(ROOT)),'excerpt_sha256':excerpt_hash,'observed_at':stamp,'evidence':parsed}
  except ValueError as e:errors.append(str(e))
 raise ValueError('; '.join(errors))


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
   if set(bases)!={e['basis_evidence']}:raise ValueError('Auditor basis context changed')
   texts.insert(0,e['basis_evidence'])
 parsed=parse_growth('\n'.join(texts),details['latest'])
 for name in ('current','prior'):
  parsed[name]['observed_at']=proof['observed_at']
  if parsed[name]!=e[name]:raise ValueError('Income evidence changed')
 summary=checked_pair(details,parsed['current'],parsed['prior'])
 summary['growth_sources']={k:'company_annual_report' for k in ('revenue','net_profit')}
 out=copy.deepcopy(details);out['summary']=summary;out['company_growth_repair']=copy.deepcopy(proof);return out

def retain_company_growth(details,previous,symbol,isin):
 proof=previous.get('company_growth_repair')
 if not proof:return details
 try:return apply_growth(details,proof,symbol,isin)
 except (ValueError,KeyError,OSError):return details
