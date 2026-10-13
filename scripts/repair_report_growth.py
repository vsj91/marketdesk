#!/usr/bin/env python3
import sys,json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,get_state,now
from marketdesk.report_growth import recover_report_growth,apply_growth
from marketdesk.intraday_data import atomic_json
from marketdesk.quality import audit
apply='--apply' in sys.argv;previous=json.loads((ROOT/'data/investor-growth/latest.json').read_text());symbols=[r['symbol'] for r in previous['results'] if r.get('reason')=='Matching prior-year filing absent from saved NSE index']
with connect(DEFAULT_DB) as db:
 rows=[dict(r) for r in db.execute('select i.symbol,i.isin,f.details,q.report from instruments i join fundamentals f using(symbol) join quality q using(symbol)') if r['symbol'] in symbols and json.loads(r['details'])['latest']['profit_before_tax']>0 and not json.loads(r['report'])['investor_ready']]
 quality=get_state(db,'quality_summary',{})
started=now();folder=ROOT/'data/report-growth'/started.replace(':','-');atomic_json(folder/'before.json',rows);report={'status':'running','started_at':started,'total':len(rows),'done':0,'recovered':0,'quality_before':quality,'results':[],'mode':'local_paper_only','applied':apply,'run_path':str(folder.relative_to(ROOT))}
saved=ROOT/'data/report-growth/latest.json'
verified={r['symbol']:r['evidence'] for r in json.loads(saved.read_text())['results'] if r['status']=='verified'} if saved.exists() else {}
def fetch(r):
 if r['symbol'] in verified:return verified[r['symbol']]
 return recover_report_growth(r['symbol'],r['isin'],json.loads(r['details']))
with ThreadPoolExecutor(max_workers=4) as pool:
 futures={pool.submit(fetch,r):r for r in rows}
 for f in as_completed(futures):
  row=futures[f];entry={'symbol':row['symbol']}
  try:
   proof=f.result()
   with connect(DEFAULT_DB) as db:
    d=json.loads(db.execute('select details from fundamentals where symbol=?',(row['symbol'],)).fetchone()[0]);updated=apply_growth(d,proof,row['symbol'],row['isin']);assert d['latest']==updated['latest']
    if apply:db.execute('update fundamentals set details=? where symbol=?',(json.dumps(updated,allow_nan=False),row['symbol']))
   entry.update(status='verified',evidence=proof);report['recovered']+=1
   if apply:report['quality']=audit(DEFAULT_DB)
  except Exception as e:entry.update(status='unresolved',reason=str(e))
  report['done']+=1;report['results'].append(entry);atomic_json(folder/'report.json',report);print(row['symbol'],entry['status'],entry.get('reason',''),flush=True)
report.update(status='complete',finished_at=now())
if apply:report['quality']=audit(DEFAULT_DB)
atomic_json(folder/'report.json',report);atomic_json(ROOT/'data/report-growth/latest.json',report);print(json.dumps({k:report[k] for k in ('total','done','recovered','run_path')}),flush=True)
