import json,hashlib,pymupdf,re
from concurrent.futures import ThreadPoolExecutor
from marketdesk.storage import ROOT,connect,DEFAULT_DB
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks
from marketdesk.annual_reports import balance_text
symbols=['AARVI','ACUTAAS','ADANIENT','ASHAPURMIN','ARVINDFASN','BAJAJCON','TTKPRESTIG','VINATIORGA']
def run(s):
 with connect(DEFAULT_DB) as db:d=json.loads(db.execute('select details from fundamentals where symbol=?',(s,)).fetchone()[0])
 a=d['annual_report_check'];url=next(x['source_url'] for x in a['attempts'] if x['provider']=='NSE');folder=ROOT/'tmp/pdfs'/s;folder.mkdir(parents=True,exist_ok=True);pdf=folder/'report.pdf'
 if not pdf.exists():
  with request(Fallbacks().session,url) as r:pdf.write_bytes(r.content)
 doc=pymupdf.open(pdf);pages=[]
 for i,p in enumerate(doc):
  low=' '.join(p.get_text().lower().split())
  if 'balance sheet' in low[:1800] and 'cash and cash equivalents' in low:
   pages.append(i+1);(folder/f'{i+1}.txt').write_text(balance_text(p))
 print(s,'basis',d['latest']['statement_type'],'balancepages',pages,flush=True)
with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(run,symbols))
