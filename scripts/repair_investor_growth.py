#!/usr/bin/env python3
"""Repair only stocks whose sole Investor blocker is missing annual growth."""
import json,sys,copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,set_state
from marketdesk.growth_repair import recover,checked_pair
from marketdesk.quality import audit,financial_reasons,source_reasons,IST
from marketdesk.intraday_data import atomic_json
from datetime import datetime,timezone

apply='--apply' in sys.argv
with connect(DEFAULT_DB) as db:
 quality=get_state(db,'quality_summary',{})
 rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol)')]
rows=[r for r in rows if json.loads(r['report'])['financial_reasons']==['Comparable annual growth inputs missing'] and not json.loads(r['report'])['price_reasons']]
if '--sample' in sys.argv:rows=rows[:10]
started=now();folder=ROOT/'data/investor-growth'/started.replace(':','-');atomic_json(folder/'before.json',rows)
report={'status':'running','started_at':started,'total':len(rows),'done':0,'recovered':0,'investor_before':quality.get('investor_ready'),'applied':apply,'results':[]}
def fetch(r):return recover(r['symbol'],r['isin'],json.loads(r['details']))
with ThreadPoolExecutor(max_workers=6) as pool:
 futures={pool.submit(fetch,r):r for r in rows}
 for future in as_completed(futures):
  row=futures[future];entry={'symbol':row['symbol']}
  try:
   evidence=future.result()
   with connect(DEFAULT_DB) as db:
    raw=db.execute('SELECT details,observed_at FROM fundamentals WHERE symbol=?',(row['symbol'],)).fetchone();d=json.loads(raw['details']);before=copy.deepcopy(d)
    d['summary']=checked_pair(d,evidence['current'],evidence['prior']);d['growth_repair']=evidence
    times={r['endpoint']:r['observed_at'] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(row['symbol'],))}
    reasons=financial_reasons(d,raw['observed_at'],datetime.now(timezone.utc))+source_reasons(d,times,datetime.now(timezone.utc))
    if reasons:raise ValueError('; '.join(reasons))
    assert before['latest']==d['latest'],'Annual values changed'
    if apply:db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(d,allow_nan=False),row['symbol']))
   entry.update(status='verified',current=evidence['current'],prior=evidence['prior']);report['recovered']+=1
  except Exception as error:entry.update(status='unresolved',reason=str(error) if isinstance(error,ValueError) else type(error).__name__)
  report['results'].append(entry);report['done']+=1
  if report['done']%20==0:atomic_json(folder/'report.json',report);print(json.dumps({k:report[k] for k in ('done','total','recovered')}),flush=True)
if apply:
 report['quality']=audit(DEFAULT_DB);report['investor_after']=report['quality']['investor_ready']
report.update(status='complete',finished_at=now());atomic_json(folder/'report.json',report);atomic_json(ROOT/'data/investor-growth/latest.json',report)
if apply:
 annual_path=ROOT/'data/fundamental-repair/latest.json'
 if annual_path.exists():
  annual=json.loads(annual_path.read_text())
  if annual.get('status')=='complete':
   annual.setdefault('annual_pass_quality',annual.get('quality'))
   annual['quality']=report['quality'];annual['investor_growth']={k:v for k,v in report.items() if k not in ('results','quality')};annual['updated_at']=report['finished_at']
   atomic_json(annual_path,annual);atomic_json(ROOT/'data/fundamental-repair'/annual['started_at'].replace(':','-')/'report.json',annual)
   with connect(DEFAULT_DB) as db:set_state(db,'fundamental_repair',{k:v for k,v in annual.items() if k!='results'})
print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))
