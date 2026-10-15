#!/usr/bin/env python3
"""Verify all existing financial conflicts; preserve values and paper trading rules."""
import json,sys,copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.working_universe import symbol_filter
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,set_state
from marketdesk.conflict_repair import verify_nse,reconcile
from marketdesk.quality import audit
from marketdesk.intraday_data import atomic_json
apply='--apply' in sys.argv
with connect(DEFAULT_DB) as db:
 quality=get_state(db,'quality_summary',{})
 rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol) WHERE '+symbol_filter(db,'i.symbol'))]
rows=[r for r in rows if json.loads(r['details']).get('source_conflicts')]
rows.sort(key=lambda r:(not (json.loads(r['report'])['financial_reasons']==['Financial sources disagree'] and not json.loads(r['report'])['price_reasons']),r['symbol']))
if '--sample' in sys.argv:rows=rows[:10]
started=now();folder=ROOT/'data/source-conflicts'/started.replace(':','-');atomic_json(folder/'before.json',rows)
report={'status':'running','started_at':started,'total':len(rows),'done':0,'resolved_fields':0,'cleared_stocks':0,'unresolved_stocks':0,'investor_before':quality.get('investor_ready'),'quality_before':quality,'applied':apply,'mode':'local_paper_only','results':[]}
def publish():
 report['updated_at']=now();atomic_json(folder/'report.json',report)
 if apply:atomic_json(ROOT/'data/source-conflicts/latest.json',report)
def fetch(row):
 d=json.loads(row['details'])
 if not any('upstox' in c and 'nse' in c for c in d['source_conflicts']):raise ValueError('Primary annual reports disagree; retained for manual review')
 return verify_nse(row['symbol'],row['isin'],d)
publish()
with ThreadPoolExecutor(max_workers=6) as pool:
 futures={pool.submit(fetch,r):r for r in rows}
 for future in as_completed(futures):
  row=futures[future];entry={'symbol':row['symbol'],'before_conflicts':json.loads(row['details'])['source_conflicts']}
  try:
   evidence=future.result()
   with connect(DEFAULT_DB) as db:
    current=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(row['symbol'],)).fetchone()[0]);updated,resolved=reconcile(current,evidence)
    assert updated['latest']==current['latest'] and updated.get('summary')==current.get('summary')
    if apply and resolved:db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(updated,allow_nan=False),row['symbol']))
   clear=not updated['source_conflicts'];report['resolved_fields']+=len(resolved);report['cleared_stocks']+=int(clear)
   entry.update(status='resolved' if clear else 'partial' if resolved else 'unresolved',resolved=[r['conflict'] for r in resolved],remaining=updated['source_conflicts'],evidence=evidence,reason='Audited NSE filing verified; official values retained' if resolved else 'Conflict is not verified by this NSE filing')
  except Exception as error:entry.update(status='unresolved',remaining=entry['before_conflicts'],reason=str(error) if isinstance(error,ValueError) else type(error).__name__)
  report['unresolved_stocks']+=entry['status']!='resolved';report['results'].append(entry);report['done']+=1
  if report['done']%20==0:publish();print(json.dumps({k:report[k] for k in ('done','total','cleared_stocks','resolved_fields')}),flush=True)
if apply:
 report['quality']=audit(DEFAULT_DB);report['investor_after']=report['quality']['investor_ready']
 with connect(DEFAULT_DB) as db:
  ready={r['symbol'] for r in db.execute('SELECT symbol,report FROM quality WHERE '+symbol_filter(db)) if json.loads(r['report'])['investor_ready']}
 report['newly_eligible']=[r['symbol'] for r in rows if not json.loads(r['report'])['investor_ready'] and r['symbol'] in ready]
 report['remaining_conflict_stocks']=report['quality']['reasons'].get('Financial sources disagree',0)
report.update(status='complete',finished_at=now());publish()
print(json.dumps({k:v for k,v in report.items() if k not in ('results','quality_before','quality','newly_eligible')},indent=2))
