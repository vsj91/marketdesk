#!/usr/bin/env python3
"""Read-only candidate diagnostics; never creates portfolio orders or changes rules."""
import sys,json,html,collections
from pathlib import Path
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.storage import ROOT,DEFAULT_DB,connect
from marketdesk.replay import load_snapshot
from marketdesk.claude_trader import features,eligible
from marketdesk.blend_candidate import trader,investor,VERSION
from marketdesk.quality import inspect_history,IST

snapshot=load_snapshot(DEFAULT_DB)
with connect(DEFAULT_DB) as db:
 metadata={r['symbol']:dict(r) for r in db.execute('SELECT * FROM instruments')}
 quality={r['symbol']:json.loads(r['report']) for r in db.execute('SELECT * FROM quality')}
rows=[];groups={};blocks=collections.Counter()
for symbol in snapshot['symbols']:
 history=snapshot['histories'].get(symbol,[]);signals=features(history);groups[symbol]=signals or []
 signal=signals[-1] if signals else None;q=quality.get(symbol,{})
 t=trader(signal,signal['close'] if signal else 0,q.get('trader_ready',False))
 f=snapshot['funds'].get(symbol,({},None))[0];m=metadata[symbol]
 i=investor(f,q,m.get('sector'),m['isin'])
 blocks.update(i['reasons'])
 rows.append(dict(symbol=symbol,trader_candidate=t['eligible'],trader_score=t['score'],investor_quantitative=i['quantitative_pass'],investor_candidate=i['eligible'],investor_reasons=i['reasons']))
# Entry-condition diagnostics only: previous completed data, next-session open.
# This is deliberately not represented as a portfolio or profitability backtest.
days=snapshot['sessions'][-20:];diagnostics=[]
for day in days:
 clock=datetime.fromisoformat(day+'T09:20:00').replace(tzinfo=IST);base=0;candidate=0;valid=0
 prior_sessions=[s for s in snapshot['sessions'] if s<day]
 for symbol,signals in groups.items():
  prev=next((s for s in reversed(signals) if s['as_of']<day),None)
  history=snapshot['histories'].get(symbol,[]);bar=next((r for r in history if r['ts'][:10]==day),None)
  before=[r for r in history if r['ts'][:10]<day][-201:]
  if not prev or not bar or symbol in snapshot['bad'] or inspect_history(before,prior_sessions,clock):continue
  valid+=1;benchmark={'as_of':prior_sessions[-1],'allowed':True}
  b=eligible(prev,bar['open'],clock,benchmark,use_market_filter=False)
  base+=bool(b);candidate+=bool(b and trader(prev,bar['open'])['eligible'])
 diagnostics.append(dict(day=day,price_eligible=valid,baseline_entries=base,candidate_entries=candidate))
report=dict(version=VERSION,generated_at=datetime.now(timezone.utc).isoformat(),active=False,input_sha256=snapshot['input_sha256'],stocks=len(rows),
 trader_shortlist=sum(r['trader_candidate'] for r in rows),investor_quantitative=sum(r['investor_quantitative'] for r in rows),investor_buy_eligible=sum(r['investor_candidate'] for r in rows),investor_blockers=dict(blocks),diagnostics=diagnostics,rows=rows)
folder=ROOT/'data/strategy-lab';folder.mkdir(parents=True,exist_ok=True);(folder/'candidate.json').write_text(json.dumps(report,indent=2))
e=html.escape
body=f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MarketDesk · Draft buy rules</title><link rel="stylesheet" href="/style.css"></head><body><main><a href="/">Back to MarketDesk</a><h1>Draft buy rules · inactive</h1><p>Nifty 500 · {len(rows)} instruments · snapshot {e(report['generated_at'])}</p><div class="note">These original rules are developed and tested separately. The running baskets keep their approved rules. Entry-condition checks do not establish profitability, and no automatic activation is permitted.</div><h2>Trader: liquid, controlled entries</h2><p>Completed breakout, positive 50/200-day trend, volume ratio at least 1.2, positive 63-session momentum, average daily turnover at least ₹5 crore, price at least ₹20, ATR no more than 4% of price, and an entry gap no greater than half an ATR. Rank by momentum relative to volatility. Existing execution and portfolio-risk checks remain prerequisites.</p><p>{report['trader_shortlist']} stocks pass the current closing-price screen. A close is not an executable live quote.</p><h2>Investor: profitable growth, value and business review</h2><p>All current data checks; positive profit and equity; annual profit / closing equity at least 15%; comparable revenue and profit growth at least 10%; borrowings plus leases / equity at most 0.75; finance-cost coverage at least 4. Financial companies require a separate capital and asset-quality assessment.</p><p>{report['investor_quantitative']} stocks pass the numerical screen. {report['investor_buy_eligible']} pass the complete draft buy rules: dated, issuer-matched valuation, governance and business-quality reviews are required and have not been supplied. Missing reviews are not guessed from price, market-cap labels or growth.</p><h2>What was tested</h2><p>Unit tests cover missing evidence, stale/future reviews, failed breakouts, expensive entry gaps and sector exceptions. The table below checks prior-day entry conditions over 20 saved sessions against the current Trader entry predicate. It does not simulate exits, portfolio sizing, intraday depth or P&amp;L. Current index membership introduces survivorship bias. Historical point-in-time fundamental and qualitative-review coverage is insufficient to validate Investor performance.</p><table><thead><tr><th>Session</th><th>Price checks pass</th><th>Baseline entries</th><th>Draft entries</th></tr></thead><tbody>'''
body+=''.join(f"<tr><td>{d['day']}</td><td>{d['price_eligible']}</td><td>{d['baseline_entries']}</td><td>{d['candidate_entries']}</td></tr>" for d in diagnostics)
body+='</tbody></table><h2>Current Nifty 500 screens</h2><table><thead><tr><th>Stock</th><th>Trader screen</th><th>Investor numerical screen</th><th>Investor review blockers</th></tr></thead><tbody>'
body+=''.join(f"<tr><td>{e(r['symbol'])}</td><td>{'Pass' if r['trader_candidate'] else 'Wait'}</td><td>{'Pass' if r['investor_quantitative'] else 'Wait'}</td><td>{e('; '.join(r['investor_reasons']))}</td></tr>" for r in rows)
body+='''</tbody></table><h2>Research principles</h2><p>These are our testable rules, not copies of proprietary strategies. <a href="https://www.motilaloswalmf.com/motilal-oswal-edge/qglp">Motilal Oswal: quality, growth, longevity and price</a> · <a href="https://www.whiteoakcapitalpartners.com/investment-approach/investment-philosophy.php">White Oak: business quality and valuation</a> · <a href="https://am.jpmorgan.com/wr/en/asset-management/institutional/investment-strategies/equities/">J.P. Morgan equity research</a>.</p></main></body></html>'''
(ROOT/'web/strategy-lab.html').write_text(body)
print(json.dumps({k:v for k,v in report.items() if k not in ('rows','diagnostics','investor_blockers')}))
