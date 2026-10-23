"""Weekly broad-sector relative-strength rotation, using the verified local Nifty 500 members.

The local archive contains stock candles and a Nifty 500 benchmark, but no Nifty sector-index
history. Sector return is therefore the equal-weight return of covered constituents in that
sector, not a quoted sector-index return; this approximation is shown in the basket disclosures.
"""
from collections import defaultdict
from datetime import timedelta
from .indicators import atr_series
from ..sectors import sector_group

DEFAULTS=dict(lookback_sessions=20,top_sectors=2,stocks_per_sector=5,max_positions=10,
    max_position_pct=10.,min_turnover_inr=20_000_000.,max_daily_loss_pct=2.,monthly_fee_budget=500.,
    max_round_trip_fee_pct=.1,stop_loss_pct=8.,weekly_atr_mult=2.,)
LIMITS={'lookback_sessions':(15,40),'top_sectors':(2,2),'stocks_per_sector':(5,5),'max_positions':(10,10),
    'max_position_pct':(5.,10.),'min_turnover_inr':(20_000_000.,100_000_000.),'max_daily_loss_pct':(.5,2.),
    'monthly_fee_budget':(0.,5000.),'max_round_trip_fee_pct':(.05,.1),'stop_loss_pct':(5.,12.),'weekly_atr_mult':(1.,4.)}

def weekly_key(day):
    d=__import__('datetime').date.fromisoformat(day)
    y,w,_=d.isocalendar();return f'{y}-W{w:02d}'

def _weekly(rows):
    weeks=defaultdict(list)
    for r in rows:weeks[weekly_key(r['ts'][:10])].append(r)
    out=[]
    for w,bars in sorted(weeks.items()):
        out.append(dict(ts=bars[-1]['ts'],open=bars[0]['open'],high=max(x['high'] for x in bars),
            low=min(x['low'] for x in bars),close=bars[-1]['close'],volume=sum(x['volume'] or 0 for x in bars)))
    return out

def rank(db,clock,params,allowed):
    """Return the top two relative-strength sectors and at most five liquid momentum names each."""
    today=clock.astimezone(__import__('datetime').timezone(__import__('datetime').timedelta(hours=5,minutes=30))).date().isoformat()
    history_sessions=max(params['lookback_sessions']+1,71)
    bench=db.execute('SELECT day,close FROM claude_benchmark WHERE day<? ORDER BY day DESC LIMIT ?',
        (today,history_sessions)).fetchall()
    bench=list(reversed(bench))
    need=params['lookback_sessions']+1
    if len(bench)<need:raise ValueError('Not enough completed Nifty 500 benchmark sessions for a 4-week sector review.')
    days=[r['day'] for r in bench]
    if not allowed:raise ValueError('Verified Nifty 500 membership is empty; rebalance waits.')
    syms=sorted(allowed);marks=','.join('?' for _ in syms)
    daymarks=','.join('?' for _ in days)
    sql=f'''WITH ranked AS (
        SELECT c.symbol,c.ts,c.open,c.high,c.low,c.close,c.volume,
          ROW_NUMBER() OVER(PARTITION BY c.symbol ORDER BY c.ts DESC) rn
        FROM candles c WHERE c.symbol IN ({marks}) AND substr(c.ts,1,10) IN ({daymarks})
      ) SELECT r.* FROM ranked r WHERE r.rn<=? ORDER BY r.symbol,r.ts'''
    rows=db.execute(sql,(*syms,*days,history_sessions)).fetchall();by={s:[] for s in syms}
    for r in rows:by[r['symbol']].append(dict(r))
    sectors={r['symbol']:sector_group(r['sector'],symbol=r['symbol']) for r in db.execute(
        f"SELECT symbol,sector FROM instruments WHERE symbol IN ({marks})",syms)}
    return rank_data(by,sectors,[dict(day=r['day'],close=r['close']) for r in bench],params,days)

def rank_data(by,sectors,benchmark,params,days=None):
    """Pure ranker shared by live selection and the historical rotation simulator."""
    need=params['lookback_sessions']+1
    if days is None:days=sorted({b['ts'][:10] for rows in by.values() for b in rows})
    bench=sorted(benchmark,key=lambda r:r.get('day',r.get('ts','')))
    if len(bench)<need:raise ValueError('Not enough completed benchmark sessions for a four-week sector review.')
    benchmark_return=bench[-1]['close']/bench[-need]['close']-1
    sector_returns=defaultdict(list);stocks=[]
    for symbol,bars in by.items():
        if len(bars)<need:continue
        bars.sort(key=lambda b:b['ts']);recent=bars[-need:]
        if len(recent)<need:continue
        ret=recent[-1]['close']/recent[0]['close']-1
        sector=sectors.get(symbol,'Unknown')
        if sector!='Unknown':sector_returns[sector].append(ret)
        turnover=sum((b['close'] or 0)*(b['volume'] or 0) for b in recent[1:])/params['lookback_sessions']
        weekly=_weekly(bars)
        weekly_atr=atr_series(weekly,14)[-1] if len(weekly)>=15 else None
        stocks.append(dict(symbol=symbol,sector=sector,momentum=ret,avg_turnover=turnover,
            close=recent[-1]['close'],as_of=recent[-1]['ts'][:10],weekly_atr=weekly_atr))
    relative=[dict(sector=s,sector_return=sum(v)/len(v),benchmark_return=benchmark_return,
        excess_return=sum(v)/len(v)-benchmark_return,constituents=len(v))
        for s,v in sector_returns.items() if len(v)>=5]
    leading=sorted((r for r in relative if r['excess_return']>0),key=lambda r:(-r['excess_return'],r['sector']))[:params['top_sectors']]
    chosen=[]
    for sec in leading:
        group=sorted((s for s in stocks if s['sector']==sec['sector'] and s['momentum']>0 and
            s['avg_turnover']>=params['min_turnover_inr']),key=lambda s:(-s['momentum'],-s['avg_turnover'],s['symbol']))
        for x in group[:params['stocks_per_sector']]:chosen.append({**x,'sector_excess_return':sec['excess_return']})
    as_of=days[-1] if days else bench[-1].get('day') or bench[-1].get('ts','')[:10]
    return dict(as_of=as_of,week=weekly_key(as_of),benchmark_return=benchmark_return,
        sectors=leading,sector_ranks=sorted(relative,key=lambda r:-r['excess_return']),picks=chosen,stocks=stocks)

def stop(entry,peak,weekly_atr,params):
    hard=entry*(1-params['stop_loss_pct']/100)
    trailing=peak-params['weekly_atr_mult']*weekly_atr if weekly_atr and weekly_atr>0 else hard
    return max(hard,trailing)
