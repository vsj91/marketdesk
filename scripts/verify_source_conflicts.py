#!/usr/bin/env python3
"""Audit every resolved discrepancy against raw NSE evidence and original inputs."""
import copy,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect
from marketdesk.conflict_repair import retain_resolutions,validate_context,same
from marketdesk.fallbacks import nse_annual
p=ROOT/'data/source-conflicts/latest.json';report=json.loads(p.read_text());assert report['status']=='complete'
folder=ROOT/'data/source-conflicts'/report['started_at'].replace(':','-');baseline=json.loads((folder/'before.json').read_text());before={r['symbol']:r for r in baseline}
assert len(report['results'])==len({r['symbol'] for r in report['results']})==len(before)
fields=cleared=0;eligible=[];parsed={}
with connect(DEFAULT_DB) as db:
 for r in report['results']:
  s=r['symbol'];b=json.loads(before[s]['details']);row=db.execute('SELECT i.isin,f.details,q.report FROM instruments i JOIN fundamentals f USING(symbol) JOIN quality q USING(symbol) WHERE symbol=?',(s,)).fetchone();d=json.loads(row['details']);q=json.loads(row['report'])
  assert d['latest']==b['latest'] and d.get('summary')==b.get('summary'),s
  assert r['before_conflicts']==b['source_conflicts'] and r['remaining']==d.get('source_conflicts',[]),s
  resolved=[c for c in b['source_conflicts'] if c not in d.get('source_conflicts',[])]
  assert resolved==r.get('resolved',[]),s
  fields+=len(resolved);cleared+=not d.get('source_conflicts')
  for c in resolved:
   records=[x for x in d.get('source_conflict_resolutions',[]) if x['conflict']==c];assert records,s
   record=records[-1];e=record['evidence'];assert (e['symbol'],e['isin'])==(s,row['isin']);key=(s,e['raw_sha256'])
   if key not in parsed:
    raw=(ROOT/e['evidence_path']).read_bytes();assert hashlib.sha256(raw).hexdigest()==e['raw_sha256'];parsed[key]=nse_annual(raw.decode('utf-8'),s,row['isin'],e['source_url'])
   validate_context(d,parsed[key]);assert same(parsed[key].get(c['field']),d['latest'][c['field']])
  simulated=retain_resolutions(copy.deepcopy(b),d,s,row['isin']);assert simulated.get('source_conflicts',[])==d.get('source_conflicts',[]),s
  if q['investor_ready'] and not json.loads(before[s]['report'])['investor_ready']:eligible.append(s)
 assert fields==report['resolved_fields'] and cleared==report['cleared_stocks']
 assert set(eligible)==set(report['newly_eligible'])
 assert report['investor_after']-report['investor_before']==len(eligible)
result={'status':'passed','stocks_checked':len(before),'resolved_fields_verified':fields,'verified_filing_files':len(parsed),'newly_eligible_verified':len(eligible),'financial_values_and_growth_unchanged':True,'reimport_retention_verified':True}
(folder/'verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
