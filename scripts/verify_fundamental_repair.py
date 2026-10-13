#!/usr/bin/env python3
"""Verify a completed repair against its saved baseline and current local data."""
import hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect
from marketdesk.fundamental_repair import missing


def verify(report_path):
    report=json.loads(Path(report_path).read_text())
    assert report['status']=='complete' and report['done']==report['total'],'Repair is not complete'
    folder=ROOT/'data/fundamental-repair'/report['started_at'].replace(':','-')
    before={r['symbol']:json.loads(r['details'] or '{}') for r in json.loads((folder/'before.json').read_text())}
    rows=report['results'];assert len(rows)==len({r['symbol'] for r in rows})==len(before)
    assert set(before)=={r['symbol'] for r in rows}
    fields=improved=completed=0;files=set();unchanged=0
    with connect(DEFAULT_DB) as db:
        current={r['symbol']:json.loads(r['details']) for r in db.execute('SELECT symbol,details FROM fundamentals')}
        complete_now=sum(d.get('status')=='COMPLETE' for d in current.values())
        for r in rows:
            old=before[r['symbol']];new=current[r['symbol']];latest=new.get('latest') or {}
            assert sorted(r['before_missing'])==sorted(missing(old)),r['symbol']
            assert sorted(r['after_missing'])==sorted(missing(new)),r['symbol']
            gained=set(missing(old))-set(missing(new))
            assert gained==set(r.get('recovered',[])),r['symbol']
            fields+=len(gained);improved+=bool(gained);completed+=new.get('status')=='COMPLETE'
            for key,value in (old.get('latest') or {}).items():
                if isinstance(value,(int,float)) and key in ('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents'):
                    assert math.isclose(latest[key],value,abs_tol=1e-8),f'Existing {r["symbol"]} {key} changed'
                    unchanged+=1
            if not gained:continue
            ar=r.get('annual_report') or {};e=ar.get('evidence') or {}
            assert ar.get('status')=='verified',r['symbol']
            path=ROOT/ar['evidence_path'];assert hashlib.sha256(path.read_bytes()).hexdigest()==ar['excerpt_sha256'],str(path)
            files.add(str(path));assert len(set(e['matched_fields']))>=2
            for key in ('period','statement_type','units_in'):assert e[key]==latest[key]
            for key in gained:
                src=latest['sources'][key];value=latest[key]
                assert value>=0 and math.isfinite(value)
                assert math.isclose(value,e['values'][key],abs_tol=1e-8)
                assert math.isclose(value,src['value'],abs_tol=1e-8)
                assert src['source_url']==ar['source_url'] and src['observed_at']==ar['observed_at']
                assert src['raw_sha256']==ar['raw_sha256'] and src['evidence_path']==ar['evidence_path']
                if key=='finance_costs':assert math.isclose(e['income_evidence']['matched_profit_before_tax'],latest['profit_before_tax'],abs_tol=.015)
                if key=='total_lease_liabilities' and e.get('lease_note_evidence'):
                    assert math.isclose(value,e['lease_note_evidence']['value'],abs_tol=1e-8)
        assert (fields,improved,completed)==(report['recovered_fields'],report['improved_stocks'],report['newly_complete'])
        assert complete_now==report['complete_now']==report['complete_before']+completed
    result={'status':'passed','stocks_checked':len(rows),'recovered_fields_verified':fields,'evidence_files_verified':len(files),'existing_financial_values_unchanged':unchanged,'complete_now':complete_now,'incomplete_now':report['incomplete_now']}
    (folder/'verification.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':print(json.dumps(verify(sys.argv[1] if len(sys.argv)>1 else ROOT/'data/fundamental-repair/latest.json'),indent=2))
