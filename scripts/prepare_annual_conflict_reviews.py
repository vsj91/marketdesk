#!/usr/bin/env python3
"""Save reviewed primary-statement evidence; this does not write financial data."""
import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pymupdf
from marketdesk.storage import ROOT,connect,DEFAULT_DB
from marketdesk.annual_reports import balance_text
from marketdesk.intraday_data import atomic_json
from marketdesk.annual_conflicts import reconcile_reviewed
for symbol,pages,layout in [('FISCHER',[172],'full_page'),('ORIENTBELL',[87],'left_half')]:
 folder=ROOT/'data/annual-reports'/symbol;download=json.loads((folder/'conflict-download.json').read_text());doc=pymupdf.open(folder/'conflict-full.pdf');excerpt=pymupdf.open()
 for n in pages:excerpt.insert_pdf(doc,from_page=n-1,to_page=n-1)
 path=folder/'conflict-reviewed.pdf';excerpt.save(path,garbage=4,deflate=True,no_new_id=True);excerpt.close()
 with connect(DEFAULT_DB) as db:
  row=db.execute('select isin,details from instruments join fundamentals using(symbol) where symbol=?',(symbol,)).fetchone();details=json.loads(row['details'])
 p=doc[pages[0]-1];clip=pymupdf.Rect(0,0,p.rect.width/2,p.rect.height) if layout=='left_half' else None;text=balance_text(p,clip)
 report={**download,'symbol':symbol,'isin':row['isin'],'status':'verified','evidence_path':str(path.relative_to(ROOT)),'excerpt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'evidence':{'pages':pages,'layout':layout}}
 if symbol=='FISCHER':
  report['reviewed_navigation_lines']=[text.splitlines()[0]]
  fields={'equity':'Total Equity','cash_and_cash_equivalents':'Cash and cash equivalents'}
  nse=json.loads((folder/'conflict-fresh-nse.json').read_text())
  report['review']={'field':'equity','statement':'balance','reason':'Company consolidated balance sheet reports equity of 385.1422 crore, independently confirmed by the corroborating audited NSE filing; original discrepancy preserved.','nse':{'evidence_path':'data/filings/'+symbol+'/'+nse['raw_sha256']+'.html','raw_sha256':nse['raw_sha256'],'source_url':nse['source_url']}}
 else:
  report['reviewed_glyphs']=[['H lakh','₹ lakh']]
  fields={'profit_before_tax':'IX   Profit/(loss) before tax','finance_costs':'(e) Finance costs','net_profit':'XI   Profit/(loss) for the year','tax_expense':'Total tax expense','before_exceptional':'VII  Profit/ (loss) before exceptional items and tax','exceptional_items':'VIII Exceptional Items'}
  report['review']={'field':'profit_before_tax','statement':'income','reason':'Company consolidated annual income statement reports final PBT including associates; verified against profit plus tax and the exceptional-item bridge, with matching net-profit and finance-cost anchors.'}
 lines={}
 for field,needle in fields.items():
  found=[l.strip() for l in text.splitlines() if needle in l]
  assert len(found)==1,(symbol,field,found)
  lines[field]=found[0]
 report['review']['lines']=lines
 updated,record=reconcile_reviewed(details,report,symbol,row['isin']);atomic_json(folder/'conflict-review.json',report)
 print(symbol,record['changes'])
