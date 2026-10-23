#!/usr/bin/env python3
"""Shared candle-only simulator for the three new paper intraday baskets.

Historical files are the local data/backtests/ema5 cache. They cover only a partial universe (40
symbols/year in 2022-24; more, but incomplete, later) and contain no historical order book. The
order-flow runner therefore uses a disclosed candle-location/volume proxy for depth confirmation.
The simulation is paper-only and includes Upstox intraday charge estimates and adverse OHLC order.
"""
import argparse,collections,json,math,statistics,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.execution import intraday_charges
from marketdesk.signal_baskets import intraday_rules as R
from marketdesk.signal_baskets import ema_rules
from marketdesk.signal_baskets.indicators import ema_series
from marketdesk.signal_baskets.names import LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION,ORB_VOLUME_BREAKOUT
FOLDER=ROOT/'data/backtests/ema5';CAPITAL=100_000.
NAMES={LIQUIDITY_SWEEP:'liquidity sweep reversal',ORDER_FLOW_REVERSAL:'order-flow reversal (candle proxy)',VOLATILITY_COMPRESSION:'volatility compression breakout',ORB_VOLUME_BREAKOUT:'ORB volume breakout'}

def load(year):
    data={}
    for f in sorted(FOLDER.glob(f'*_{year}-*.json')):
        try:rows=json.loads(f.read_text())
        except (OSError,json.JSONDecodeError):continue
        symbol=f.name.split('_'+str(year)+'-',1)[0]
        data.setdefault(symbol,{}).update({b['ts']:b for b in rows if b.get('ts','')[:4]==str(year)})
    return {s:sorted(rows.values(),key=lambda b:b['ts']) for s,rows in data.items()}

def depth_proxy(bars,i):
    """An explicitly synthetic pressure proxy; cannot be interpreted as bid/ask order-book depth."""
    b=bars[i];span=max(1e-9,b['high']-b['low']);buy=max(0.,min(1.,(b['close']-b['low'])/span))
    sell=max(0.,1-buy);return min(10.,(buy+.01)/(sell+.01))

def metric(closed,daily,fees):
    wins=[x['pnl'] for x in closed if x['pnl']>0];losses=[x['pnl'] for x in closed if x['pnl']<=0]
    peak=CAPITAL;dd=0.
    for e in daily:
        peak=max(peak,e);dd=max(dd,(peak-e)/peak*100)
    end=daily[-1] if daily else CAPITAL;years=max(1,len(daily)/252)
    return dict(trades=len(closed),win_rate_pct=round(100*len(wins)/len(closed),2) if closed else None,
        expectancy_inr=round(sum(x['pnl'] for x in closed)/len(closed),2) if closed else None,
        cagr_pct=round(((end/CAPITAL)**(252/max(1,len(daily)))-1)*100,2) if daily and end>0 else None,
        net_return_pct=round((end/CAPITAL-1)*100,2),worst_intra_year_drawdown_pct=round(dd,2),fees_inr=round(fees,2),
        ending_equity=round(end,2),wins=len(wins),losses=len(losses),mean_win=round(statistics.mean(wins),2) if wins else None,
        mean_loss=round(statistics.mean(losses),2) if losses else None)

