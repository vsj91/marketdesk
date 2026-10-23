#!/usr/bin/env python3
"""Historical simulator for the Sector Relative Strength paper basket.

Usage: python3 scripts/backtest_sector_rotation.py [--years 2022 2023 ...] [--capital 100000]
Aggregates the local 5-minute candle archive to daily bars. Sector indices and point-in-time
membership are absent, so sector and Nifty 500 returns are equal-weight covered-constituent
proxies using today's stored sector labels. Delivery fees include sell DP charges. The specified
0.1% round-trip cap is enforced and will reject allocations too small to meet the fee floor.
"""
import argparse,collections,json,math,statistics,sys,re
from datetime import date
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.execution import charges
from marketdesk.signal_baskets import sector_rules
from marketdesk.sectors import sector_group
from marketdesk.storage import connect

CACHE=ROOT/'data/backtests/ema5';DB=ROOT/'data/marketdesk.sqlite3'
DEFAULT_CAPITAL=100_000.

def load(year):
    """Deduplicate overlapping 5-minute cache files, then aggregate to daily OHLCV."""
    grouped=collections.defaultdict(list)
    for f in sorted(CACHE.glob(f'*_{year}-*.json')):
        symbol=f.name.split('_'+str(year)+'-',1)[0]
        match=re.search(r'_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$',f.name)
        span=(date.fromisoformat(match.group(2))-date.fromisoformat(match.group(1))).days if match else 0
        grouped[symbol].append((span,f))
    out={}
    for symbol,files in grouped.items():
        # Full-year cache files supersede overlapping refresh slices for the same symbol.
        f=max(files,key=lambda x:(x[0],x[1].name))[1]
        try:rows=json.loads(f.read_text())
        except (OSError,json.JSONDecodeError):continue
        daily={}
        for b in rows:
            ts=b.get('ts','')
            if ts[:4]!=str(year):continue
            day=ts[:10];bar=daily.get(day)
            if bar is None:daily[day]=dict(ts=day+'T15:25:00+05:30',open=b['open'],high=b['high'],low=b['low'],close=b['close'],volume=b.get('volume') or 0)
            else:bar.update(high=max(bar['high'],b['high']),low=min(bar['low'],b['low']),close=b['close'],volume=bar['volume']+(b.get('volume') or 0))
        out[symbol]=[daily[d] for d in sorted(daily)]
    return out

def full_history(years):
    """Load through the first requested year so the first rank has prior lookback observations."""
    data=collections.defaultdict(dict)
    for year in years:
        for symbol,bars in load(year).items():
            data[symbol].update({b['ts'][:10]:b for b in bars})
    return {s:[v[d] for d in sorted(v)] for s,v in data.items()}

def sector_map(symbols):
    if not DB.exists():return {s:'Unknown' for s in symbols}
    marks=','.join('?' for _ in symbols)
    with connect(DB) as db:
        rows=db.execute(f'SELECT symbol,sector FROM instruments WHERE symbol IN ({marks})',symbols).fetchall()
    mapped={r['symbol']:sector_group(r['sector'],symbol=r['symbol']) for r in rows}
    return {s:mapped.get(s,'Unknown') for s in symbols}

def max_drawdown(curve,initial):
    peak=initial;worst=0.
    for value in curve:
        peak=max(peak,value);worst=max(worst,(peak-value)/peak*100)
    return worst

