"""Signal rules for the additional paper baskets. Pure functions over completed daily candles.

The live engine and the backtest call the same functions, so tested and traded rules cannot drift.
Every input is a completed session; nothing here sees the current session's price except `price` checks."""
import math,statistics
from .history_rules import long_window,effective_rows

MIN_HISTORY=130          # 6-month lookback + 1-month skip + margin
LOOKBACK,SKIP=126,21     # 6-1 momentum: return from ~6 months ago to ~1 month ago
VOL_WINDOW=120
HIGH_WINDOW=252          # 52-week high: about one year of sessions
LONG_LOOKBACK=252        # 12-1 momentum: return from ~12 months ago to ~1 month ago
OVERNIGHT_WINDOW=105     # overnight returns summed over the 6-1 window (~126 to 21 sessions ago)
GAP_PCT,GAP_VOLUME=.04,3.  # earnings-gap proxy: open 4%+ above the prior close on 3x the prior 50-session volume
ALPHA_WINDOW,ALPHA_MIN=250,200  # one-year Jensen alpha against the Nifty 500: 250 daily returns, at least 200 paired
MIN_TURNOVER=10_000_000  # ₹1 crore average daily traded value, same as Trader/Investor
CLENOW_WINDOW,CLENOW_MAX_MOVE,INDEX_TREND=90,.15,200  # Clenow: 90-session regression, no 15% one-day move, Nifty 500 above its 200-day average

