"""Signal rules for the additional paper baskets. Pure functions over completed daily candles.

The live engine and the backtest call the same functions, so tested and traded rules cannot drift.
Every input is a completed session; nothing here sees the current session's price except `price` checks."""
import math,statistics
from .history_rules import long_window,effective_rows

MIN_HISTORY=130          # 6-month lookback + 1-month skip + margin
LOOKBACK,SKIP=126,21     # 6-1 momentum: return from ~6 months ago to ~1 month ago
VOL_WINDOW=120
MIN_TURNOVER=10_000_000  # ₹1 crore average daily traded value, same as Trader/Investor

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
}

def rsi(closes,period=2):
    gain=loss=None
    for a,b in zip(closes,closes[1:]):
        g=max(b-a,0);l=max(a-b,0)
        if gain is None:gain,loss=g,l
        else:gain=(gain*(period-1)+g)/period;loss=(loss*(period-1)+l)/period
    if gain is None:return None
    return 100. if loss==0 else 100-100/(1+gain/loss)

def stats(rows):
    """Indicators from completed rows (oldest first), or None if history is short or broken."""
    rows=effective_rows(rows)
    if len(rows)<MIN_HISTORY:return None
    closes=[r['close'] for r in rows]
    if any(not (isinstance(c,(int,float)) and math.isfinite(c) and c>0) for c in closes):return None
    recent=rows[-(LOOKBACK+1):]
    # A >25% one-day move inside the lookback is treated as an unreconciled corporate action.
    if any(abs(b['close']/a['close']-1)>.25 for a,b in zip(recent,recent[1:])):return None
    rets=[math.log(b/a) for a,b in zip(closes[-VOL_WINDOW-1:],closes[-VOL_WINDOW:])]
    window=long_window(len(rows))
    return dict(as_of=rows[-1]['ts'][:10],close=closes[-1],dma200=sum(closes[-window:])/window,dma5=sum(closes[-5:])/5,
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
    else:raise ValueError('Unknown basket')
    return [k for _,k in sorted(pool)]

def rebalance_exit(basket,symbol,signal,order):
    """Reason to sell a holding at a monthly review, or None to keep it."""
    rule=BASKETS[basket]
    if not signal:return 'No valid completed history at monthly review'
    if signal['close']<signal['dma200']:return 'Completed close below the 200-day average at monthly review'
    if basket=='momentum' and signal['momentum']<=0:return 'Negative 6-1 momentum at monthly review'
    rank=order.index(symbol)+1 if symbol in order else None
    if rank is None or rank>rule['keep_rank']:return f"Outside the top {rule['keep_rank']} at monthly review"
    return None

def daily_exit(basket,signal,held_sessions):
    """Completed-close exits checked every session (mean reversion only)."""
    if basket!='reversion':return None
    if signal and signal['close']>signal['dma5']:return 'Completed close above the 5-day average'
    if held_sessions>=BASKETS[basket]['max_hold']:return f"Held {BASKETS[basket]['max_hold']} sessions"
    return None

def stop_exit(basket,average_cost,price):
    pct=BASKETS[basket]['stop_pct']
    return f'{pct}% loss from average cost' if price<=average_cost*(1-pct/100) else None
