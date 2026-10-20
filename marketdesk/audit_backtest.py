"""Offline daily-open audit proxy. No network, configuration writes or deployment.

Reuses production signal/risk/fee functions. Daily bars cannot reproduce the live
engine or depth fills. Results are conditional sensitivity checks, not approval
for strategy changes. Investor evidence must predate the simulated decision.
"""
import argparse,collections,json,math,sqlite3,statistics
from bisect import bisect_left
from datetime import datetime,timezone
from pathlib import Path
from .claude_trader import features,eligible,quantity,holding_stop_risk,fits
from .autotrading import IST,entry_signal,exit_signal,investor_stop_risk,investor_risk_quantity,investor_buy_fits
from .replay import signal_from
from .quality import financial_reasons,source_reasons,inspect_history
from .execution import charges,money
from .sectors import sector_group

PROTOCOL={
 'model':'Daily-open proxy; current rules sampled once per session. No intraday stops, live breakout or order-book reconstruction.',
 'fill':'Next daily open plus 10 bps adverse slippage each side. At most 1% of prior 20-day mean volume per symbol/session; IOC remainder cancelled. This is assumed capacity, not observed depth.',
 'universe':'Frozen current Nifty 500 membership; survivorship bias. Saved candles may include later revisions. No dividends or corporate-action ledger.',
 'costs':'Unchanged marketdesk.execution.charges; delivery estimates, DP on first symbol sale per day, charged fee rounding. Flat current schedule applied throughout for comparability, not historical tariff reconstruction.',
 'variants':'Predeclared: Trader completed breakout, index on/off, sector max 2/none, fee ceiling 1%/none plus active 2.5% reference. Investor risk cap 3%/6%, fee ceiling 1%/none plus active 2.5% reference; ₹3500 target fixed at both capital sizes.',
 'validation':'Full longest locally saved eligible period plus independent chronological first-70% and last-30% resets. No tuning. Period already used by earlier project research; last 30% is temporal validation, not guaranteed untouched out-of-sample.',
 'decision':'All live-strategy recommendations inconclusive without historical depth/full intraday coverage. Fewer than 30 closed trades is also inconclusive.'}