BASKETS={
 'momentum':dict(name='Cross-sectional momentum · monthly · paper basket',family='Cross-sectional momentum (6-1 month relative strength)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=25,rebalance='monthly',
   entry='Rank Nifty 500 stocks by return from 126 to 21 completed sessions ago (skipping the latest month). Buy the top 10 that also have a positive 6-1 return and a completed close above the 200-day average. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 25, turn negative on 6-1 momentum or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active.'),
 'lowvol':dict(name='Low volatility · monthly · paper basket',family='Low-volatility anomaly with a trend filter',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=15,keep_rank=30,rebalance='monthly',
   entry='Among Nifty 500 stocks with a completed close above the 200-day average, rank by the standard deviation of the latest 120 daily returns (lowest first). Buy the 10 calmest. Empty slots are refilled from the current 10 calmest.',
   exit='At the first session of each month, sell holdings that leave the 30 calmest or close below the 200-day average. Between rebalances a 15% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active.'),
 'reversion':dict(name='Mean reversion (RSI-2 pullback) · paper basket',family='Short-term mean reversion inside an uptrend',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=8,max_hold=10,rebalance='daily',
   entry='Completed close above the 200-day average and 2-day RSI below 10 (a sharp short dip inside an uptrend). Buy the deepest dips first, next session.',
   exit='Sell the session after a completed close above the 5-day average, after 10 sessions held, or at an 8% loss from average cost.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. Frequent trading makes fees the main risk. New buys pause at a 15% fall from the equity peak; exits stay active.'),
 'high52':dict(name='52-week high · monthly · paper basket',family='52-week-high momentum (George and Hwang)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=30,rebalance='monthly',
   entry='Rank Nifty 500 stocks by how close the completed close is to the highest high of the latest 252 sessions (nearest first). Buy the top 10 that also close above the 200-day average. Stocks with under a year of clean history are not ranked. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 30 or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active.'),
 'momentum12':dict(name='12-1 momentum · monthly · paper basket',family='Cross-sectional momentum (12-1 month relative strength, Jegadeesh and Titman)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=30,rebalance='monthly',
   entry='Rank Nifty 500 stocks by return from 252 to 21 completed sessions ago (about 12 months, skipping the latest month). Buy the top 10 that also have a positive 12-1 return and a completed close above the 200-day average. Stocks with under a year of clean history are not ranked. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 30, turn negative on 12-1 momentum or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active. Expect deep drawdowns: about 50% at worst in the 2018-2026 backtest.'),
 # Three research baskets added for forward paper testing against their 2018-2026 backtests (scripts/research_baskets.py).
 # Each passed the uniqueness rule in scripts/research_multi_year.py (excess-return correlation <= 0.65 and holdings overlap
 # <= 50% against every active rule basket and each other); smooth momentum missed it at 50.2% overlap with 12-1 momentum.
 'overnight':dict(name='Overnight momentum · monthly · paper basket',family='Overnight-return momentum (Lou, Polk and Skouras)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=30,rebalance='monthly',
   entry='Rank Nifty 500 stocks by the sum of their overnight returns (each open against the prior close) from 126 to 21 completed sessions ago. Buy the top 10 with a positive sum and a completed close above the 200-day average. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 30 or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active. Backtest: beat the equal-weight hold overall but not among the 200 most traded stocks, and failed without the trend filter.'),
 'gap_drift':dict(name='Gap-up drift · daily · paper basket',family='Post-announcement drift proxy from price and volume (earnings-gap drift)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=15,max_hold=60,rebalance='daily',
   entry='A completed session that opened at least 4% above the prior close on at least 3 times the prior 50-session average volume and closed at or above its open, above the 200-day average. Buy the next session, largest volume surge first.',
   exit='Sell after 60 sessions held, or at a 15% loss from average cost.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active. Backtest: 8.1% a year, behind the 12.1% equal-weight hold.'),
 # Added at the user's request as a deliberate twin of 12-1 momentum, although it fails the uniqueness rule
 # (0.86 excess-return correlation, 78% shared holdings): paper results show how far two close rules drift apart.
 'alpha':dict(name='Alpha · monthly · paper basket',family='One-year Jensen alpha against the Nifty 500 (NSE Alpha-style)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=30,rebalance='monthly',
   entry='For each Nifty 500 stock, take the average daily return of the latest 250 completed sessions minus beta times the Nifty 500 average, beta from the same sessions: the return its market sensitivity does not explain. Buy the 10 highest positive alphas that close above the 200-day average. Stocks with under a year of clean history are not ranked. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 30, turn negative on alpha or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active. Backtest: 27.3% a year vs 12.1% for the hold, with about 50% worst fall; holds mostly the same stocks as 12-1 momentum.'),
 # Passed every check and the uniqueness rule in the 2018-2026 point-in-time backtest (scripts/research_baskets.py, 'clenow'):
 # 15.0% a year vs 12.1% for the equal-weight hold, 37.8% worst fall vs 54.7%. Published rules, no per-stock stop.
 'clenow':dict(name='Stocks on the Move · monthly · paper basket',family='Trend quality: regression slope x R squared (Andreas Clenow, Stocks on the Move)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=None,keep_rank=100,rebalance='monthly',
   entry='Fit a straight line to the log of the latest 90 completed closes. Score each Nifty 500 stock by the line’s annualized slope times its R squared, so steady climbs beat jumpy ones. Buy the 10 highest positive scores that close above the 100- and 200-day averages and had no one-day move of 15% or more in 90 sessions, only while the Nifty 500 closes above its own 200-day average. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 100 scores, close below the 100-day average or had a one-day move of 15% or more in 90 sessions. No per-stock stop loss, as in the published rules.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak and while the Nifty 500 is below its 200-day average; exits stay active. Backtest: 15.0% a year vs 12.1% for the hold, 37.8% worst fall vs 54.7%.'),
 'sector_rotation':dict(name='Sector rotation · monthly · paper basket',family='Industry momentum rotation (Moskowitz and Grinblatt)',
   max_positions=10,position_pct=10,max_buys_per_day=10,stop_pct=20,keep_rank=30,rebalance='monthly',
   entry='Average the 6-1 momentum of tradable Nifty 500 stocks in each sector with at least 5 of them. In the 3 strongest sectors, rank stocks with positive 6-1 momentum and a completed close above the 200-day average by that momentum. Buy the top 10. Empty slots are refilled from the current top 10.',
   exit='At the first session of each month, sell holdings that drop out of the top 30 (including when their sector leaves the top 3) or close below the 200-day average. Between rebalances a 20% loss from average cost exits.',
   risk='Equal weight: up to 10% of starting capital per stock including buy fees. New buys pause at a 15% fall from the equity peak; exits stay active. Backtest: 1.5% a year, behind the 12.1% equal-weight hold.'),
}

