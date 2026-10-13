#!/usr/bin/env python3
"""Combine verified conflict passes against the original eligibility baseline."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,set_state
from marketdesk.intraday_data import atomic_json
first_path=Path(sys.argv[1]);first=json.loads(first_path.read_text());second=json.loads((ROOT/'data/source-conflicts/latest.json').read_text())
assert first['status']==second['status']=='complete'
previous={r['symbol']:r for r in first['results']};later={r['symbol']:r for r in second['results']};assert set(later)<=set(previous)
folder=ROOT/'data/source-conflicts'/first['started_at'].replace(':','-');baseline={r['symbol']:r for r in json.loads((folder/'before.json').read_text())};results=[];newly=[]
with connect(DEFAULT_DB) as db:
 for s,original in previous.items():
  d=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(s,)).fetchone()[0]);q=json.loads(db.execute('SELECT report FROM quality WHERE symbol=?',(s,)).fetchone()[0]);base=json.loads(baseline[s]['details']);row=dict(later.get(s,original));remaining=d.get('source_conflicts',[]);resolved=[c for c in base['source_conflicts'] if c not in remaining]
  row.update(before_conflicts=base['source_conflicts'],remaining=remaining,resolved=resolved,status='resolved' if not remaining else 'partial' if resolved else 'unresolved')
  if resolved:
   records=[r for r in d['source_conflict_resolutions'] if r['conflict'] in resolved];row['evidence']=records[-1]['evidence']
  if q['investor_ready'] and not json.loads(baseline[s]['report'])['investor_ready']:newly.append(s)
  results.append(row)
combined={**first,'results':results,'resolved_fields':sum(len(r['resolved']) for r in results),'cleared_stocks':sum(not r['remaining'] for r in results),'unresolved_stocks':sum(bool(r['remaining']) for r in results),'newly_eligible':newly,'quality':second['quality'],'investor_after':second['investor_after'],'remaining_conflict_stocks':second['remaining_conflict_stocks'],'finished_at':second['finished_at'],'updated_at':second['updated_at'],'passes':[{k:r[k] for k in ('started_at','finished_at','done','cleared_stocks','resolved_fields')} for r in (first,second)]}
assert combined['investor_after']-combined['investor_before']==len(newly)
atomic_json(folder/'combined.json',combined);atomic_json(ROOT/'data/source-conflicts/latest.json',combined)
annual_path=ROOT/'data/fundamental-repair/latest.json'
if annual_path.exists():
 annual=json.loads(annual_path.read_text())
 if annual.get('status')=='complete':
  annual['quality']=combined['quality'];annual['conflict_verification']={k:combined[k] for k in ('investor_before','investor_after','cleared_stocks','resolved_fields','remaining_conflict_stocks')};annual['updated_at']=combined['updated_at']
  with connect(DEFAULT_DB) as db:
   counts={r['symbol']:len(json.loads(r['details']).get('source_conflicts',[])) for r in db.execute('SELECT symbol,details FROM fundamentals')}
   for row in annual['results']:row['source_conflicts']=counts.get(row['symbol'],row.get('source_conflicts',0))
   set_state(db,'fundamental_repair',{k:v for k,v in annual.items() if k!='results'})
  atomic_json(annual_path,annual);atomic_json(ROOT/'data/fundamental-repair'/annual['started_at'].replace(':','-')/'report.json',annual)
print(json.dumps({k:combined[k] for k in ('investor_before','investor_after','cleared_stocks','resolved_fields','remaining_conflict_stocks')}))
