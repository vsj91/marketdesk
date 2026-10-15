import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import *
from marketdesk.report_growth import extract_pdf,apply_growth
from marketdesk.annual_reports import save_excerpt
from marketdesk.intraday_data import atomic_json
from marketdesk.quality import audit
import pymupdf
s='HNDFDS';p=ROOT/'tmp/pdfs'/('oct15-'+s);source=json.loads((p/'source.json').read_text());r=json.loads((ROOT/'data/report-growth/latest.json').read_text());folder=ROOT/r['run_path']
with connect(DEFAULT_DB) as db:row=db.execute('select isin,details from instruments join fundamentals using(symbol) where symbol=?',(s,)).fetchone();d=json.loads(row['details'])
sha=hashlib.sha256((p/'report.pdf').read_bytes()).hexdigest();assert sha==source['hash'];e,ex=extract_pdf(p/'report.pdf',d['latest']);target,digest=save_excerpt(ROOT/'data/annual-reports'/s,ex,sha);stamp=now()
for k in ('current','prior'):e[k]['observed_at']=stamp
proof={'symbol':s,'isin':row['isin'],'status':'verified','source':'company_annual_report','provider':'NSE','source_url':source['url'],'raw_sha256':sha,'evidence_path':str(target.relative_to(ROOT)),'excerpt_sha256':digest,'observed_at':stamp,'evidence':e}
with connect(DEFAULT_DB) as db:
 current=json.loads(db.execute('select details from fundamentals where symbol=?',(s,)).fetchone()[0]);updated=apply_growth(current,proof,s,row['isin']);assert current['latest']==updated['latest'];db.execute('update fundamentals set details=? where symbol=?',(json.dumps(updated,allow_nan=False),s))
r['results']=[{'symbol':s,'status':'verified','evidence':proof} if x['symbol']==s else x for x in r['results']];r['recovered']=sum(x['status']=='verified' for x in r['results']);r.update(finished_at=now(),quality=audit(DEFAULT_DB));atomic_json(folder/'report.json',r);atomic_json(ROOT/'data/report-growth/latest.json',r)
doc=pymupdf.open(ex);page=doc[0];page.get_pixmap(clip=pymupdf.Rect(page.rect.width/2,0,page.rect.width,page.rect.height)).save(p/'income-review.png');print(s,r['quality']['investor_ready'],e['pages'])
