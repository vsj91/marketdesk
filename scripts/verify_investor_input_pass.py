#!/usr/bin/env python3
"""Verify recovered report fields, growth, and retention through the raw import path."""
import sys,json,copy,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,connect,DEFAULT_DB,now
from marketdesk.provider import extract_fundamental_details
from marketdesk.importer import summary_metrics
from marketdesk.fallbacks import apply_fallback
from marketdesk.annual_reports import retain_report,pdf_worker,parse_balance
from marketdesk.growth_repair import retain_growth
from marketdesk.conflict_repair import retain_resolutions
from marketdesk.annual_conflicts import retain_annual_resolutions,report_text
from marketdesk.report_growth import retain_company_growth,apply_growth
from marketdesk.intraday_data import atomic_json
published=json.loads((ROOT/'data/fundamental-repair/latest.json').read_text())
inputs=json.loads((ROOT/published['run_path']/'report.json').read_text());growth=json.loads((ROOT/'data/report-growth/latest.json').read_text());assert inputs['status']==growth['status']=='complete'
base={r['symbol']:r for r in json.loads((ROOT/inputs['run_path']/'before.json').read_text())};base.update({r['symbol']:r for r in json.loads((ROOT/growth['run_path']/'before.json').read_text())})
growth_symbols={r['symbol'] for r in growth['results'] if r['status']=='verified'}
selected=list(dict.fromkeys(inputs.get('newly_eligible',[])+sorted(growth_symbols)));results=[]
with connect(DEFAULT_DB) as db:
 for s in selected:
  r=db.execute('select i.isin,f.details,q.report from instruments i join fundamentals f using(symbol) join quality q using(symbol) where symbol=?',(s,)).fetchone();old=json.loads(r['details']);b=json.loads(base[s]['details']);q=json.loads(r['report']);assert q['investor_ready'],s
  if s in growth_symbols:
   proof=old['company_growth_repair'];reparsed=apply_growth(b,proof,s,r['isin']);assert reparsed['summary']==old['summary'] and old['latest']==b['latest'],s
  else:
   proof=old['annual_report_check'];text=report_text(proof,s,r['isin']);parsed=parse_balance(text,b['latest'],True)
   expected=proof['evidence']['values'];assert all(abs(parsed['values'][k]-v)<1e-8 for k,v in expected.items() if k!='total_lease_liabilities' or not proof['evidence'].get('lease_note_evidence')),s
   # For a separate carrying-value note, the saved worker reparses the note too.
   if proof['evidence'].get('lease_note_evidence'):
    from marketdesk.annual_reports import balance_text,parse_lease_note
    import pymupdf
    doc=pymupdf.open(ROOT/proof['evidence_path']);note=parse_lease_note(balance_text(doc[-1]),b['latest']);assert abs(note['value']-expected['total_lease_liabilities'])<1e-8,s
   assert old.get('summary')==b.get('summary'),s
  payloads={p['endpoint']:json.loads(p['payload']) for p in db.execute('SELECT endpoint,payload FROM payloads WHERE symbol=? AND http_status=200 AND (error IS NULL OR error="")',(s,))}
  d=extract_fundamental_details(payloads);d['summary']=summary_metrics(payloads);d=apply_fallback(d,json.loads((ROOT/'data/fallbacks'/(s+'.json')).read_text()))
  for f in (retain_resolutions,retain_report,retain_growth,retain_resolutions,retain_annual_resolutions,retain_company_growth):d=f(d,old,s,r['isin'])
  assert not d.get('source_conflicts') and not d.get('annual_conflict_recheck_required'),s
  assert all(d['latest'].get(k)==old['latest'].get(k) for k in ['profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents']),s
  if old.get('company_growth_repair'):assert d['summary']==old['summary'],s
  results.append({'symbol':s,'evidence_reparsed':True,'raw_reimport_retained':True,'investor_ready':True})
result={'status':'passed','checked_at':now(),'stocks_verified':len(results),'results':results};atomic_json(ROOT/inputs['run_path']/'verification.json',result);print(json.dumps(result,indent=2))
