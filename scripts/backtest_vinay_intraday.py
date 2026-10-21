#!/usr/bin/env python3
"""Backtest of the Vinay Intraday Trader on the cached Upstox 5-minute candles (data/backtests/ema5, 2022-2026).

Calls the live rule functions (signal_baskets.vinay_rules: evaluate, plan, size, fee_check, arm, on_price, on_bar,
apply_fill), so tested and traded rules cannot drift. Paper only and read-only: never touches the database.

Simulation: completed candles only. A candidate from candle t is bought at the open of candle t+1 plus slippage,
only if that open is inside the chase limit and above VWAP. Each later candle is walked open -> low -> high -> close
(the adverse order: a stop touched in the same candle as the target counts as the stop); a stop fills at the worse
of its level and the touching price, a limit at its level. Intraday charges on every leg; ₹1,00,000 starting cash,
compounding; everything is flat by the square-off candle.

  python3 scripts/backtest_vinay_intraday.py [--params '{"min_rvol":1.5}'] [--years 2022 2023] [--save]

--save writes data/backtests/vinay-intraday-2022-2026.json, which research_multi_year.py summary adds to the Backtests page."""
import argparse,json,math,sys
from collections import defaultdict
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.signal_baskets import vinay_rules as V
from marketdesk.execution import intraday_charges
FOLDER=ROOT/'data/backtests/ema5'

def leg(value,side):return intraday_charges(value,side)['total']
def cost(value):return leg(value,'BUY')+leg(value,'SELL')

def load(year):
    data={}
    for f in sorted(FOLDER.glob(f'*_{year}-*.json')):
        bars=json.loads(f.read_text());data.setdefault(f.name.rsplit('_',2)[0],[]).extend(bars)
    out={}
    for s,bars in data.items():
        days=defaultdict(list)
        for b in sorted({b['ts']:b for b in bars}.values(),key=lambda b:b['ts']):days[b['ts'][:10]].append(b)
        out[s]=days
    return out

def quick(prior_close,session,cfg):
    """Cheap pre-check (day change band and a close above the opening range) before the full evaluate."""
    n=cfg['opening_range_minutes']//cfg['timeframe_minutes']
    if len(session)<=n:return False
    c=session[-1]['close'];ch=(c/prior_close-1)*100
    return cfg['min_day_change_pct']<=ch<=cfg['max_day_change_pct'] and c>max(b['high'] for b in session[:n])

