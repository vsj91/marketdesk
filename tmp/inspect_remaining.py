import sys,json,hashlib,re,sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path.cwd()))
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks
from marketdesk.annual_reports import balance_text
import pymupdf
symbols=sys.argv[1:];db=sqlite3.connect('data/marketdesk.sqlite3');rows=[]
for s in symbols:
 d=json.loads(db.execute('select details from fundamentals where symbol=?',(s,)).fetchone()[0]);proof=d.get('annual_report_check') or {}
 if not proof.get('attempts'):
  old=[json.loads(p.read_text()) for p in (Path('data/annual-reports')/s).glob('*.json')];old=[p for p in old if p.get('attempts')];proof=max(old,key=lambda p:p.get('observed_at','')) if old else {}
 a=next((a for a in proof.get('attempts',[]) if a.get('provider')=='NSE' and a.get('raw_sha256')),None)
 if a:rows.append((s,d,a))
 else:print(s,'no NSE attempt',flush=True)
def fetch(row):
 s,d,a=row
 try:
  path=Path('tmp/remaining-reviews')/s;path.mkdir(parents=True,exist_ok=True)
  pdf=path/'report.pdf'
  if pdf.exists():raw=pdf.read_bytes()
  else:
   response=request(Fallbacks().session,a['source_url'],stream=True);raw=bytearray()
   try:
    for chunk in response.iter_content(65536):
     raw.extend(chunk)
     if len(raw)>40000000:raise ValueError('Too large')
   finally:response.close()
  if hashlib.sha256(raw).hexdigest()!=a['raw_sha256']:raise ValueError('Hash changed')
  pdf.write_bytes(raw);(path/'context.json').write_text(json.dumps({'latest':d['latest']}));(path/'source.json').write_text(json.dumps(a))
  doc=pymupdf.open(stream=raw,filetype='pdf');pages=sorted({x['page'] for x in a.get('diagnostics',[]) if x.get('layout') in ('full_page','half_page')})
  if not pages:
   pages=[i+1 for i,p in enumerate(doc) if re.search(r'balance\s+sheet',' '.join(p.get_text().lower().split())) and 'cash and cash equivalents' in ' '.join(p.get_text().lower().split())]
  for p in pages:
   for n in range(max(0,p-3),min(p+2,len(doc))):
    page=doc[n];(path/f'{n+1}.txt').write_text(balance_text(page))
    if page.rect.width>page.rect.height*1.15:
     for name,x0,x1 in [('left',0,page.rect.width/2),('right',page.rect.width/2,page.rect.width)]:
      (path/f'{n+1}-{name}.txt').write_text(balance_text(page,pymupdf.Rect(x0,0,x1,page.rect.height)))
  print(s,pages,flush=True)
 except Exception as e:print(s,type(e).__name__,str(e)[:200],flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(fetch,rows))
