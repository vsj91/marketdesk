import sys,re,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()))
import pymupdf
from marketdesk.annual_reports import balance_text
for s in sys.argv[1:]:
 folder=Path('tmp/remaining-reviews')/s;doc=pymupdf.open(folder/'report.pdf');out=[]
 for i,p in enumerate(doc):
  plain=' '.join(p.get_text().lower().split())
  if re.search(r'lease liabilit',plain) and re.search(r'closing|end of|carrying|reconciliation|maturity|current|total',plain):
   t=balance_text(p);(folder/f'lease-{i+1}.txt').write_text(t);out.append(i+1)
 print(s,out)
