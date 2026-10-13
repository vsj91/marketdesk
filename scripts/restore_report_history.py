#!/usr/bin/env python3
"""Restore saved, hash-verified provenance for supplements from older repairs.

Does not change financial values, their observation times, status or paper tables.
Dry run unless --apply is supplied. Uses only existing local report caches.
"""
import copy,hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now

def values(report):
    evidence=report.get('evidence') or {}
    return evidence.get('values',{'total_lease_liabilities':evidence.get('value')})

def restore(apply=False):
    changes=[];unresolved=[]
    with connect(DEFAULT_DB) as db:
        rows=list(db.execute('SELECT i.symbol,i.isin,f.details FROM instruments i JOIN fundamentals f USING(symbol)'))
        for row in rows:
            d=json.loads(row['details']);latest=d.get('latest') or {};history=copy.deepcopy(d.get('annual_report_history',[]));reports=history+[d.get('annual_report_check') or {}]
            for field,src in (latest.get('sources') or {}).items():
                if src.get('source')!='company_annual_report':continue
                def matches(a):
                    return a.get('status')=='verified' and a.get('evidence_path')==src.get('evidence_path') and a.get('observed_at')==src.get('observed_at') and field in values(a)
                if any(matches(a) for a in reports):continue
                found=None
                for file in (ROOT/'data/annual-reports'/row['symbol']).glob('*.json'):
                    a=json.loads(file.read_text());e=a.get('evidence') or {}
                    if not matches(a) or (a.get('symbol'),a.get('isin'))!=(row['symbol'],row['isin']):continue
                    if any(e.get(k)!=latest.get(k) for k in ('period','statement_type','units_in')):continue
                    v=values(a).get(field)
                    if not isinstance(v,(int,float)) or not math.isclose(v,latest[field],abs_tol=1e-8):continue
                    path=ROOT/a['evidence_path']
                    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=a.get('excerpt_sha256'):continue
                    if len(set(e.get('matched_fields',[])))<2:continue
                    found=a;break
                if found:
                    history.append(found);reports.append(found)
                else:unresolved.append({'symbol':row['symbol'],'field':field})
            if history!=d.get('annual_report_history',[]):
                changes.append({'symbol':row['symbol'],'before':row['details'],'history':history})
        if apply:
            folder=ROOT/'data/fundamental-repair/provenance-restorations';folder.mkdir(parents=True,exist_ok=True)
            (folder/(now().replace(':','-')+'.json')).write_text(json.dumps(changes))
            for change in changes:
                # Read again in the write transaction; preserve any repair update.
                current=json.loads(db.execute('SELECT details FROM fundamentals WHERE symbol=?',(change['symbol'],)).fetchone()[0]);existing=current.setdefault('annual_report_history',[])
                for a in change['history']:
                    if not any((r.get('excerpt_sha256'),r.get('observed_at'))==(a.get('excerpt_sha256'),a.get('observed_at')) for r in existing):existing.append(a)
                db.execute('UPDATE fundamentals SET details=? WHERE symbol=?',(json.dumps(current,allow_nan=False),change['symbol']))
    return {'applied':apply,'stocks_with_restored_provenance':len(changes),'unresolved':unresolved}

if __name__=='__main__':print(json.dumps(restore('--apply' in sys.argv),indent=2))
