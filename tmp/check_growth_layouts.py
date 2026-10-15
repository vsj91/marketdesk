import json,sys,hashlib,re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import *
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks
from marketdesk.annual_reports import balance_text
from marketdesk.report_growth import parse_growth
import pymupdf

def check(s):
 folder=ROOT/'tmp/pdfs'/('oct15-'+s);folder.mkdir(exist_ok=True)
 idx=json.loads((ROOT/'data/annual-reports'/s/'index.json').read_text());url=next(x['fileName'] for x in idx['payload']['data'] if str(x['toYr'])=='2026' and '_'+s+'_' in x['fileName']);pdf=folder/'report.pdf'
 if not pdf.exists():
  size=0
  with request(Fallbacks().session,url,stream=True) as r,pdf.open('wb') as f:
   for c in r.iter_content(131072):
    size+=len(c)
    if size>40000000:raise ValueError('Too large')
    f.write(c)
 with connect(DEFAULT_DB) as db:d=json.loads(db.execute('select details from fundamentals where symbol=?',(s,)).fetchone()[0])
 (folder/'source.json').write_text(json.dumps({'url':url,'hash':hashlib.sha256(pdf.read_bytes()).hexdigest(),'latest':d['latest']}))
 doc=pymupdf.open(pdf)
 for i,p in enumerate(doc):
  text=p.get_text();head=' '.join(text.lower().split())[:1000]
  if not re.search(r'statement of (?:standalone |consolidated )?profit',head):continue
  clips=[None] if p.rect.width<p.rect.height*1.15 else [pymupdf.Rect(0,0,p.rect.width/2,p.rect.height),pymupdf.Rect(p.rect.width/2,0,p.rect.width,p.rect.height)]
  for j,clip in enumerate(clips):
   t=balance_text(p,clip);(folder/f'{i+1}-{j}.txt').write_text(t)
   try:parse_growth(t,d['latest']);reason='OK'
   except ValueError as e:reason=str(e)
   print(s,i+1,j,reason,flush=True)
with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(check,['CYIENT','BLUESTONE','PROZONER','HNDFDS']))