# Retired baskets keep their ledger history but never trade again and are hidden from the screens.
RETIRED={'reversion':'Retired: it duplicates the daily Stat-arb basket, and fees outweighed gains in the backtest.',
 'high52':'Retired before its first trade: 2.2% a year vs 12.1% for the equal-weight hold in the 2018-2026 point-in-time backtest (scripts/research_baskets.py).'}

def rsi(closes,period=2):
    gain=loss=None
    for a,b in zip(closes,closes[1:]):
        g=max(b-a,0);l=max(a-b,0)
        if gain is None:gain,loss=g,l
        else:gain=(gain*(period-1)+g)/period;loss=(loss*(period-1)+l)/period
    if gain is None:return None
    return 100. if loss==0 else 100-100/(1+gain/loss)

def jensen_alpha(rows,bench):
    """Mean daily log return minus beta times the benchmark mean over the latest ALPHA_WINDOW returns (sample
    covariance and variance), pairing each stock return with the benchmark move between the same two sessions."""
    pairs=[(math.log(b['close']/a['close']),math.log(bench[b['ts'][:10]]/bench[a['ts'][:10]])) for a,b in zip(rows[-ALPHA_WINDOW-1:],rows[-ALPHA_WINDOW:])
           if a['ts'][:10] in bench and b['ts'][:10] in bench]
    if len(pairs)<ALPHA_MIN:return None
    r,m=[p[0] for p in pairs],[p[1] for p in pairs];mr,mm=statistics.mean(r),statistics.mean(m)
    var=sum((x-mm)**2 for x in m)/(len(m)-1)
    if var<=0:return None
    beta=sum((x-mr)*(y-mm) for x,y in zip(r,m))/(len(m)-1)/var
    return mr-beta*mm

def clenow_score(closes):
    """Annualized least-squares slope of log closes times R squared over the latest CLENOW_WINDOW closes."""
    y=[math.log(c) for c in closes[-CLENOW_WINDOW:]]
    if len(y)<CLENOW_WINDOW:return None
    n=len(y);mt=(n-1)/2;my=statistics.mean(y)
    stt=sum((t-mt)**2 for t in range(n));syy=sum((v-my)**2 for v in y);sty=sum((t-mt)*(v-my) for t,v in enumerate(y))
    if syy<=0:return None
    return (math.exp(sty/stt*250)-1)*sty*sty/(stt*syy)

def index_up(bench,day):
    """Nifty 500 close above its 200-session average on `day`; None (no buys) if that close is missing."""
    days=sorted(d for d in bench if d<=day)[-INDEX_TREND:]
    if len(days)<INDEX_TREND or days[-1]!=day:return None
    return bench[day]>sum(bench[d] for d in days)/INDEX_TREND

