#!/usr/bin/env python3
"""Publish the input and company-growth runs against their original baseline."""
import sys,json,copy,collections
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect,now,get_state,set_state
from marketdesk.intraday_data import atomic_json
inputs=json.loads((ROOT/'data/fundamental-repair/latest.json').read_text());growth=json.loads((ROOT/'data/report-growth/latest.json').read_text())
assert inputs['status']==growth['status']=='complete'
folder=ROOT/inputs['run_path'];original=json.loads((folder/'report.json').read_text());combined=copy.deepcopy(original)
base={r['symbol']:r for r in json.loads((folder/'before.json').read_text())};base.update({r['symbol']:r for r in json.loads((ROOT/growth['run_path']/'before.json').read_text())})
with connect(DEFAULT_DB) as db:
 quality=get_state(db,'quality_summary',{});records={r['symbol']:(json.loads(r['details']),json.loads(r['report'])) for r in db.execute('SELECT symbol,details,report FROM fundamentals JOIN quality USING(symbol)')}
 for r in growth['results']:
  d,q=records[r['symbol']];proof=r.get('evidence');okay=r['status']=='verified'
  combined['results'].append({'symbol':r['symbol'],'status':'complete' if okay else 'unresolved','before_missing':['comparable_annual_growth'],'after_missing':[] if okay else ['comparable_annual_growth'],'recovered':['revenue_growth_yoy','net_profit_growth_yoy'] if okay else [],'source':d.get('selected_source'),'period':d['latest'].get('period'),'basis':d['latest'].get('statement_type'),'source_url':d['latest'].get('source_url'),'nse_status':'report_checked','yahoo_status':'not_used','annual_report':proof if okay else {'status':'unresolved','reason':r.get('reason')},'source_conflicts':len(d.get('source_conflicts',[]))})
 combined.update(total=len(combined['results']),done=len(combined['results']),quality=quality,updated_at=now(),finished_at=growth['finished_at'],annual_reports_verified=original['annual_reports_verified']+growth['recovered'],recovered_fields=original['recovered_fields']+2*growth['recovered'],improved_stocks=original['improved_stocks']+growth['recovered'])
 combined['report_provider_counts']['NSE']=combined['report_provider_counts'].get('NSE',0)+growth['recovered']
 combined['newly_eligible']=[s for s,r in base.items() if not json.loads(r['report'])['investor_ready'] and records[s][1]['investor_ready']]
 combined['blocker_breakdown']={'ineligible':sum(not q['investor_ready'] for _,q in records.values()),'financial_only':sum(not q['price_reasons'] and not q['investor_ready'] for _,q in records.values()),'price_blocked':sum(bool(q['price_reasons']) for _,q in records.values())}
 combined['source_issue_stocks']=sum(r['status']=='unresolved' or bool(r.get('source_conflicts')) for r in combined['results'])
 combined['passes']=[{'type':'annual_inputs','total':original['total'],'recovered_stocks':original['improved_stocks'],'path':original['run_path']},{'type':'company_report_growth','total':growth['total'],'recovered_stocks':growth['recovered'],'path':growth['run_path']}]
 combined['finished_at']=max(original['finished_at'],growth['finished_at'])
 combined['note']=f"{combined['total']} targeted report checks: {original['total']} annual-input blockers and {growth['total']} company growth-report checks. Newly Investor eligible is measured against the {original['quality_before']['investor_ready']}-stock baseline. Annual completeness and strategy signals remain separate. Both portfolios are paper only."
 # Export every remaining blocker, including overlapping financial and price reasons.
 remaining=[{'symbol':s,'price_reasons':q['price_reasons'],'financial_reasons':q['financial_reasons'],'missing_fields':d.get('missing_fields',[]),'annual_report_reason':(d.get('annual_report_check') or {}).get('reason'),'period':d.get('latest',{}).get('period')} for s,(d,q) in records.items() if not q['investor_ready']]
 atomic_json(folder/'remaining-blockers.json',{'checked_at':quality['checked_at'],'counts':combined['blocker_breakdown'],'stocks':remaining})
 set_state(db,'fundamental_repair',{k:v for k,v in combined.items() if k!='results'})
assert len(combined['newly_eligible'])==combined['quality']['investor_ready']-combined['quality_before']['investor_ready']
atomic_json(folder/'combined.json',combined);atomic_json(ROOT/'data/fundamental-repair/latest.json',combined)
print(json.dumps({'investor_before':combined['quality_before']['investor_ready'],'investor_after':quality['investor_ready'],'gained':len(combined['newly_eligible']),'blockers':combined['blocker_breakdown'],'quality_reasons':quality['reasons']},indent=2))
