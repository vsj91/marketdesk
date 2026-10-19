#!/usr/bin/env python3
"""Daily backtest of the live Claude V2 Trader rules on saved candles (read-only).

Reuses claude_trader.features/eligible/quantity and execution.charges, so entry, sizing and fees match the
live code. Signals use completed closes; entries fill at the next session's open plus slippage; the initial
2 ATR stop and the 3 ATR trailing stop (ratcheting from the following session) fill at the stop price, or
the open on a gap; a completed close below the prior 50-day average exits at the next open.
Limits: frozen current Nifty 500 membership (survivorship bias), daily bars only, no dividends, no market filter."""
import argparse,collections,json,sqlite3,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime,timezone
from unittest.mock import patch
import marketdesk.claude_trader as ct
from marketdesk.execution import charges
from marketdesk.working_universe import symbol_filter
SLIP=.0025

def load(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    symbols=[r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))]
    bars=collections.defaultdict(list)
    for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):bars[r['symbol']].append(dict(r))
    return {s:bars[s] for s in symbols if bars[s]}

def prepare(bars):
    feats={};by_day={}
    for s,rows in bars.items():
        f=ct.features(rows);by_day[s]={r['ts'][:10]:r for r in rows}
        if f:feats[s]={x['as_of']:x for x in f}
    return feats,by_day

def benchmark_gate(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);rows=db.execute('SELECT day,close FROM claude_benchmark ORDER BY day').fetchall()
    return {d:c>sum(x[1] for x in rows[n-199:n+1])/200 for n,(d,c) in enumerate(rows) if n>=199}

def run(bars,feats,by_day,capital,trade_risk,heat_cap,max_positions=10,max_buys=10,start=None,end=None,gate=None):
    days=sorted({d for s in by_day for d in by_day[s]});days=[d for d in days if (not start or d>=start) and (not end or d<=end)]
    cash=float(capital);pos={};trades=[];equity=[];prev=None
    with patch.object(ct,'TRADE_RISK',trade_risk),patch.object(ct,'HEAT_CAP',heat_cap):
        for i,day in enumerate(days):
            # 1. exits on today's bar
            for s in list(pos):
                p=pos[s];bar=by_day[s].get(day)
                if not bar:continue
                exit_px=None;reason=None
                if bar['open']<=p['stop']:exit_px,reason=bar['open'],'stop(gap)'
                elif bar['low']<=p['stop']:exit_px,reason=p['stop'],'stop'
                elif p['ma_exit']:exit_px,reason=bar['open'],'trend'
                if exit_px:
                    px=exit_px*(1-SLIP);val=p['qty']*px;fee=charges(round(val,2),'SELL',True)['total'];cash+=val-fee
                    trades.append(dict(symbol=s,entry=p['entry_day'],exit=day,pnl=val-fee-p['cost'],ret=(val-fee)/p['cost']-1,reason=reason,days=i-p['i']));del pos[s];continue
                # end-of-day updates from the completed close
                p['highest']=max(p['highest'],bar['close']);f=feats.get(s,{}).get(day)
                if f and p['i']<i:p['stop']=max(p['stop'],p['highest']-3*f['atr'])
                if f and bar['close']<f['dma50']:p['ma_exit']=True
            # 2. entries from yesterday's completed signals
            if prev and (gate is None or gate.get(prev)):
                cands=[]
                for s,fd in feats.items():
                    f=fd.get(prev)
                    if f and s not in pos and by_day[s].get(day) and ct.eligible(f,by_day[s][day]['open'],datetime.fromisoformat(day+'T09:20:00+05:30'),dict(as_of=prev,allowed=True),use_market_filter=False):cands.append((f['rank'],s,f))
                cands.sort(reverse=True);buys=0
                for _,s,f in cands:
                    if len(pos)>=max_positions or buys>=max_buys:break
                    bar=by_day[s][day];price=bar['open']*(1+SLIP)
                    mark=sum(q['qty']*by_day[k].get(day,{}).get('close',q['entry_px']) for k,q in pos.items())
                    eq=cash+mark
                    heat=sum(max(0,q['cost']-q['qty']*q['stop']+charges(round(q['qty']*q['stop'],2),'SELL',True)['total'])+q['qty']*q['entry_px']*.005 for q in pos.values())
                    qty=ct.quantity(price*1.005,f['atr'],eq,heat,min(cash,capital*.1))
                    if qty<1:continue
                    val=qty*price;fee=charges(round(val,2),'BUY')['total']
                    if val+fee>cash:continue
                    cash-=val+fee;buys+=1
                    pos[s]=dict(qty=qty,entry_px=price,cost=val+fee,stop=price-2*f['atr'],highest=bar['close'],ma_exit=False,entry_day=day,i=i)
            equity.append((day,cash+sum(p['qty']*by_day[s].get(day,{}).get('close',p['entry_px']) for s,p in pos.items())));prev=day
    return trades,equity,pos

def metrics(trades,equity,capital):
    wins=[t for t in trades if t['pnl']>0];loss=[t for t in trades if t['pnl']<=0]
    peak=capital;dd=0
    for _,e in equity:peak=max(peak,e);dd=max(dd,1-e/peak)
    gp=sum(t['pnl'] for t in wins);gl=-sum(t['pnl'] for t in loss)
    return dict(closed_trades=len(trades),win_rate=round(len(wins)/len(trades),3) if trades else None,avg_win_pct=round(100*statistics.mean(t['ret'] for t in wins),2) if wins else None,
        avg_loss_pct=round(100*statistics.mean(t['ret'] for t in loss),2) if loss else None,profit_factor=round(gp/gl,2) if gl else None,
        net_pnl=round(equity[-1][1]-capital,2),return_pct=round(100*(equity[-1][1]/capital-1),2),max_drawdown_pct=round(100*dd,2),avg_days_held=round(statistics.mean(t['days'] for t in trades),1) if trades else None)

def main():
    a=argparse.ArgumentParser();a.add_argument('--database',required=True);a.add_argument('--output',required=True);args=a.parse_args()
    bars=load(args.database);feats,by_day=prepare(bars)
    all_days=sorted({d for s in by_day for d in by_day[s]});first=min(d for f in feats.values() for d in f)
    days=[d for d in all_days if d>=first];cut=days[int(len(days)*.6)]
    # equal-weight buy-and-hold of the same universe over the tested window, for reference
    def bh(start,end):
        r=[by_day[s][end]['close']/by_day[s][start]['close']-1 for s in by_day if start in by_day[s] and end in by_day[s]];return round(100*statistics.mean(r),2)
    out={'window':{'first_signal_day':first,'last_day':days[-1],'sessions':len(days),'split_day':cut},'equal_weight_buy_and_hold_pct':{'full':bh(days[0],days[-1]),'first60pct':bh(days[0],cut),'last40pct':bh(cut,days[-1])},'results':[]}
    gate=benchmark_gate(args.database)
    for name,tr,hc,g in (('half_percent_3_heat',.005,.03,None),('one_percent_6_heat',.01,.06,None),('half_percent_3_heat+nifty200',.005,.03,gate),('one_percent_6_heat+nifty200',.01,.06,gate)):
        for period,(s,e) in {'full':(None,None),'first60pct':(None,cut),'last40pct':(cut,None)}.items():
            for capital in (100000,):
                trades,equity,_=run(bars,feats,by_day,capital,tr,hc,start=s or days[0],end=e,gate=g)
                out['results'].append(dict(variant=name,period=period,capital=capital,**metrics(trades,equity,capital)))
                print(name,period,out['results'][-1],flush=True)
    Path(args.output).write_text(json.dumps(out,indent=2))
if __name__=='__main__':main()