def simulate(data,years,capital=DEFAULT_CAPITAL):
    p=dict(sector_rules.DEFAULTS);p['lookback_sessions']=20
    symbols=sorted(data);sectors=sector_map(symbols)
    daily_by={s:{b['ts'][:10]:b for b in bars} for s,bars in data.items()}
    days=sorted({d for series in daily_by.values() for d in series})
    benchmark=[]
    # Equal-weight broad-market proxy: use stocks with an available close on each day.
    for d in days:
        closes=[series[d]['close'] for series in daily_by.values() if d in series]
        if closes:benchmark.append(dict(day=d,close=sum(closes)/len(closes)))
    benchmark_by_day={r['day'] for r in benchmark}
    cash=capital;positions={};fees=0.;closed=[];monthly=collections.defaultdict(float);curve=[]
    blocked_fee=0;daily_circuit_days=0;last_week=None;year_results={}
    for ix,d in enumerate(days):
        day=date.fromisoformat(d);week=sector_rules.weekly_key(d)
        todays={s:daily_by[s][d] for s in symbols if d in daily_by[s]}
        if not todays:continue
        opening_equity=cash+sum(st['qty']*daily_by[s].get(days[ix-1],{}).get('close',st['entry']) for s,st in positions.items()) if ix else cash
        prior=[days[ix-1]] if ix and days[ix-1] in benchmark_by_day else []
        ranked=None
        # Review at the first archived trading session of each ISO week, using only prior closes.
        if week!=last_week and prior:
            cutoff=prior[-1];bmark=[r for r in benchmark if r['day']<=cutoff]
            history={s:[b for b in data[s] if b['ts'][:10]<=cutoff] for s in symbols}
            try:ranked=sector_rules.rank_data(history,sectors,bmark,p,[cutoff])
            except ValueError:ranked=None
            if ranked:
                leaders={x['sector'] for x in ranked['sectors']}
                # Rotation exits at this session's open; estimated sell charges and DP apply.
                for s,st in list(positions.items()):
                    if st['sector'] not in leaders and s in todays:
                        bar=todays[s];price=bar['open'];value=price*st['qty'];fee=charges(value,'SELL',True)['total']
                        cash+=value-fee;fees+=fee;monthly[d[:7]]+=fee
                        pnl=value-fee-st['basis'];closed.append(dict(symbol=s,day=d,pnl=pnl,reason='sector rotation'))
                        del positions[s]
                # Weekly trailing stop uses only data completed by the review cutoff.
                stats={x['symbol']:x for x in ranked['stocks']}
                for s,st in positions.items():
                    if st['sector'] in leaders:
                        metric=stats.get(s,{})
                        st['peak']=max(st['peak'],metric.get('close') or st['peak'])
                        st['weekly_atr']=metric.get('weekly_atr') or st.get('weekly_atr')
                        st['stop']=sector_rules.stop(st['entry'],st['peak'],st.get('weekly_atr'),p)
                last_week=week
        # Stops are tested against daily lows; if a stop and target were both possible, this strategy has no target.
        for s,st in list(positions.items()):
            bar=todays.get(s)
            if bar and bar['low']<=st['stop']:
                price=min(bar['open'],st['stop']);value=price*st['qty'];fee=charges(value,'SELL',True)['total']
                cash+=value-fee;fees+=fee;monthly[d[:7]]+=fee
                closed.append(dict(symbol=s,day=d,pnl=value-fee-st['basis'],reason='trailing stop'));del positions[s]
        # Weekly buys occur at market open, up to ten names with at most 10% of starting capital each.
        if ranked and not positions.get('__never__'):
            equity=cash+sum(st['qty']*todays.get(s,{}).get('open',st['entry']) for s,st in positions.items())
            blocked_today=equity<=opening_equity*(1-p['max_daily_loss_pct']/100)
            if blocked_today:daily_circuit_days+=1
            if not blocked_today:
                held=set(positions);leaders={x['sector'] for x in ranked['sectors']}
                # Exit any surviving nonleader at open (e.g. missing price on the prior loop).
                for s,st in list(positions.items()):
                    if st['sector'] not in leaders and s in todays:
                        value=todays[s]['open']*st['qty'];fee=charges(value,'SELL',True)['total'];cash+=value-fee;fees+=fee;monthly[d[:7]]+=fee
                        closed.append(dict(symbol=s,day=d,pnl=value-fee-st['basis'],reason='sector rotation'));del positions[s]
                held=set(positions)
                for pick in ranked['picks']:
                    s=pick['symbol'];bar=todays.get(s)
                    if not bar or s in held or len(positions)>=p['max_positions']:continue
                    price=bar['open']*1.0005;budget=min(cash,p['max_position_pct']/100*capital)
                    qty=math.floor(budget/price)
                    if qty<1:continue
                    value=price*qty;buy_fee=charges(value,'BUY')['total'];sell_fee=charges(value,'SELL',True)['total']
                    if (buy_fee+sell_fee)>value*p['max_round_trip_fee_pct']/100 or monthly[d[:7]]+buy_fee+sell_fee>p['monthly_fee_budget']:
                        blocked_fee+=1;continue
                    if value+buy_fee>cash:continue
                    cash-=value+buy_fee;fees+=buy_fee;monthly[d[:7]]+=buy_fee
                    stop=price*(1-p['stop_loss_pct']/100)
                    positions[s]=dict(qty=qty,entry=price,basis=value+buy_fee,sector=pick['sector'],stop=stop,peak=price,weekly_atr=pick.get('weekly_atr'))
                    held.add(s)
        mark=cash+sum(st['qty']*todays.get(s,{}).get('close',st['entry']) for s,st in positions.items())
        curve.append(mark)
        if d[:4] in {str(y) for y in years}:
            year_results.setdefault(d[:4],[]).append(mark)
    # Mark remaining holdings at final available close with delivery sale fee for realized summary/equity.
    for s,st in list(positions.items()):
        bar=data[s][-1];value=bar['close']*st['qty'];fee=charges(value,'SELL',True)['total']
        closed.append(dict(symbol=s,day=bar['ts'][:10],pnl=value-fee-st['basis'],reason='period end'))
    # Unclosed liquidation costs affect ending equity/CAGR.
    ending=cash+sum(data[s][-1]['close']*st['qty']-charges(data[s][-1]['close']*st['qty'],'SELL',True)['total'] for s,st in positions.items())
    return dict(days=days,curve=curve,closed=closed,fees=fees,ending=ending,blocked_fee=blocked_fee,
        daily_circuit_days=daily_circuit_days,year_results=year_results,universe=len(data),trade_count=len(closed))

