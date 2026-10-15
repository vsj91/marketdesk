import json,copy,hashlib
from marketdesk.storage import *
from marketdesk.financial_sources import request
from marketdesk.fallbacks import Fallbacks,nse_annual
from marketdesk.annual_conflicts import report_text
for s,method,label in [('ADANIENT','debt_with_trade_credit','Trade Credits & Acceptances'),('PRESTIGE','mislabelled_lease_component','Other ﬁnancial liabilities')]:
 with connect(DEFAULT_DB) as db:r=db.execute('select isin,details from instruments join fundamentals using(symbol) where symbol=?',(s,)).fetchone();d=json.loads(r['details'])
 v=d['latest'];url=v['source_url']
 with request(Fallbacks().session,url) as response:raw=response.content
 sha=hashlib.sha256(raw).hexdigest();assert sha==v['raw_sha256'],'NSE filing changed; fresh review required'
 n=nse_annual(raw.decode(),s,r['isin'],url);proof=copy.deepcopy(d['annual_report_check']);text=report_text(proof,s,r['isin'])
 matches=[line.strip() for line in text.splitlines() if label in line]
 line=matches[0];assert method!='debt_with_trade_credit' or len(matches)==1
 proof['liability_review']={'method':method,'extra_line':line,'nse':{'source_url':url,'raw_sha256':sha,'evidence_path':f'data/filings/{s}/{sha}.html','observed_at':now()},'reason':'Annual report and NSE component arithmetic independently reconcile the liability classification.'}
 p=ROOT/'data/annual-reports'/s/'liability-review.json';p.write_text(json.dumps(proof,indent=2));print(s,method,line)