def run(year,params=None):
    cfg=V.merged(params);data=load(year);days=sorted({d for s in data.values() for d in s})
    cash=100_000.;closed=[];daily=[];fees=0.;month={}
    for di,d in enumerate(days):
        start_equity=cash;open_={};done=set();losers=0
        hist={}
        for s,by in data.items():
            if d not in by:continue
            before=[x for x in days[max(0,di-cfg['rvol_sessions']):di] if x in by]
            if len(before)<3:continue
            hist[s]=(sum((by[x] for x in before),[]),by[d],by[before[-1]][-1]['close'])
        times=sorted({b['ts'][11:16] for _,today,_ in hist.values() for b in today})
        pos={s:{b['ts'][11:16]:i for i,b in enumerate(today)} for s,(_,today,_) in hist.items()}
        for t in times:
            for s in list(open_):                         # manage open trades on this candle
                st=open_[s];i=pos[s].get(t)
                if i is None:continue
                b=hist[s][1][i];exits=[]
                if t>=cfg['square_off']:exits.append(('CLOCK',b['open'],st['qty']))
                else:
                    for px in (b['open'],b['low'],b['high'],b['close']):
                        intent,_=V.on_price(st,px,cfg)
                        if not intent:continue
                        price=min(px,intent['level']) if intent['order']=='stop' else intent['level'] if intent['order']=='limit' else px
                        if intent['order']=='market':price*=1-cfg['slippage_bps']/1e4
                        exits.append((intent['kind'],price,intent['quantity']));V.apply_fill(st,intent,intent['quantity'],price,cfg)
                        if st['qty']<=0:break
                    if st['qty']>0:V.on_bar(st,hist[s][0]+hist[s][1][:i+1],cfg)
                for kind,price,q in exits:
                    fee=leg(price*q,'SELL');cash+=price*q-fee;fees+=fee;st['pnl']+=(price-st['entry'])*q-fee;month[d[:7]]=month.get(d[:7],0.)+fee
                    if kind=='CLOCK':st['qty']=0
                if st['qty']<=0:
                    closed.append(dict(symbol=s,day=d,pnl=st['pnl'],kind=exits[-1][0]));del open_[s];losers+=st['pnl']<0
            if not cfg['entry_start']<=t<cfg['entry_cutoff'] or len(open_)>=cfg['max_positions']:continue
            equity=cash+sum(st['qty']*hist[s][1][min(pos[s].get(t,0),len(hist[s][1])-1)]['close'] for s,st in open_.items())
            if equity<=start_equity*(1-cfg['max_daily_loss_pct']/100) or losers>=cfg['max_losing_trades']:continue
            if sum(1 for c in closed if c['day']==d)+len(open_)>=cfg['max_entries_per_day']:continue
            cands=[]
            for s,(prior,today,pc) in hist.items():
                i=pos[s].get(t)
                if i is None or s in done or s in open_ or i+1>=len(today) or not quick(pc,today[:i+1],cfg):continue
                ev=V.evaluate(prior+today[:i+1],cfg,pc)
                if ev and ev['status']=='BUY_CANDIDATE':cands.append((ev['score'],s,i,ev))
            for _,s,i,ev in sorted(cands,key=lambda x:-x[0]):
                if len(open_)>=cfg['max_positions']:break
                fill=hist[s][1][i+1]['open']*(1+cfg['slippage_bps']/1e4)
                if fill>ev['close']*(1+cfg['max_chase_pct']/100) or fill<=ev['vwap']:continue
                lv=V.plan(fill,ev['stop_pct'],cfg)
                risk=sum(max(0,st['entry']-st['stop'])*st['qty'] for st in open_.values())
                q=V.size(equity,cash,fill,lv['stop'],cfg,risk,cost)['quantity']
                if q<1 or V.fee_check(fill,q,cost,cfg,month.get(d[:7],0.)):continue
                fee=leg(fill*q,'BUY');cash-=fill*q+fee;fees+=fee;month[d[:7]]=month.get(d[:7],0.)+fee
                st=V.arm(fill,q,ev['stop_pct'],cfg,cost(fill*q)/q,hist[s][1][i+1]['ts']);st['pnl']=-fee
                open_[s]=st;done.add(s)
        for s,st in open_.items():                        # data ended before the square-off candle
            b=hist[s][1][-1];fee=leg(b['close']*st['qty'],'SELL');cash+=b['close']*st['qty']-fee;fees+=fee
            closed.append(dict(symbol=s,day=d,pnl=st['pnl']+(b['close']-st['entry'])*st['qty']-fee,kind='EOD'))
        daily.append(cash)
    return year,dict(closed=closed,daily=daily,fees=fees)

def summary(closed,daily,fees,capital=100_000.):
    wins=[c['pnl'] for c in closed if c['pnl']>0];losses=[c['pnl'] for c in closed if c['pnl']<=0]
    peak=capital;dd=0.
    for e in daily:peak=max(peak,e);dd=max(dd,(1-e/peak)*100)
    kinds=defaultdict(int)
    for c in closed:kinds[c['kind']]+=1
    end=daily[-1] if daily else capital
    return dict(trades=len(closed),win_rate_pct=round(len(wins)/len(closed)*100,1) if closed else None,
        profit_factor=round(sum(wins)/-sum(losses),2) if losses and sum(losses) else None,
        avg_win=round(sum(wins)/len(wins)) if wins else None,avg_loss=round(sum(losses)/len(losses)) if losses else None,
        net_return_pct=round((end/capital-1)*100,2),fees=round(fees),max_drawdown_pct=round(dd,2),
        green_days_pct=round(sum(1 for a,b in zip([capital]+daily,daily) if b>a)/len(daily)*100,1) if daily else None,exits=dict(kinds))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--params',default='{}');ap.add_argument('--years',nargs='*',default=['2022','2023','2024','2025','2026'])
    ap.add_argument('--save',action='store_true')
    a=ap.parse_args();params=json.loads(a.params)
    with Pool(min(5,len(a.years))) as pool:results=dict(pool.starmap(run,[(y,params) for y in a.years]))
    total=[];growth=1.;years={}
    for y in a.years:
        r=results[y];m=summary(r['closed'],r['daily'],r['fees']);growth*=1+m['net_return_pct']/100;total+=r['closed'];years[y]=m
        print(y,json.dumps(m))
    print('compounded',round((growth-1)*100,2),'% over',len(a.years),'years;',len(total),'trades')
    if a.save:
        days=sorted(c['day'] for c in total)
        out=dict(window=[days[0],days[-1]],params=V.merged(params),years=years,compounded_pct=round((growth-1)*100,2),trades=len(total),
            universe='40 most traded stocks each year; 2025-2026 add Nifty 500 ranks 41-200 by turnover')
        (ROOT/'data/backtests/vinay-intraday-2022-2026.json').write_text(json.dumps(out,indent=1));print('saved')

if __name__=='__main__':main()