def report(years,capital):
    data=full_history(years);result=simulate(data,years,capital);metrics=[]
    initial=capital
    for year in years:
        ds=[d for d in result['days'] if d.startswith(str(year))]
        vals=[result['curve'][result['days'].index(d)] for d in ds]
        if not ds:continue
        # Annualized over observed sessions; partial 2026 is explicitly just its available segment.
        start=initial if year==years[0] else (result['curve'][result['days'].index(ds[0])-1] if result['days'].index(ds[0]) else initial)
        end=vals[-1];ret=(end/start-1)*100 if start else 0.
        trades=[x for x in result['closed'] if x['day'].startswith(str(year))]
        wins=[x['pnl'] for x in trades if x['pnl']>0]
        metrics.append(dict(year=year,sessions=len(ds),return_pct=round(ret,2),cagr_pct=round(((end/start)**(252/len(ds))-1)*100,2) if start>0 and end>0 else None,
            trades=len(trades),win_rate_pct=round(100*len(wins)/len(trades),2) if trades else None,
            expectancy_inr=round(sum(x['pnl'] for x in trades)/len(trades),2) if trades else None,
            worst_intra_year_drawdown_pct=round(max_drawdown([start]+vals,start),2)))
    tr=result['closed'];wins=[x['pnl'] for x in tr if x['pnl']>0]
    sessions=len(result['days']);net=result['ending']/capital-1
    print(json.dumps(dict(strategy='sector-relative-strength/v1',years=metrics,
        overall=dict(sessions=sessions,universe_symbols=result['universe'],trades=result['trade_count'],
            win_rate_pct=round(100*len(wins)/len(tr),2) if tr else None,
            expectancy_inr=round(sum(x['pnl'] for x in tr)/len(tr),2) if tr else None,
            cagr_pct=round(((result['ending']/capital)**(252/max(sessions,1))-1)*100,2) if result['ending']>0 else -100.,
            worst_intra_year_drawdown_pct=max((x['worst_intra_year_drawdown_pct'] for x in metrics),default=0),
            fees_inr=round(result['fees'],2),ending_equity=round(result['ending'],2),
            fee_floor_blocks=result['blocked_fee'],daily_circuit_days=result['daily_circuit_days']),
        limitations=['No official sector index histories or point-in-time membership; equal-weight constituent proxies and today’s classifications used.',
            'The local 5-minute archive is partial, especially before 2025; delivery depth is unavailable.',
            'At ₹1,00,000 capital and a 10% per-stock limit, the required 0.1% round-trip fee cap may block all orders because delivery brokerage and DP fees have fixed floors.']),sort_keys=True))

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--years',nargs='*',type=int,default=[2022,2023,2024,2025,2026]);ap.add_argument('--capital',type=float,default=DEFAULT_CAPITAL);args=ap.parse_args()
    report(args.years,args.capital)

if __name__=='__main__':main()