def load(path):
 db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
 try:
  db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  membership=json.loads(db.execute("SELECT value FROM state WHERE key='nifty500_membership'").fetchone()[0]);symbols=sorted(membership['members'])
  bars={};signals={};sectors={};funds={};evidence={}
  for s in symbols:
   rows=[dict(r) for r in db.execute('SELECT * FROM candles WHERE symbol=? ORDER BY ts',(s,))]
   bars[s]={r['ts'][:10]:r for r in rows};signals[s]={r['as_of']:r for r in features(rows) or []}
   sector=db.execute('SELECT sector FROM instruments WHERE symbol=?',(s,)).fetchone();sectors[s]=sector_group(sector[0] if sector else None,symbol=s)
   fund=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(s,)).fetchone()
   funds[s]=(json.loads(fund['details']),fund['observed_at']) if fund else ({},None)
   evidence[s]={r['endpoint']:r['observed_at'] for r in db.execute('SELECT endpoint,observed_at FROM payloads WHERE symbol=?',(s,))}
  benchmark=dict(db.execute('SELECT day,close FROM claude_benchmark ORDER BY day'));bmdays=sorted(benchmark)
  bm={d:dict(as_of=d,allowed=benchmark[d]>sum(benchmark[k] for k in bmdays[n-200:n])/200) for n,d in enumerate(bmdays) if n>=200}
  count=collections.Counter(d for rows in bars.values() for d in rows)
  sessions=sorted(d for d,n in count.items() if n>=len(symbols)//2 and d<datetime.now(IST).date().isoformat())
  return dict(symbols=symbols,bars=bars,signals=signals,sectors=sectors,funds=funds,evidence=evidence,benchmark=bm,sessions=sessions,membership_fetched_at=membership['fetched_at'])
 finally:db.close()


def metrics(trades,daily,capital,positions):
 closed=[t for t in trades if t['side']=='SELL' and t['closed_pnl'] is not None];pnl=[t['closed_pnl'] for t in closed]
 wins=[x for x in pnl if x>0];losses=[x for x in pnl if x<0];peak=capital;dd=0
 for d in daily:peak=max(peak,d['equity']);dd=max(dd,(peak-d['equity'])/peak*100)
 fees=sum(t['fee'] for t in trades);gross_profit=sum(t['closed_gross_pnl'] for t in closed if t['closed_gross_pnl']>0)
 return dict(buy_fills=sum(t['side']=='BUY' for t in trades),sell_fills=sum(t['side']=='SELL' for t in trades),closed_trades=len(closed),
  win_rate_pct=len(wins)/len(pnl)*100 if pnl else None,average_win=statistics.mean(wins) if wins else None,average_loss=statistics.mean(losses) if losses else None,
  profit_factor=sum(wins)/-sum(losses) if losses else None,net_return_pct=(daily[-1]['equity']/capital-1)*100 if daily else None,
  max_daily_close_drawdown_pct=dd,fees=fees,fees_pct_closed_gross_profit=fees/gross_profit*100 if gross_profit>0 else None,
  average_close_capital_deployed_pct=statistics.mean((d['equity']-d['cash'])/d['equity']*100 for d in daily) if daily else None,
  open_positions=len(positions),status='inconclusive: daily proxy; '+('fewer than 30 closed trades' if len(closed)<30 else 'no historical depth or exact intraday execution'))


def run(data,days,capital,portfolio,*,index=False,sector_limit=None,fee_limit=None,risk_cap=3):
 cash=capital;positions={};trades=[];daily=[];rejects=collections.Counter();coverage=[]
 sessions=data['sessions'];idx={d:n for n,d in enumerate(sessions)};hist_days={s:sorted(data['bars'][s]) for s in data['symbols']}
 for day in days:
  clock=datetime.fromisoformat(day+'T09:20:00').replace(tzinfo=IST);n=idx[day];prior_day=sessions[n-1] if n else None
  cache=data.setdefault('_frames',{})
  if (portfolio,day) not in cache:
   frames={};opens={};caps={};eligible_funds=0;quality_ok=set()
   for s in data['symbols']:
    bar=data['bars'][s].get(day);ds=hist_days[s];cut=bisect_left(ds,day);prior=[data['bars'][s][d] for d in ds[max(0,cut-201):cut]]
    if bar and bar['open']>0:opens[s]=bar['open']
    if len(prior)<201 or prior[-1]['ts'][:10]!=prior_day:continue
    caps[s]=int(sum(r['volume'] for r in prior[-20:])/20*.01)
    if not inspect_history(prior,sessions[:n],clock):quality_ok.add(s)
    if portfolio=='trader':
     sig=data['signals'][s].get(prior_day)
     if sig:frames[s]=sig
    else:
     f,observed=data['funds'][s]
     if not financial_reasons(f,observed,clock) and not source_reasons(f,data['evidence'][s],clock):
      frames[s]=signal_from(prior,f);eligible_funds+=1
     else:
      # Exit trend still uses prior completed bars; missing financial evidence cannot force an exit.
      frames[s]=signal_from(prior,{})
   cache[portfolio,day]=(frames,opens,caps,eligible_funds,quality_ok)
  frames,opens,caps,eligible_funds,quality_ok=cache[portfolio,day]
  coverage.append(dict(session=day,price_signals=len(frames),investor_evidence_eligible=eligible_funds if portfolio=='investor' else None))
  def equity(prices):return cash+sum(p['quantity']*prices.get(s,p['mark']) for s,p in positions.items())
  opening_equity=equity(opens);sold=set();used=collections.Counter()
  for s,p in list(positions.items()):
   if s not in opens:continue
   sig=frames.get(s);op=opens[s]
   if portfolio=='trader':
    if sig and sig['as_of']>=p['entry_day']:
     p['highest']=max(p['highest'],sig['close']);p['stop']=max(p['stop'],p['highest']-3*sig['atr']);p['ma_exit']=sig['close']<sig['dma50']
    reason='ATR stop at next observed open' if op<=p['stop'] else 'completed MA exit' if p.get('ma_exit') else None
   else:reason=exit_signal('investor',sig,p,op)
   if not reason:continue
   q=min(p['quantity'],caps.get(s,0));price=op*.999
   if q<1:rejects['exit_capacity']+=1;continue
   value=money(q*price);fee=charges(value,'SELL',s not in sold)['total'];remaining=p['quantity']-q;cost=money(p['cost']*remaining/p['quantity']);removed=p['cost']-cost
   cash=money(cash+value-fee);p['net_realized']+=value-fee-removed;p['gross_realized']+=q*(price-p['entry'])
   trades.append(dict(session=day,symbol=s,side='SELL',quantity=q,price=price,fee=fee,reason=reason,closed_pnl=p['net_realized'] if not remaining else None,closed_gross_pnl=p['gross_realized'] if not remaining else None))
   p.update(quantity=remaining,cost=cost);sold.add(s);used[s]+=q
   if not remaining:del positions[s]
  candidates=[];bm=data['benchmark'].get(prior_day)
  for s,sig in frames.items():
   if s not in opens or s in positions or s in sold or s not in quality_ok:continue
   if portfolio=='trader':
    if eligible(sig,opens[s],clock,bm,use_market_filter=index,live_breakout=False):candidates.append((sig['rank'],s))
   else:
    result=entry_signal('investor',sig,opens[s],clock)
    if result:candidates.append((result[1],s))
  buys=0
  for _,s in sorted(candidates,key=lambda x:(-x[0],x[1])):
   if len(positions)>=10 or buys>=10:break
   sig=frames[s];op=opens[s];price=op*1.001;eq=equity(opens)
   if portfolio=='investor' and eq<=opening_equity*.98:rejects['daily_loss']+=1;break
   if sector_limit and sum(data['sectors'][k]==data['sectors'][s] for k in positions)>=sector_limit:rejects['sector']+=1;continue
   budget=min(cash,capital*.1,3500 if portfolio=='investor' else math.inf)
   if portfolio=='trader':
    heat=sum(holding_stop_risk(p,p) for p in positions.values());q=quantity(op*1.005,sig['atr'],eq,heat,budget)
   else:
    heat=sum(investor_stop_risk(p['cost']) for p in positions.values());risk=dict(equity=eq,risk=heat,cap_pct=risk_cap)
    q=investor_risk_quantity(max(0,math.floor((budget-25)/(op*1.025))),op*1.005,op,risk)
   requested=q;q=min(q,max(0,caps.get(s,0)-used[s]))
   if q<1:rejects['risk_or_capacity']+=1;continue
   value=money(q*price);fee=charges(value,'BUY')['total']
   if fee_limit is not None and money(fee+charges(value,'SELL',True)['total'])*100>fee_limit*value:rejects['fee_floor']+=1;continue
   if value+fee>budget:rejects['budget']+=1;continue
   if portfolio=='trader' and not fits(q,price,sig['atr'],eq,heat,budget,op,fee):rejects['actual_fill_risk']+=1;continue
   if portfolio=='investor' and not investor_buy_fits(q,price,op,risk,fee):rejects['actual_fill_risk']+=1;continue
   cash=money(cash-value-fee);positions[s]=dict(quantity=q,cost=value+fee,entry=price,entry_day=day,mark=price,highest=price,stop=price-2*sig['atr'] if portfolio=='trader' else price*.9,ma_exit=False,net_realized=0,gross_realized=0)
   trades.append(dict(session=day,symbol=s,side='BUY',quantity=q,requested_quantity=requested,price=price,fee=fee));used[s]+=q;buys+=1
  stale=[]
  for s,p in positions.items():
   bar=data['bars'][s].get(day)
   if bar:p['mark']=bar['close']
   else:stale.append(s)
  daily.append(dict(session=day,cash=cash,equity=equity({}),positions=len(positions),stale_marks=stale))
 result=dict(portfolio=portfolio,capital=capital,index_filter=index,sector_limit=sector_limit,fee_limit=fee_limit,risk_cap=risk_cap,start=days[0],end=days[-1],sessions=len(days),trades=trades,daily=daily,rejections=dict(rejects),coverage=coverage)
 result['metrics']=metrics(trades,daily,capital,positions)
 return result


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--database',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
 out=Path(args.output);out.mkdir(parents=True,exist_ok=True);(out/'protocol.json').write_text(json.dumps(PROTOCOL,indent=2))
 data=load(args.database);all_days=[d for d in data['sessions'] if any(k<d for k in data['benchmark'])]
 cut=int(len(all_days)*.7);periods={'full':all_days,'development':all_days[:cut],'temporal_check':all_days[cut:]};summary=[]
 variants=[('trader','close_fee_2.5',{'fee_limit':2.5}),('trader','close_no_floor',{}),('trader','close_index_on',{'index':True}),('trader','close_sector_2',{'sector_limit':2}),('trader','close_fee_1',{'fee_limit':1})]
 variants += [('investor',f'risk_{risk}_fee_{floor}',{'risk_cap':risk,'fee_limit':floor}) for risk in (3,6) for floor in (None,1,2.5)]
 for portfolio,name,kwargs in variants:
  for capital in (100000,1000000):
   for period,days in periods.items():
    result=run(data,days,capital,portfolio,**kwargs);filename=f'{portfolio}-{name}-{capital}-{period}.json';(out/filename).write_text(json.dumps(result,indent=2,allow_nan=False))
    summary.append(dict(portfolio=portfolio,variant=name,capital=capital,period=period,start=days[0],end=days[-1],file=filename,**result['metrics']))
   print(portfolio,name,capital,'complete',flush=True)
 (out/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False))
 (out/'coverage.json').write_text(json.dumps({'symbols':len(data['symbols']),'first_raw_session':data['sessions'][0],'last_raw_session':data['sessions'][-1],'periods':{k:{'first':v[0],'last':v[-1],'sessions':len(v)} for k,v in periods.items()},'membership_fetched_at':data['membership_fetched_at']},indent=2))

if __name__=='__main__':main()
