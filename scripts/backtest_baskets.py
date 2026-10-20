#!/usr/bin/env python3
"""Daily backtest of the additional paper baskets on saved candles (read-only).

Uses marketdesk.basket_rules (the live rules) and execution.charges (the live fee model). Signals use the
previous completed close; orders fill at the next session's open with 0.25% adverse slippage; stops fill at
the stop price, or the open when the open gaps through it. Equal weight, up to ₹10,000 per stock on ₹1,00,000.
Limits: today's Nifty 500 membership for the whole history (survivorship bias), daily bars only, no dividends."""
import argparse,collections,json,sqlite3,statistics,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from marketdesk.basket_rules import BASKETS,stats,ranking,rebalance_exit,daily_exit,stop_exit
from marketdesk.execution import charges
from marketdesk.working_universe import symbol_filter
SLIP=.0025

def load(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    bars=collections.defaultdict(list)
    for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):bars[r['symbol']].append(dict(r))
    load.sectors=dict(db.execute('SELECT symbol,sector FROM instruments'));load.bench=dict(db.execute('SELECT day,close FROM claude_benchmark'))
    return bars

def prepare(bars):
    """{day: {symbol: stats as of that completed close}} and {symbol: {day: bar}}."""
    by_day={s:{r['ts'][:10]:r for r in rows} for s,rows in bars.items()};signals=collections.defaultdict(dict)
    for s,rows in bars.items():
        for n in range(len(rows)):
            st=stats(rows[max(0,n-299):n+1],bench=getattr(load,'bench',None))
            if st:st['sector']=getattr(load,'sectors',{}).get(s);signals[rows[n]['ts'][:10]][s]=st
    return signals,by_day

def run(basket,signals,by_day,capital=100000,start=None):
    rule=BASKETS[basket];days=sorted(signals);days=[d for d in days if not start or d>=start]
    cash=float(capital);pos={};trades=[];equity=[];prev=None;fees=0.
    for i,day in enumerate(days):
        if prev:
            sig=signals[prev];order=ranking(basket,sig)
            review=rule['rebalance']=='monthly' and day[:7]!=prev[:7]
            sells={}
            for s,p in pos.items():
                if not by_day[s].get(day):continue
                reason=rebalance_exit(basket,s,sig.get(s),order) if review else daily_exit(basket,sig.get(s),i-p['i'])
                if reason:sells[s]=reason
            for s,p in list(pos.items()):
                bar=by_day[s].get(day)
                if not bar:continue
                level=p['cost']/p['qty']*(1-rule['stop_pct']/100);px=None
                if s in sells:px,reason=bar['open'],sells[s]
                elif bar['open']<=level:px,reason=bar['open'],'stop (gap)'
                elif bar['low']<=level:px,reason=level,'stop'
                if px is None:continue
                value=p['qty']*px*(1-SLIP);fee=charges(round(value,2),'SELL',True)['total'];cash+=value-fee;fees+=fee
                trades.append(dict(symbol=s,entry=p['day'],exit=day,pnl=value-fee-p['cost'],ret=(value-fee)/p['cost']-1,reason=reason,days=i-p['i']));del pos[s]
            targets=order[:rule['max_positions']] if rule['rebalance']=='monthly' else order
            for s in targets:
                if len(pos)>=rule['max_positions']:break
                bar=by_day[s].get(day)
                if s in pos or not bar or any(t['symbol']==s and t['exit']==day for t in trades[-20:]):continue
                price=bar['open']*(1+SLIP);budget=min(cash,capital*rule['position_pct']/100)
                qty=int(max(0,budget-25)//(price*1.025))
                if qty<1:continue
                value=qty*price;fee=charges(round(value,2),'BUY')['total']
                if value+fee>cash or fee+charges(round(value,2),'SELL',True)['total']>value*.015:continue
                cash-=value+fee;fees+=fee;pos[s]=dict(qty=qty,cost=value+fee,day=day,i=i)
        equity.append((day,cash+sum(p['qty']*by_day[s].get(day,{}).get('close',p['cost']/p['qty']) for s,p in pos.items())));prev=day
    return trades,equity,pos,fees

def metrics(trades,equity,capital,fees):
    wins=[t for t in trades if t['pnl']>0];peak=capital;dd=0
    for _,e in equity:peak=max(peak,e);dd=max(dd,1-e/peak)
    gp=sum(t['pnl'] for t in wins);gl=-sum(t['pnl'] for t in trades if t['pnl']<=0)
    return dict(closed_trades=len(trades),win_rate=round(len(wins)/len(trades),3) if trades else None,profit_factor=round(gp/gl,2) if gl else None,
        return_pct=round(100*(equity[-1][1]/capital-1),2),max_drawdown_pct=round(100*dd,2),fees=round(fees,2),
        avg_days_held=round(statistics.mean(t['days'] for t in trades),1) if trades else None)

def main():
    a=argparse.ArgumentParser();a.add_argument('--database',required=True);a.add_argument('--output',required=True);args=a.parse_args()
    bars=load(args.database);signals,by_day=prepare(bars);days=sorted(signals);cut=days[len(days)*6//10]
    def ew(s,e):
        r=[by_day[k][e]['close']/by_day[k][s]['close']-1 for k in by_day if s in by_day[k] and e in by_day[k]];return round(100*statistics.mean(r),2)
    out={'window':{'first':days[0],'last':days[-1],'sessions':len(days),'split':cut},
         'equal_weight_hold_pct':{'full':ew(days[0],days[-1]),'first60pct':ew(days[0],cut),'last40pct':ew(cut,days[-1])},'results':[]}
    for basket in BASKETS:
        for period,start in (('full',None),('last40pct',cut)):
            t,e,pos,fees=run(basket,signals,by_day,start=start)
            if period=='first60pct':pass
            out['results'].append(dict(basket=basket,period=period,open_positions=len(pos),**metrics(t,e,100000,fees)))
            print(out['results'][-1],flush=True)
    Path(args.output).write_text(json.dumps(out,indent=1))
    print(out['window'],out['equal_weight_hold_pct'])

if __name__=='__main__':main()
