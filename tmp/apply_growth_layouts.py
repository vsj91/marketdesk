import json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import *
from marketdesk.report_growth import extract_pdf,apply_growth
from marketdesk.annual_reports import save_excerpt
from marketdesk.intraday_data import atomic_json
from marketdesk.quality import audit
symbols=['CYIENT','BLUESTONE','PROZONER','HNDFDS'];started=now();folder=ROOT/'data/report-growth'/started.replace(':','-')
with connect(DEFAULT_DB) as db:
 rows=[dict(r) for r in db.execute('select i.symbol,i.isin,f.details,q.report from instruments i join fundamentals f using(symbol) join quality q using(symbol)') if r['symbol'] in symbols];quality=get_state(db,'quality_summary',{})
atomic_json(folder/'before.json',rows);report={'status':'running','started_at':started,'total':len(rows),'done':0,'recovered':0,'quality_before':quality,'results':[],'mode':'local_paper_only','applied':True,'run_path':str(folder.relative_to(ROOT))}
for row in rows:
 s=row['symbol'];entry={'symbol':s};d=json.loads(row['details']);p=ROOT/'tmp/pdfs'/('oct15-'+s)
 try:
  source=json.loads((p/'source.json').read_text());idx=json.loads((ROOT/'data/annual-reports'/s/'index.json').read_text());url=source['url'];sha=hashlib.sha256((p/'report.pdf').read_bytes()).hexdigest()
  assert sha==source['hash'] and url.startswith('https://nsearchives.nseindia.com/annual_reports/') and '_'+s+'_' in url and any(r['fileName']==url and str(r['toYr'])=='2026' for r in idx['payload']['data'])
  e,ex=extract_pdf(p/'report.pdf',d['latest']);target,digest=save_excerpt(ROOT/'data/annual-reports'/s,ex,sha);stamp=now()
  for k in ('current','prior'):e[k]['observed_at']=stamp
  proof={'symbol':s,'isin':row['isin'],'status':'verified','source':'company_annual_report','provider':'NSE','source_url':url,'raw_sha256':sha,'evidence_path':str(target.relative_to(ROOT)),'excerpt_sha256':digest,'observed_at':stamp,'evidence':e}
  with connect(DEFAULT_DB) as db:
   current=json.loads(db.execute('select details from fundamentals where symbol=?',(s,)).fetchone()[0]);updated=apply_growth(current,proof,s,row['isin']);assert current['latest']==updated['latest'];db.execute('update fundamentals set details=? where symbol=?',(json.dumps(updated,allow_nan=False),s))
  entry.update(status='verified',evidence=proof);report['recovered']+=1
 except Exception as err:entry.update(status='unresolved',reason=str(err))
 report['done']+=1;report['results'].append(entry);atomic_json(folder/'report.json',report);print(s,entry['status'],entry.get('reason',''),flush=True)
report.update(status='complete',finished_at=now(),quality=audit(DEFAULT_DB));atomic_json(folder/'report.json',report);atomic_json(ROOT/'data/report-growth/latest.json',report);print(report['run_path'],report['recovered'],report['quality']['investor_ready'],flush=True)
