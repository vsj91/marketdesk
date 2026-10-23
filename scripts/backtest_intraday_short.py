#!/usr/bin/env python3
"""Backtest of an intraday Opening Range Breakdown Short for falling markets, on the cached Upstox 5-minute candles
(data/backtests/ema5, 2022-2026; 40 symbols a year in 2022-24, about 200 in 2025-26, not a point-in-time Nifty 500).

Rules and pass criteria were fixed before the first run and are not tuned on these results.
- Falling market: the Nifty 500's previous daily close is below its 50-day average.
- Weak stock: the stock's previous daily close is below its own 20-day average.
- Signal, 09:30-11:00 IST: a completed 5-minute candle closes below the 09:15-09:30 opening-range low and below session
  VWAP on at least 2x the prior 20-candle average volume. Strongest volume surge first; one trade a day.
- Entry: short at the next candle's open less slippage; skipped if that open is more than 0.3% below the signal close.
- Stop: the opening-range high, kept between 0.6% and 1.5% above entry. Target 2R; stop to breakeven at 1R.
  Within a candle the stop is checked before the target (adverse order). Square-off at 15:10.
- Risk: 1% of equity at risk per trade, position at most 95% of equity (cash only, no leverage), 2% daily loss limit,
  Upstox intraday charges on both legs (STT on the opening sell).
Pass: CAGR of at least 6.5% after fees, profitable in 2022-24 and in 2025-26, profit factor of at least 1.2, still
profitable with double slippage, and at least 50 trades. Paper only."""
import argparse,collections,json,math,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.execution import intraday_charges
FOLDER=ROOT/'data/backtests/ema5';INDEX=ROOT/'data/backtests/daily-long/_NIFTY500.json';OUT=ROOT/'data/backtests/intraday-short.json'
CAPITAL=100_000.;YEARS=('2022','2023','2024','2025','2026')
P=dict(index_ma=50,stock_ma=20,entry_start='09:30',entry_cutoff='11:00',min_rvol=2.,max_chase_pct=.3,min_stop_pct=.6,max_stop_pct=1.5,
    target_r=2.,breakeven_r=1.,square_off='15:10',risk_pct=1.,max_position_pct=95.,max_daily_loss_pct=2.,slippage_bps=5.)

def load():
    """{symbol: {day: [bars]}} across all cached years."""
    data=collections.defaultdict(dict)
    for f in sorted(FOLDER.glob('*.json')):
        try:rows=json.loads(f.read_text())
        except (OSError,json.JSONDecodeError):continue
        s=f.name.rsplit('_',2)[0]
        for b in rows:
            if b.get('ts'):data[s].setdefault(b['ts'][:10],{})[b['ts']]=b
    return {s:{d:[v[k] for k in sorted(v)] for d,v in days.items()} for s,days in data.items()}

def falling_days():
    rows=json.loads(INDEX.read_text());c=[r[4] for r in rows];d=[r[0] for r in rows];n=P['index_ma']
    # Day d trades when the close before d sits under the average of the n closes before that.
    return {d[i+1] for i in range(n,len(c)-1) if c[i]<sum(c[i-n+1:i+1])/n}

def weak(days,day):
    """Previous daily close below the stock's 20-day average of daily closes, from its own 5-minute candles."""
    prior=[d for d in sorted(days) if d<day][-P['stock_ma']:]
    if len(prior)<P['stock_ma']:return False
    closes=[days[d][-1]['close'] for d in prior];return closes[-1]<sum(closes)/len(closes)

def vwap(bars):
    v=sum(b['volume'] for b in bars);return sum((b['high']+b['low']+b['close'])/3*b['volume'] for b in bars)/v if v else None

