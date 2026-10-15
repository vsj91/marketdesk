#!/usr/bin/env python3
"""Apply annual-report corrections with before/after evidence and an eligibility audit."""
import json,sys,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,set_state
from marketdesk.annual_conflicts import reconcile_debt,reconcile_reviewed,retain_annual_resolutions
from marketdesk.liability_reviews import reconcile_liability_review
from marketdesk.quality import audit
from marketdesk.intraday_data import atomic_json
apply='--apply' in sys.argv
with connect(DEFAULT_DB) as db:
 quality=get_state(db,'quality_summary',{});rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol)') if json.loads(r['details']).get('source_conflicts')]
started=now();folder=ROOT/'data/annual-conflicts'/started.replace(':','-');atomic_json(folder/'before.json',rows)
report={'status':'running','method':'company_annual_reports','started_at':started,'total':len(rows),'done':0,'resolved_fields':0,'cleared_stocks':0,'unresolved_stocks':0,'investor_before':quality.get('investor_ready'),'quality_before':quality,'applied':apply,'mode':'local_paper_only','results':[],'run_path':str(folder.relative_to(ROOT))}
def publish():
 report['updated_at']=now();atomic_json(folder/'report.json',report)
 if apply:atomic_json(ROOT/'data/source-conflicts/latest.json',report)
publish()
for row in rows:
 symbol=row['symbol'];entry={'symbol':symbol};before=json.loads(row['details']);entry['before_conflicts']=before['source_conflicts']
 try:
  review=ROOT/'data/annual-reports'/symbol/'conflict-review.json'
  liability=ROOT/'data/annual-reports'/symbol/'liability-review.json'
  if liability.exists():proof=json.loads(liability.read_text());resolver=reconcile_liability_review
  elif review.exists():proof=json.loads(review.read_text());resolver=reconcile_reviewed
  else:
   proof=copy.deepcopy(before['annual_report_check']);resolver=reconcile_debt
   if symbol in ('JUBLINGREA','ZUARI'):
    proof['reviewed_nse_rounding']={'quantum_crore':.01,'reason':'Reviewed NSE components are rounded to whole lakhs despite .00 padding; company report gives finer precision. Both components and the total reconcile within 0.005 crore per rounded component.'}
  with connect(DEFAULT_DB) as db:
   current=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()[0])
   if current!=before:raise ValueError('Concurrent financial update; retry against fresh snapshot')
   updated,record=resolver(current,proof,symbol,row['isin'])
   retained=retain_annual_resolutions(current,updated,symbol,row['isin'])
   assert retained['latest']==updated['latest'] and retained['source_conflicts']==updated['source_conflicts'],'Retention check failed'
   assert updated.get('summary')==current.get('summary'),'Growth values must remain unchanged'
   if apply:db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(updated,allow_nan=False),symbol))
  entry.update(status='resolved' if not updated['source_conflicts'] else 'partial',resolved=[{**c,'corrected_value':record['changes'][c['field']]} for c in record['conflicts']],remaining=updated['source_conflicts'],reason=record['reason'],evidence={'source_url':proof['source_url'],'observed_at':proof['observed_at'],'evidence_path':proof['evidence_path'],'excerpt_sha256':proof['excerpt_sha256'],'pages':proof['evidence']['pages']},changes=record['changes'])
  report['resolved_fields']+=len(record['conflicts']);report['cleared_stocks']+=not updated['source_conflicts']
 except Exception as e:entry.update(status='unresolved',resolved=[],remaining=before['source_conflicts'],reason=str(e))
 report['results'].append(entry);report['done']+=1;report['unresolved_stocks']+=entry['status']!='resolved';publish();print(symbol,entry['status'],entry.get('changes',entry['reason']),flush=True)
if apply:
 report['quality']=audit(DEFAULT_DB);report['investor_after']=report['quality']['investor_ready'];report['remaining_conflict_stocks']=report['quality']['reasons'].get('Financial sources disagree',0)
 with connect(DEFAULT_DB) as db:
  ready={r['symbol'] for r in db.execute('SELECT symbol,report FROM quality') if json.loads(r['report'])['investor_ready']}
 report['newly_eligible']=[r['symbol'] for r in rows if not json.loads(r['report'])['investor_ready'] and r['symbol'] in ready]
report.update(status='complete',finished_at=now());publish()
if apply:
 p=ROOT/'data/fundamental-repair/latest.json'
 if p.exists():
  annual=json.loads(p.read_text());annual['quality']=report['quality'];annual['conflict_verification']={k:report[k] for k in ('investor_before','investor_after','cleared_stocks','resolved_fields','remaining_conflict_stocks')};annual['updated_at']=now()
  with connect(DEFAULT_DB) as db:
   counts={r['symbol']:len(json.loads(r['details']).get('source_conflicts',[])) for r in db.execute('SELECT symbol,details FROM fundamentals')}
   for r in annual.get('results',[]):r['source_conflicts']=counts.get(r['symbol'],r.get('source_conflicts',0))
   set_state(db,'fundamental_repair',{k:v for k,v in annual.items() if k!='results'})
  atomic_json(p,annual)
print(json.dumps({k:v for k,v in report.items() if k not in ('results','quality','quality_before','newly_eligible')},indent=2))