def stats(rows,confirmed=(),bench=None):
    """Indicators from completed rows (oldest first), or None if history is short or broken.
    `confirmed` holds session dates whose large move NSE verified as an ordinary market move
    (price_reconciliation.confirmed, as in the data audit); those are not treated as breaks.
    `bench` maps session dates to Nifty 500 closes; without it the alpha field is None."""
    rows=effective_rows(rows,confirmed)
    if len(rows)<MIN_HISTORY:return None
    closes=[r['close'] for r in rows]
    if any(not (isinstance(c,(int,float)) and math.isfinite(c) and c>0) for c in closes):return None
    recent=rows[-(LOOKBACK+1):]
    # An unverified >25% one-day move inside the lookback is treated as an unreconciled corporate action.
    if any(b['ts'][:10] not in confirmed and abs(b['close']/a['close']-1)>.25 for a,b in zip(recent,recent[1:])):return None
    rets=[math.log(b/a) for a,b in zip(closes[-VOL_WINDOW-1:],closes[-VOL_WINDOW:])]
    window=long_window(len(rows))
    high52=closes[-1]/max(r['high'] for r in rows[-HIGH_WINDOW:]) if len(rows)>=HIGH_WINDOW else None
    year=rows[-(LONG_LOOKBACK+1):]
    # 12-1 momentum needs the whole year free of unverified breaks, not just the 6-month lookback.
    clean_year=len(rows)>LONG_LOOKBACK and not any(b['ts'][:10] not in confirmed and abs(b['close']/a['close']-1)>.25 for a,b in zip(year,year[1:]))
    mom12=closes[-SKIP-1]/closes[-LONG_LOOKBACK-1]-1 if clean_year else None
    alpha=jensen_alpha(rows,bench) if clean_year and bench else None
    window_on=rows[-(OVERNIGHT_WINDOW+SKIP+1):-SKIP]
    overnight=sum(math.log(b['open']/a['close']) for a,b in zip(window_on,window_on[1:])) if len(window_on)==OVERNIGHT_WINDOW+1 and all(
        isinstance(b.get('open'),(int,float)) and math.isfinite(b['open']) and b['open']>0 for b in window_on[1:]) else None
    last,before=rows[-1],rows[-2];base=[r['volume'] for r in rows[-51:-1]]
    gap=last['open']/before['close']-1 if isinstance(last.get('open'),(int,float)) and last['open']>0 else None
    gap_volume=last['volume']/(sum(base)/len(base)) if base and sum(base)>0 else None
    move90=max(abs(b/a-1) for a,b in zip(closes[-CLENOW_WINDOW-1:-1],closes[-CLENOW_WINDOW:]))
    return dict(clenow=clenow_score(closes),move90=move90,dma100=sum(closes[-100:])/100,index_up=index_up(bench,rows[-1]['ts'][:10]) if bench else None,
        high52=high52,momentum12=mom12,alpha=alpha,overnight=overnight,gap=gap,gap_volume=gap_volume,open=last.get('open'),as_of=rows[-1]['ts'][:10],close=closes[-1],dma200=sum(closes[-window:])/window,dma5=sum(closes[-5:])/5,
        momentum=closes[-SKIP-1]/closes[-LOOKBACK-1]-1,volatility=statistics.pstdev(rets)*math.sqrt(250),rsi2=rsi(closes[-30:]),
        turnover20=sum(r['close']*r['volume'] for r in rows[-20:])/20)

def tradable(s):return bool(s) and s['close']>=20 and s['turnover20']>=MIN_TURNOVER