def run(slippage_bps=None):
    slip=(P['slippage_bps'] if slippage_bps is None else slippage_bps)/1e4
    data=load();falling=falling_days()
    days=sorted({d for s in data.values() for d in s if d[:4] in YEARS})
    equity=CAPITAL;curve=[];trades=[];fees=0.;skipped_regime=0
    for day in days:
        if day not in falling:skipped_regime+=1;curve.append((day,equity));continue
        bars={s:v[day] for s,v in data.items() if day in v and len(v[day])>=10 and weak(v,day)}
        start=equity;trade=None;done=False
        times=sorted({b['ts'][11:16] for v in bars.values() for b in v})
        index={s:{b['ts'][11:16]:i for i,b in enumerate(v)} for s,v in bars.items()}
        for t in times:
            if trade:
                s=trade['s'];i=index[s].get(t)
                if i is not None:
                    b=bars[s][i];exit_=None
                    if t>=P['square_off']:exit_=('CLOCK_EXIT',b['open'])
                    else:
                        for px in (b['open'],b['high'],b['low'],b['close']):             # adverse order: stop before target
                            if px>=trade['stop']:exit_=('STOP_LOSS',max(px,trade['stop']));break
                            if px<=trade['target']:exit_=('TARGET',trade['target']);break
                        if exit_ is None and b['low']<=trade['be']:trade['stop']=min(trade['stop'],trade['entry'])
                    if exit_:
                        kind,px=exit_;px*=1+slip;q=trade['q'];fee=intraday_charges(px*q,'BUY')['total'];fees+=fee
                        pnl=(trade['entry']-px)*q-trade['fee']-fee;equity+=pnl;trades.append(dict(s=s,day=day,kind=kind,pnl=round(pnl,2)));trade=None
                continue
            if done or t<P['entry_start'] or t>=P['entry_cutoff'] or equity<=start*(1-P['max_daily_loss_pct']/100):continue
            best=None
            for s,v in bars.items():
                i=index[s].get(t)
                if i is None or i<3 or i+1>=len(v):continue
                b=v[i];opening=v[:3];low=min(x['low'] for x in opening);high=max(x['high'] for x in opening)
                prior=v[max(0,i-20):i];avg=sum(x['volume'] for x in prior)/len(prior) if prior else 0
                vw=vwap(v[:i+1])
                if not(b['close']<low and vw and b['close']<vw and avg>0 and b['volume']>=P['min_rvol']*avg):continue
                rvol=b['volume']/avg
                if best is None or rvol>best[0]:best=(rvol,s,i,high)
            if best is None:continue
            _,s,i,high=best;sig=bars[s][i];fill=bars[s][i+1]['open']*(1-slip)
            if fill<sig['close']*(1-P['max_chase_pct']/100):done=True;continue
            stop=min(max(high,fill*(1+P['min_stop_pct']/100)),fill*(1+P['max_stop_pct']/100));risk=stop-fill
            q=int(min(equity*P['risk_pct']/100/risk,equity*P['max_position_pct']/100/fill))
            if q<1:done=True;continue
            fee=intraday_charges(fill*q,'SELL')['total'];fees+=fee
            trade=dict(s=s,entry=fill,q=q,stop=stop,target=fill-P['target_r']*risk,be=fill-P['breakeven_r']*risk,fee=fee);done=True
        if trade:                                                                     # data ended before 15:10
            s=trade['s'];px=bars[s][-1]['close']*(1+slip);q=trade['q'];fee=intraday_charges(px*q,'BUY')['total'];fees+=fee
            pnl=(trade['entry']-px)*q-trade['fee']-fee;equity+=pnl;trades.append(dict(s=s,day=day,kind='DATA_END',pnl=round(pnl,2)))
        curve.append((day,equity))
    return summarize(curve,trades,fees,len(days),skipped_regime)

def summarize(curve,trades,fees,days,skipped):
    def part(a,b):
        sel=[t['pnl'] for t in trades if a<=t['day'][:4]<=b];return dict(trades=len(sel),pnl=round(sum(sel)))
    peak=CAPITAL;dd=0.
    for _,e in curve:peak=max(peak,e);dd=max(dd,(peak-e)/peak)
    end=curve[-1][1] if curve else CAPITAL;gain=sum(t['pnl'] for t in trades if t['pnl']>0);loss=-sum(t['pnl'] for t in trades if t['pnl']<=0)
    years={}
    for d,e in curve:years.setdefault(d[:4],[e,e])[1]=e
    prev=CAPITAL;by_year={}
    for y in sorted(years):by_year[y]=round((years[y][1]/prev-1)*100,2);prev=years[y][1]
    return dict(window=[curve[0][0],curve[-1][0]] if curve else None,data_days=days,falling_market_days=days-skipped,trades=len(trades),
        win_rate_pct=round(100*sum(t['pnl']>0 for t in trades)/len(trades),1) if trades else None,
        profit_factor=round(gain/loss,2) if loss else None,net_return_pct=round((end/CAPITAL-1)*100,2),
        cagr_pct=round(((end/CAPITAL)**(252/max(1,days))-1)*100,2) if end>0 else -100.,max_drawdown_pct=round(dd*100,2),fees_inr=round(fees),
        years=by_year,h1=part('2022','2024'),h2=part('2025','2026'),exits=dict(collections.Counter(t['kind'] for t in trades)))

def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    base=run();double=run(P['slippage_bps']*2)
    checks=dict(beats_tbills=base['cagr_pct']>=6.5,both_halves=base['h1']['pnl']>0 and base['h2']['pnl']>0,
        profit_factor=(base['profit_factor'] or 0)>=1.2,slip2x=double['net_return_pct']>0,enough_trades=base['trades']>=50)
    out=dict(strategy='Opening Range Breakdown Short (falling markets)',rules=P,base=base,slip2x=double,checks=checks,
        failed=[k for k,v in checks.items() if not v],passed=all(checks.values()),
        note='Partial, survivorship-biased universe (40 symbols a year in 2022-24, ~200 in 2025-26); candle-only fills, no historical order book.')
    OUT.write_text(json.dumps(out,indent=1));print(json.dumps(out,indent=1))

if __name__=='__main__':main()
