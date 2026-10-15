import sys,json,re,hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path.cwd()))
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks
from marketdesk.annual_reports import balance_text
import pymupdf

def fetch(s):
 p=Path('tmp/growth-context-reviews')/s;p.mkdir(parents=True,exist_ok=True)
 r=json.loads((Path('data/annual-reports')/s/'index.json').read_text());a=next(x for x in r['payload']['data'] if str(x['toYr'])=='2026');url=a['fileName']
 assert url.startswith('https://nsearchives.nseindia.com/annual_reports/') and '_'+s+'_' in url
 raw=bytearray()
 with request(Fallbacks().session,url,stream=True) as response:
  for chunk in response.iter_content(65536):
   raw.extend(chunk)
   if len(raw)>40000000:raise ValueError('too large')
 (p/'report.pdf').write_bytes(raw);(p/'source.json').write_text(json.dumps({'source_url':url,'raw_sha256':hashlib.sha256(raw).hexdigest()}));doc=pymupdf.open(stream=raw,filetype='pdf');pages=[]
 for i,page in enumerate(doc):
  plain=' '.join(page.get_text().lower().split())
  if re.search(r'statement\s+of\s+(?:consolidated\s+|standalone\s+)?profit',plain) and 'finance cost' in plain:
   pages.append(i+1);(p/f'{i+1}.txt').write_text(balance_text(page))
   if page.rect.width>page.rect.height*1.15:
    for name,x0,x1 in [('left',0,page.rect.width/2),('right',page.rect.width/2,page.rect.width)]:
     (p/f'{i+1}-{name}.txt').write_text(balance_text(page,pymupdf.Rect(x0,0,x1,page.rect.height)))
 print(s,pages,flush=True)
with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(fetch,sys.argv[1:]))
