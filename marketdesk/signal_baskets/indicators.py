"""Pure indicator kernels over completed OHLCV bars (oldest first). No I/O, no clock.

Every function only reads the bars it is given. Callers pass completed candles only, so a value
computed at bar i never depends on a later bar. Swing points need `k` bars on both sides; a pivot
at index i is reported only when bars i+1..i+k are already in the list (it is "confirmed at" i+k),
so swing detection never looks ahead of the newest completed candle."""
import math
from decimal import Decimal,ROUND_CEILING,ROUND_FLOOR

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def ema_series(values,period):
    """EMA with alpha 2/(n+1), seeded by the SMA of the first `period` values; None before that."""
    out=[None]*len(values)
    if period<1 or len(values)<period:return out
    alpha=2/(period+1);value=sum(values[:period])/period;out[period-1]=value
    for i in range(period,len(values)):
        value=values[i]*alpha+value*(1-alpha);out[i]=value
    return out

def rsi_series(closes,period=14):
    """Wilder RSI: first average gain/loss is the simple mean of `period` changes, then Wilder smoothing."""
    out=[None]*len(closes)
    if len(closes)<period+1:return out
    changes=[b-a for a,b in zip(closes,closes[1:])]
    gain=sum(max(c,0) for c in changes[:period])/period;loss=sum(max(-c,0) for c in changes[:period])/period
    def value(g,l):return 100. if l==0 and g>0 else (50. if l==0 else 100-100/(1+g/l))
    out[period]=value(gain,loss)
    for i in range(period,len(changes)):
        c=changes[i];gain=(gain*(period-1)+max(c,0))/period;loss=(loss*(period-1)+max(-c,0))/period
        out[i+1]=value(gain,loss)
    return out

def true_range(prev,bar):
    return max(bar['high']-bar['low'],abs(bar['high']-prev['close']),abs(bar['low']-prev['close'])) if prev else bar['high']-bar['low']

def atr_series(bars,period=14):
    """Wilder ATR: SMA of the first `period` true ranges (each needs a previous close), then Wilder smoothing."""
    out=[None]*len(bars)
    if len(bars)<period+1:return out
    tr=[true_range(a,b) for a,b in zip(bars,bars[1:])]
    value=sum(tr[:period])/period;out[period]=value
    for i in range(period,len(tr)):
        value=(value*(period-1)+tr[i])/period;out[i+1]=value
    return out

def round_tick(price,tick,direction):
    """Round to a whole number of ticks: 'up' for buy triggers/limits, 'down' for long stops/targets."""
    if not (finite(price) and finite(tick) and tick>0):raise ValueError('Invalid price or tick size')
    steps=(Decimal(str(price))/Decimal(str(tick))).to_integral_value(rounding=ROUND_CEILING if direction=='up' else ROUND_FLOOR)
    return float(steps*Decimal(str(tick)))

def swing_points(bars,k=3,lookback=None):
    """Confirmed pivot highs/lows: bar i is a pivot low when its low is strictly below the k lows before it
    and not above the k lows after it (mirror for highs). Only pivots with k later completed bars count."""
    start=0 if lookback is None else max(0,len(bars)-lookback)
    highs=[];lows=[]
    for i in range(max(start,k),len(bars)-k):
        left,right=bars[i-k:i],bars[i+1:i+k+1]
        if bars[i]['low']<min(b['low'] for b in left) and bars[i]['low']<=min(b['low'] for b in right):
            lows.append(dict(index=i,price=bars[i]['low'],ts=bars[i].get('ts'),confirmed_at=i+k))
        if bars[i]['high']>max(b['high'] for b in left) and bars[i]['high']>=max(b['high'] for b in right):
            highs.append(dict(index=i,price=bars[i]['high'],ts=bars[i].get('ts'),confirmed_at=i+k))
    return highs,lows

def cluster_levels(points,tolerance):
    """Merge pivot prices within `tolerance` into shelves/zones: {'price','low','high','touches','last_index'}."""
    levels=[]
    for p in sorted(points,key=lambda x:x['price']):
        if levels and p['price']-levels[-1]['high']<=tolerance:
            z=levels[-1];z['high']=p['price'];z['touches']+=1;z['last_index']=max(z['last_index'],p['index'])
        else:levels.append(dict(low=p['price'],high=p['price'],touches=1,last_index=p['index']))
    for z in levels:z['price']=(z['low']+z['high'])/2
    return levels

def structure(bars,k=3,lookback=150,tolerance=None):
    """Support and resistance from confirmed swings. A swing high that price has closed above becomes
    breakout support (role reversal); swing lows price closed below become overhead supply."""
    highs,lows=swing_points(bars,k,lookback)
    if tolerance is None:
        atr=[x for x in atr_series(bars) if x is not None]
        tolerance=.25*atr[-1] if atr else 0
    close=bars[-1]['close'] if bars else None
    supports=cluster_levels([p for p in lows if p['price']<close]+[p for p in highs if p['price']<close],tolerance) if bars else []
    resistances=cluster_levels([p for p in highs if p['price']>close]+[p for p in lows if p['price']>close],tolerance) if bars else []
    return dict(supports=supports,resistances=resistances,swing_highs=highs,swing_lows=lows,tolerance=tolerance)

def nearest_support(levels,below):
    """Highest support zone whose low is strictly below `below`; uses the zone low (conservative for a long stop)."""
    c=[z for z in levels if z['low']<below]
    return max(c,key=lambda z:z['low']) if c else None

def nearest_resistance(levels,above):
    """Lowest resistance zone whose low is strictly above `above`; uses the zone low (conservative for a target)."""
    c=[z for z in levels if z['low']>above]
    return min(c,key=lambda z:z['low']) if c else None
