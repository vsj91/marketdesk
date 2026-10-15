import sys,json,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path.cwd()))
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks
from marketdesk.annual_reports import balance_text
import pymupdf
r=json.load(open('data/investor-inputs/2026-10-15T06-50-16.367527+00-00/report.json'));targets={'CEATLTD','MEDANTA','CROMPTON','DIVISLAB','AMBUJACEM'}
def fetch(e):
 s=e['symbol'];a=next(a for a in e['annual_report']['attempts'] if a.get('provider')=='NSE' and a.get('diagnostics'))
 path=Path('tmp/full-balance-reviews')/s;path.mkdir(parents=True,exist_ok=True)
 response=request(Fallbacks().session,a['source_url'],stream=True)
 raw=bytearray()
 for chunk in response.iter_content(65536):
  raw.extend(chunk)
  if len(raw)>40000000:raise ValueError('Too large')
 response.close()
 if hashlib.sha256(raw).hexdigest()!=a['raw_sha256']:raise ValueError('Hash changed')
 (path/'report.pdf').write_bytes(raw)
 doc=pymupdf.open(stream=raw,filetype='pdf');pages=sorted({x['page'] for x in a['diagnostics'] if x.get('layout') in ('full_page','half_page')})
 out=pymupdf.open()
 for p in pages:
  out.insert_pdf(doc,from_page=p-1,to_page=min(p,len(doc)-1))
  (path/f'{p}.txt').write_text(balance_text(doc[p-1]))
 out.save(path/'excerpt.pdf')
 print(s,pages,flush=True)
with ThreadPoolExecutor(max_workers=4) as p:list(p.map(fetch,[e for e in r['results'] if e['symbol'] in targets]))