def ranking(basket,signals):
    """Ordered candidate symbols for a basket from {symbol: stats}."""
    if basket=='momentum':
        pool=[(-s['momentum'],k) for k,s in signals.items() if tradable(s) and s['momentum']>0 and s['close']>s['dma200']]
    elif basket=='lowvol':
        pool=[(s['volatility'],k) for k,s in signals.items() if tradable(s) and s['volatility']>0 and s['close']>s['dma200']]
    elif basket=='reversion':
        pool=[(s['rsi2'],k) for k,s in signals.items() if tradable(s) and s['rsi2'] is not None and s['rsi2']<10 and s['close']>s['dma200']]
    elif basket=='high52':
        pool=[(-s['high52'],k) for k,s in signals.items() if tradable(s) and s.get('high52') and s['close']>s['dma200']]
    elif basket=='momentum12':
        pool=[(-s['momentum12'],k) for k,s in signals.items() if tradable(s) and s.get('momentum12') is not None and s['momentum12']>0 and s['close']>s['dma200']]
    elif basket=='alpha':
        pool=[(-s['alpha'],k) for k,s in signals.items() if tradable(s) and s.get('alpha') is not None and s['alpha']>0 and s['close']>s['dma200']]
    elif basket=='overnight':
        pool=[(-s['overnight'],k) for k,s in signals.items() if tradable(s) and s.get('overnight') is not None and s['overnight']>0 and s['close']>s['dma200']]
    elif basket=='gap_drift':
        pool=[(-s['gap_volume'],k) for k,s in signals.items() if tradable(s) and s['close']>s['dma200'] and s.get('gap') is not None and s['gap']>=GAP_PCT
              and s.get('gap_volume') is not None and s['gap_volume']>=GAP_VOLUME and s['close']>=s['open']]
    elif basket=='sector_rotation':
        groups={}
        for s in signals.values():
            if tradable(s) and s.get('sector'):groups.setdefault(s['sector'],[]).append(s['momentum'])
        strong={g for _,g in sorted((-statistics.mean(v),g) for g,v in groups.items() if len(v)>=5)[:3]}
        pool=[(-s['momentum'],k) for k,s in signals.items() if tradable(s) and s.get('sector') in strong and s['momentum']>0 and s['close']>s['dma200']]
    elif basket=='clenow':
        pool=[(-s['clenow'],k) for k,s in signals.items() if tradable(s) and s.get('clenow') is not None and s['clenow']>0 and s['close']>s['dma200']
              and s['close']>s['dma100'] and s['move90']<CLENOW_MAX_MOVE and s.get('index_up') is True]
    else:raise ValueError('Unknown basket')
    return [k for _,k in sorted(pool)]

def exit_ranking(basket,signals):
    """Order the monthly keep-rank is read from; Clenow ranks every clean stock above its 100-day average."""
    if basket!='clenow':return ranking(basket,signals)
    pool=[(-s['clenow'],k) for k,s in signals.items() if s and s.get('clenow') is not None and s['close']>s['dma100'] and s['move90']<CLENOW_MAX_MOVE]
    return [k for _,k in sorted(pool)]

def rebalance_exit(basket,symbol,signal,order):
    """Reason to sell a holding at a monthly review, or None to keep it."""
    rule=BASKETS[basket]
    if not signal:return 'No valid completed history at monthly review'
    if basket=='clenow':
        if signal['close']<=signal['dma100']:return 'Completed close below the 100-day average at monthly review'
        if signal['move90']>=CLENOW_MAX_MOVE:return 'One-day move of 15% or more in 90 sessions at monthly review'
    elif signal['close']<signal['dma200']:return 'Completed close below the 200-day average at monthly review'
    if basket=='momentum' and signal['momentum']<=0:return 'Negative 6-1 momentum at monthly review'
    if basket=='momentum12' and not (signal.get('momentum12') or 0)>0:return 'Negative or unavailable 12-1 momentum at monthly review'
    if basket=='alpha' and not (signal.get('alpha') or 0)>0:return 'Negative or unavailable one-year alpha at monthly review'
    rank=order.index(symbol)+1 if symbol in order else None
    if rank is None or rank>rule['keep_rank']:return f"Outside the top {rule['keep_rank']} at monthly review"
    return None

def daily_exit(basket,signal,held_sessions):
    """Completed-close exits checked every session (daily baskets only)."""
    if BASKETS[basket]['rebalance']!='daily':return None
    if basket=='reversion' and signal and signal['close']>signal['dma5']:return 'Completed close above the 5-day average'
    if held_sessions>=BASKETS[basket]['max_hold']:return f"Held {BASKETS[basket]['max_hold']} sessions"
    return None

def stop_exit(basket,average_cost,price):
    pct=BASKETS[basket]['stop_pct']
    if pct is None:return None
    return f'{pct}% loss from average cost' if price<=average_cost*(1-pct/100) else None
