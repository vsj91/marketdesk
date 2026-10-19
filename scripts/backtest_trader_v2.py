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
from marketdesk.exposure import sector_group
from marketdesk.execution import charges
from marketdesk.working_universe import symbol_filter
SLIP=.0025

def load(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    symbols=[r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))]
    bars=collections.defaultdict(list)
    for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):bars[r['symbol']].append(dict(r))
    load.sectors={r['symbol']:sector_group(r['sector']) for r in db.execute('SELECT symbol,sector FROM instruments')}
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

def run(bars,feats,by_day,capital,trade_risk,heat_cap,max_positions=10,max_buys=10,start=None,end=None,gate=None,sector_cap=None,sector_strength=False):
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
                if sector_strength:
                    by_sector=collections.defaultdict(list)
                    for s2,fd in feats.items():
                        f2=fd.get(prev)
                        if f2:by_sector[load.sectors.get(s2,'Unknown')].append(f2['rank'])
                    allr=[r for v in by_sector.values() for r in v]
                    if allr:
                        market=statistics.median(allr)
                        strong={k for k,v in by_sector.items() if len(v)>=5 and statistics.median(v)>market}
                        cands=[c for c in cands if load.sectors.get(c[1],'Unknown') in strong]
                cands.sort(reverse=True);buys=0
                for _,s,f in cands:
                    if len(pos)>=max_positions or buys>=max_buys:break
                    if sector_cap and sum(load.sectors.get(k)==load.sectors.get(s) for k in pos)>=sector_cap:continue
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

