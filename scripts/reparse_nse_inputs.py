#!/usr/bin/env python3
"""Fill previously unrecognized NSE lease captions from hash-verified annual HTML.

Preserves selected values, financial context and original observation time. Saves
both the before image and the matching fallback cache used by subsequent imports.
"""
import sys,json,copy,hashlib
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now
from marketdesk.fallbacks import nse_annual
from marketdesk.provider import FUNDAMENTAL_REQUIRED_FIELDS
from marketdesk.fundamental_repair import missing
from marketdesk.intraday_data import atomic_json
from marketdesk.quality import audit
apply='--apply' in sys.argv
folder=ROOT/'data/nse-caption-repair'/now().replace(':','-')
with connect(DEFAULT_DB) as db:rows=[dict(r) for r in db.execute('SELECT i.symbol,i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol)')]
report={'status':'running','mode':'local_paper_only','started_at':now(),'run_path':str(folder.relative_to(ROOT)),'results':[],'applied':apply}
baseline=[]
for r in rows:
 d=json.loads(r['details']);l=d.get('latest') or {};s=r['symbol']
 if d.get('selected_source')!='nse_audited_filing' or l.get('total_lease_liabilities') is not None or not all(l.get(k) for k in ('raw_sha256','source_url','observed_at')):continue
 try:
  raw=(ROOT/'data/filings'/s/(l['raw_sha256']+'.html')).read_bytes()
  if hashlib.sha256(raw).hexdigest()!=l['raw_sha256']:raise ValueError('Raw filing hash changed')
  if not 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(l['observed_at'])).total_seconds()<=30*86400:raise ValueError('Evidence expired')
  a=nse_annual(raw.decode(),s,r['isin'],l['source_url'])
  if a.get('total_lease_liabilities') is None:continue
  if any(a.get(k)!=l.get(k) for k in ('period','statement_type','units_in')):raise ValueError('Selected context differs')
  for key in ('current_lease_liabilities','non_current_lease_liabilities'):
   if l.get(key) is not None and l[key]!=a.get(key):raise ValueError('Known lease component differs')
  changes={k:a[k] for k in ('current_lease_liabilities','non_current_lease_liabilities','total_lease_liabilities') if l.get(k) is None and a.get(k) is not None}
  cache_path=ROOT/'data/fallbacks'/(s+'.json');cache=json.loads(cache_path.read_text());old_cache=copy.deepcopy(cache)
  if (cache.get('symbol'),cache.get('isin'))!=(s,r['isin']):raise ValueError('Fallback identity differs')
  candidates=[x for x in cache.get('nse',{}).get('annual',[]) if all(x.get(k)==l.get(k) for k in ('raw_sha256','source_url','period','statement_type','units_in'))]
  if len(candidates)!=1:raise ValueError('Unique matching cached annual unavailable')
  updated=copy.deepcopy(d)
  for target in (updated['latest'],candidates[0]):
   for k,v in changes.items():
    if target.get(k) is not None and target[k]!=v:raise ValueError('Cached lease input differs')
    target[k]=v;target.setdefault('sources',{})[k]=copy.deepcopy(a['sources'][k])
   target['missing_fields']=[k for k in FUNDAMENTAL_REQUIRED_FIELDS if target.get(k) is None]
  updated['missing_fields']=missing(updated);updated['status']='PARTIAL' if updated['missing_fields'] else 'COMPLETE'
  # Preserve summary and all already known selected annual values.
  assert updated.get('summary')==d.get('summary')
  assert all(updated['latest'].get(k)==v for k,v in l.items() if k not in changes and k not in ('sources','missing_fields'))
  baseline.append(r);atomic_json(folder/'before.json',baseline);atomic_json(folder/'cache-before'/(s+'.json'),old_cache)
  if apply:
   with connect(DEFAULT_DB) as db:
    current=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(s,)).fetchone()[0]
    if json.loads(current)!=d:raise ValueError('Concurrent update; retry required')
    atomic_json(cache_path,cache)
    db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(updated,allow_nan=False),s))
  entry={'symbol':s,'status':'complete' if not missing(updated) else 'improved','before_missing':missing(d),'after_missing':missing(updated),'recovered':sorted(set(missing(d))-set(missing(updated))),'changes':changes,'source':'nse_audited_filing','source_url':l['source_url'],'period':l['period'],'basis':l['statement_type'],'raw_sha256':l['raw_sha256'],'observed_at':l['observed_at'],'nse_status':'verified','yahoo_status':'not_used'}
 except (ValueError,KeyError,OSError) as e:entry={'symbol':s,'status':'unresolved','reason':str(e)}
 report['results'].append(entry)
report.update(status='complete',finished_at=now(),done=len(report['results']),total=len(report['results']))
if apply:report['quality']=audit(DEFAULT_DB)
atomic_json(folder/'report.json',report);atomic_json(ROOT/'data/nse-caption-repair/latest.json',report)
print(json.dumps(report,indent=2))
