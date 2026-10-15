import sys,json,copy
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
from marketdesk.storage import ROOT,connect,DEFAULT_DB
from marketdesk.provider import extract_fundamental_details
from marketdesk.importer import summary_metrics
from marketdesk.fallbacks import apply_fallback
from marketdesk.annual_reports import retain_report
from marketdesk.growth_repair import retain_growth
from marketdesk.conflict_repair import retain_resolutions
from marketdesk.annual_conflicts import retain_annual_resolutions
from marketdesk.report_growth import retain_company_growth
from marketdesk.intraday_data import atomic_json
before={r['symbol']:json.loads(r['details']) for r in json.loads((ROOT/'tmp/remaining-reviews/before-all.json').read_text())}
keys=['profit_before_tax','finance_costs','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents','equity']
syms=set()
with connect(DEFAULT_DB) as db:
 for r in db.execute('SELECT symbol,details FROM fundamentals'):
  d=json.loads(r['details']);b=before[r['symbol']]
  if any(d.get('latest',{}).get(k)!=b.get('latest',{}).get(k) for k in keys) or any(d.get('summary',{}).get(k)!=b.get('summary',{}).get(k) for k in ('revenue_growth_yoy','net_profit_growth_yoy')):syms.add(r['symbol'])
result=[]
with connect(DEFAULT_DB) as db:
 for s in sorted(syms):
  r=db.execute('select i.isin,f.details from instruments i join fundamentals f using(symbol) where symbol=?',(s,)).fetchone();isin=r['isin'];after=json.loads(r['details'])
  payloads={p['endpoint']:json.loads(p['payload']) for p in db.execute('SELECT endpoint,payload FROM payloads WHERE symbol=? AND http_status=200 AND (error IS NULL OR error="")',(s,))}
  raw=extract_fundamental_details(payloads);raw['summary']=summary_metrics(payloads);raw=apply_fallback(raw,json.loads((ROOT/'data/fallbacks'/(s+'.json')).read_text()))
  for retain in (retain_resolutions,retain_report,retain_growth,retain_resolutions,retain_annual_resolutions,retain_company_growth):raw=retain(raw,after,s,isin)
  keys=['profit_before_tax','finance_costs','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents','equity','period','statement_type','units_in']
  changed=[k for k in keys if raw.get('latest',{}).get(k)!=after['latest'].get(k)]
  growthchanged=[k for k in ('revenue_growth_yoy','net_profit_growth_yoy') if raw.get('summary',{}).get(k)!=after.get('summary',{}).get(k)]
  result.append({'symbol':s,'changed_annual':changed,'changed_growth':growthchanged,'status':'passed' if not changed and not growthchanged else 'failed'})
print(json.dumps({'checked':len(result),'failures':[r for r in result if r['status']=='failed']},indent=2));atomic_json(ROOT/'tmp/remaining-reviews/retention.json',result)