def run_trend(bars,feats,by_day,capital,gate=None,size=10000,max_positions=10,stop=.10,start=None,end=None):
    """Price-only proxy for the Investor's trend rules (fundamentals cannot be tested: no point-in-time filings).
    Entry: completed close above the 200-day average with positive 63-session return, ranked by that return;
    exit: 10% stop, or a completed close below the 200-day average (next open)."""
    days=sorted({d for s2 in by_day for d in by_day[s2]});days=[d for d in days if (not start or d>=start) and (not end or d<=end)]
    cash=float(capital);pos={};trades=[];equity=[];prev=None
    for i,day in enumerate(days):
        for s2 in list(pos):
            p=pos[s2];bar=by_day[s2].get(day)
            if not bar:continue
            px=None;reason=None;lvl=p['entry_px']*(1-stop)
            if bar['open']<=lvl:px,reason=bar['open'],'stop(gap)'
            elif bar['low']<=lvl:px,reason=lvl,'stop'
            elif p['ma_exit']:px,reason=bar['open'],'trend'
            if px:
                v=p['qty']*px*(1-SLIP);fee=charges(round(v,2),'SELL',True)['total'];cash+=v-fee
                trades.append(dict(symbol=s2,entry=p['entry_day'],exit=day,pnl=v-fee-p['cost'],ret=(v-fee)/p['cost']-1,reason=reason,days=i-p['i']));del pos[s2];continue
            f=feats.get(s2,{}).get(day)
            if f and bar['close']<f['dma200']:p['ma_exit']=True
        if prev and (gate is None or gate.get(prev)):
            cands=sorted(((f['rank'],s2,f) for s2,fd in feats.items() for f in [fd.get(prev)] if f and s2 not in pos and f['quality'] and f['close']>f['dma200'] and f['rank']>0 and f['turnover20']>=1e7 and by_day[s2].get(day)),reverse=True)
            for _,s2,f in cands:
                if len(pos)>=max_positions:break
                bar=by_day[s2][day];price=bar['open']*(1+SLIP);qty=int(min(cash,size)//price)
                if qty<1:continue
                v=qty*price;fee=charges(round(v,2),'BUY')['total']
                if v+fee>cash:continue
                cash-=v+fee;pos[s2]=dict(qty=qty,entry_px=price,cost=v+fee,ma_exit=False,entry_day=day,i=i)
        equity.append((day,cash+sum(p['qty']*by_day[s2].get(day,{}).get('close',p['entry_px']) for s2,p in pos.items())));prev=day
    return trades,equity,pos

def load_fundamentals(path):
    db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row
    return {r['symbol']:json.loads(r['details']) for r in db.execute('SELECT symbol,details FROM fundamentals')}

def filing_date(details,lag_days=75):
    """Conservative public date for the stored annual figures: period end plus 75 days (SEBI allows 60)."""
    try:period=datetime.strptime(details['latest']['period'],'%b %Y')
    except (KeyError,ValueError,TypeError):return None
    import calendar
    end=period.replace(day=calendar.monthrange(period.year,period.month)[1])
    return (end+__import__('datetime').timedelta(days=lag_days)).date().isoformat()

def run_investor(bars,feats,by_day,funds,capital,gate=None,size=10000,max_positions=10,stop=.10,start=None,end=None):
    """Investor rules with the live entry_signal. Annual figures are used only after period end + 75 days, so
    only the latest filings are available: the test window is the months after those filings were public."""
    from marketdesk.autotrading import entry_signal
    avail={s2:filing_date(d) for s2,d in funds.items()}
    days=sorted({d for s2 in by_day for d in by_day[s2]});days=[d for d in days if (not start or d>=start) and (not end or d<=end)]
    cash=float(capital);pos={};trades=[];equity=[];prev=None
    for i,day in enumerate(days):
        for s2 in list(pos):
            p=pos[s2];bar=by_day[s2].get(day)
            if not bar:continue
            px=None;reason=None;lvl=p['entry_px']*(1-stop)
            if bar['open']<=lvl:px,reason=bar['open'],'stop(gap)'
            elif bar['low']<=lvl:px,reason=lvl,'stop'
            elif p['ma_exit']:px,reason=bar['open'],'trend'
            if px:
                v=p['qty']*px*(1-SLIP);fee=charges(round(v,2),'SELL',True)['total'];cash+=v-fee
                trades.append(dict(symbol=s2,entry=p['entry_day'],exit=day,pnl=v-fee-p['cost'],ret=(v-fee)/p['cost']-1,reason=reason,days=i-p['i']));del pos[s2];continue
            f=feats.get(s2,{}).get(day)
            if f and bar['close']<f['dma200']:p['ma_exit']=True
        if prev and (gate is None or gate.get(prev)):
            clock=datetime.fromisoformat(day+'T09:20:00+05:30');cands=[]
            for s2,fd in feats.items():
                f=fd.get(prev)
                if not f or s2 in pos or not by_day[s2].get(day) or not avail.get(s2) or avail[s2]>prev:continue
                sig=dict(as_of=prev+'T00:00:00+05:30',close=f['close'],dma50=f['dma50'],dma200=f['dma200'],high20=f['high20'],volume_ratio=f['volume_ratio'],turnover20=f['turnover20'],fundamentals=funds[s2])
                r=entry_signal('investor',sig,by_day[s2][day]['open'],clock)
                if r:cands.append((r[1],s2))
            for _,s2 in sorted(cands,reverse=True):
                if len(pos)>=max_positions:break
                bar=by_day[s2][day];price=bar['open']*(1+SLIP);qty=int(min(cash,size)//price)
                if qty<1:continue
                v=qty*price;fee=charges(round(v,2),'BUY')['total']
                if v+fee>cash:continue
                cash-=v+fee;pos[s2]=dict(qty=qty,entry_px=price,cost=v+fee,ma_exit=False,entry_day=day,i=i)
        equity.append((day,cash+sum(p['qty']*by_day[s2].get(day,{}).get('close',p['entry_px']) for s2,p in pos.items())));prev=day
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
    for name,g,kw in (('base',None,{}),('cap2',None,{'sector_cap':2}),('strength',None,{'sector_strength':True}),('cap2+strength',None,{'sector_cap':2,'sector_strength':True}),
                      ('nifty200',gate,{}),('nifty200+cap2',gate,{'sector_cap':2}),('nifty200+strength',gate,{'sector_strength':True}),('nifty200+cap2+strength',gate,{'sector_cap':2,'sector_strength':True})):
        for period,(st,e) in {'full':(None,None),'first60pct':(None,cut),'last40pct':(cut,None)}.items():
            trades,equity,_=run(bars,feats,by_day,100000,.01,.06,start=st or days[0],end=e,gate=g,**kw)
            out['results'].append(dict(variant=name,period=period,capital=100000,**metrics(trades,equity,100000)))
            print(name,period,out['results'][-1],flush=True)
    for name,g in (('investor_trend_proxy',None),('investor_trend_proxy+nifty200',gate)):
        for period,(st,e) in {'full':(None,None),'first60pct':(None,cut),'last40pct':(cut,None)}.items():
            trades,equity,_=run_trend(bars,feats,by_day,100000,gate=g,start=st or days[0],end=e)
            out['results'].append(dict(variant=name,period=period,capital=100000,**metrics(trades,equity,100000)))
            print(name,period,out['results'][-1],flush=True)
    funds=load_fundamentals(args.database);fd=sorted(v for v in (filing_date(d) for d in funds.values()) if v)
    start=fd[int(len(fd)*.5)];window=[d for d in all_days if d>=start]
    ew=lambda a,b:round(100*statistics.mean(by_day[s2][b]['close']/by_day[s2][a]['close']-1 for s2 in by_day if a in by_day[s2] and b in by_day[s2]),2)
    out['investor_window']={'start':start,'end':window[-1],'sessions':len(window),'equal_weight_buy_and_hold_pct':ew(window[0],window[-1])}
    for name,g in (('investor_live_rules',None),('investor_live_rules+nifty200',gate)):
        trades,equity,pos=run_investor(bars,feats,by_day,funds,100000,gate=g,start=start)
        m=metrics(trades,equity,100000);m['open_positions']=len(pos);out['results'].append(dict(variant=name,period='after_filings',capital=100000,**m));print(name,out['results'][-1],flush=True)
    Path(args.output).write_text(json.dumps(out,indent=2))
if __name__=='__main__':main()