def run(name,year,params=None):
    p=R.merged(name,params);data=load(year);days=sorted({b['ts'][:10] for rows in data.values() for b in rows})
    by_symbol={s:collections.defaultdict(list) for s in data}
    by_time={}
    for s,rows in data.items():
        for i,b in enumerate(rows):by_symbol[s][b['ts'][:10]].append((i,b))
    closes={s:{} for s in data}
    for s,rows in data.items():
        for b in rows:closes[s][b['ts'][:10]]=b['close']
    daily_ema={}
    if name==ORDER_FLOW_REVERSAL:
        for s,vals in closes.items():
            ds=sorted(vals);ser=ema_series([vals[d] for d in ds],50)
            daily_ema[s]={ds[i]:ser[i-1] if i else None for i in range(len(ds))}
    cash=CAPITAL;fees=0.;closed=[];daily=[];monthly=collections.defaultdict(float);start_equity=cash
    for d in days:
        start_equity=cash;traded=False;trade=None;day_start=cash;marks={}
        timebars=collections.defaultdict(dict)
        for s,group in by_symbol.items():
            for i,b in group.get(d,[]):timebars[b['ts'][11:16]][s]=(i,b)
        times=sorted(timebars)
        # Inspect each shared completed-candle boundary. Signals enter at the next candle's open.
        for t in times:
            candidates=[]
            for s,(i,bar) in timebars[t].items():
                today=by_symbol[s].get(d,[]);ix=next(j for j,(idx,_) in enumerate(today) if idx==i);marks[s]=bar['close']
                if trade and trade['symbol']==s:
                    st=trade;exited=None
                    if t>=p['square_off']:
                        exited=('CLOCK_EXIT',bar['open']*(1-p['slippage_bps']/1e4))
                    else:
                        for px in (bar['open'],bar['low'],bar['high'],bar['close']):
                            if px<=st['stop']:
                                exited=('STOP_LOSS',min(px,st['stop'])*(1-p['slippage_bps']/1e4));break
                            if px>=st['target']:
                                exited=('TARGET',st['target']*(1-p['slippage_bps']/1e4));break
                        if exited is None and bar['high']>=st['be_trigger'] and st['stop']<st['entry']:
                            st['stop']=max(st['stop'],st['entry'])
                        if exited is None and st['trail_atr_mult'] and bar['high']>=st['be_trigger']:
                            atr=R.atr_series(data[s][max(0,i-120):i+1],p['atr_period'])[-1]
                            if atr:st['stop']=max(st['stop'],st['best']-st['trail_atr_mult']*atr)
                        st['best']=max(st['best'],bar['high'])
                    if exited:
                        kind,px=exited;q=st['qty'];fee=intraday_charges(px*q,'SELL')['total'];fees+=fee;monthly[d[:7]]+=fee;cash+=px*q-fee
                        pnl=(px-st['entry'])*q-st['buy_fee']-fee;closed.append(dict(symbol=s,day=d,kind=kind,pnl=pnl));trade=None
                if trade or traded or t<p['entry_start'] or t>=p['entry_cutoff'] or len(today)<=ix+1:continue
                eq=cash
                if eq<=day_start*(1-p['max_daily_loss_pct']/100):continue
                if monthly[d[:7]]>=p['monthly_fee_budget']:continue
                prefix=data[s][max(0,i-120):i+1];daily_ema_value=(daily_ema.get(s) or {}).get(d)
                proxy=depth_proxy(prefix,len(prefix)-1) if name==ORDER_FLOW_REVERSAL else None
                # Cheap necessary-condition gates avoid computing ATR/Bollinger histories for routine bars.
                session=[x for x in prefix if x['ts'][:10]==d];bar=prefix[-1]
                if name==LIQUIDITY_SWEEP:
                    if len(session)<3:continue
                    levels=(min(x['low'] for x in session[:3]),min((x['low'] for x in prefix[-31:-1]),default=bar['low']))
                    prior=prefix[-2] if len(prefix)>1 else bar
                    if not any((bar['low']<lv and bar['close']>lv) or (prior['low']<lv and bar['close']>lv) for lv in levels):continue
                    if (R._rvol(prefix,len(prefix)-1) or 0)<p['min_rvol']:continue
                elif name==ORDER_FLOW_REVERSAL:
                    if daily_ema_value is None or bar['close']<=daily_ema_value:continue
                    vw=R._vwap(session)
                    if not vw or bar['close']<vw or abs(bar['close']/vw-1)*100>p['pullback_vwap_pct'] or (proxy or 0)<p['max_depth_ratio']:continue
                elif name==ORB_VOLUME_BREAKOUT:
                    if t<'09:30' or t>='10:00' or len(session)<4:continue
                    opening=session[:3];vw=R._vwap(session)
                    if bar['close']<=max(x['high'] for x in opening) or not vw or bar['close']<=vw:continue
                    if (R._rvol(prefix,len(prefix)-1) or 0)<p['min_rvol']:continue
                else:
                    band=R._bb([x['close'] for x in prefix],p['bb_period'],p['bb_std']);vw=R._vwap(session)
                    if not band or bar['close']<=band[0] or not vw or bar['close']<=vw or (R._rvol(prefix,len(prefix)-1) or 0)<p['min_rvol']:continue
                ev=R.evaluate(name,prefix,p,depth_ratio=proxy,daily_ema50=daily_ema_value)
                if not ev or ev['status']!='BUY_CANDIDATE':continue
                nextbar=today[ix+1][1];fill=nextbar['open']*(1+p['slippage_bps']/1e4)
                if fill>ev['close']*(1+p['max_chase_pct']/100):continue
                candidates.append((ev['score'],s,fill,ev))
            if candidates and not trade and not traded:
                _,s,fill,ev=max(candidates,key=lambda x:x[0]);eq=cash
                sizing_cost=lambda value:intraday_charges(value,'BUY')['total']+intraday_charges(value,'SELL')['total']
                qty=ema_rules.size(eq,cash,fill,ev['initial_stop'],p,0.,sizing_cost)['quantity']
                if qty<1:continue
                buy_fee=intraday_charges(fill*qty,'BUY')['total'];roundtrip=buy_fee+intraday_charges(fill*qty,'SELL')['total']
                if roundtrip>fill*qty*p['fee_max_pct']/100 or monthly[d[:7]]+roundtrip>p['monthly_fee_budget']:continue
                cash-=fill*qty+buy_fee;fees+=buy_fee;monthly[d[:7]]+=buy_fee
                risk=fill-ev['initial_stop'];trade=dict(symbol=s,entry=fill,qty=qty,stop=ev['initial_stop'],target=fill+p['target_r']*risk,
                    be_trigger=fill+p['breakeven_r']*risk,trail_atr_mult=p['trail_atr_mult'],best=fill,buy_fee=buy_fee)
                traded=True
        # Candle caches sometimes end before 15:10. Mark any residual position out at the last close.
        if trade:
            s=trade['symbol'];bars=by_symbol[s].get(d,[]);px=bars[-1][1]['close'];q=trade['qty'];fee=intraday_charges(px*q,'SELL')['total'];fees+=fee;monthly[d[:7]]+=fee;cash+=px*q-fee
            closed.append(dict(symbol=s,day=d,kind='EOD_DATA_END',pnl=(px-trade['entry'])*q-trade['buy_fee']-fee));trade=None
        daily.append(cash)
    return dict(year=year,strategy=NAMES[name],universe_symbols=len(data),data_days=len(days),metrics=metric(closed,daily,fees),monthly_fee_cap=p['monthly_fee_budget'],
        order_flow_depth='not historically available; candle-location/volume proxy used' if name==ORDER_FLOW_REVERSAL else None)

def main(name):
    ap=argparse.ArgumentParser();ap.add_argument('--years',nargs='*',default=['2022','2023','2024','2025','2026']);ap.add_argument('--params',default='{}');a=ap.parse_args();cfg=json.loads(a.params)
    outputs=[]
    for y in a.years:
        result=run(name,y,cfg);outputs.append(result);print(json.dumps(result,sort_keys=True),flush=True)
    returns=[1+x['metrics']['net_return_pct']/100 for x in outputs];compound=math.prod(returns)-1
    period_years=max(1.,sum(x['data_days'] for x in outputs)/252);print(json.dumps(dict(strategy=NAMES[name],period_years=round(period_years,2),compounded_return_pct=round(compound*100,2),
        cagr_pct=round(((1+compound)**(1/period_years)-1)*100,2) if compound>-1 else -100.,
        trades=sum(x['metrics']['trades'] for x in outputs),note='Local archive is incomplete and not a point-in-time Nifty 500; order-book depth is absent from historical candles.'),sort_keys=True))

if __name__=='__main__':main(sys.argv[1] if len(sys.argv)>1 else LIQUIDITY_SWEEP)
