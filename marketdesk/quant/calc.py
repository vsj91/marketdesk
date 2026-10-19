"""Pure calculation kernels for the quant paper baskets. No I/O, no clock, no database.

Every function takes completed daily data (oldest first) and returns plain numbers or dicts,
so the cron pipeline, the replay and the tests all exercise exactly the same math."""
import math,statistics

TRADING_DAYS=252

def finite(x):return isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)

def sma(values,window):
    return sum(values[-window:])/window if len(values)>=window else None

def log_returns(closes):
    return [math.log(b/a) for a,b in zip(closes,closes[1:]) if a>0 and b>0]

def realized_vol(closes,window):
    """Annualized standard deviation of the latest `window` daily log returns."""
    if len(closes)<window+1:return None
    r=log_returns(closes[-window-1:])
    return statistics.stdev(r)*math.sqrt(TRADING_DAYS) if len(r)>=2 else None

def momentum_12_1(closes,long=TRADING_DAYS,skip=21):
    """R(12M) - R(1M): trailing 12-month return minus the latest month, so the
    short-term reversal month does not drive the ranking."""
    if len(closes)<long+1:return None
    return (closes[-1]/closes[-long-1]-1)-(closes[-1]/closes[-skip-1]-1)

def rolling_z(values,window=20):
    """(latest - mean) / sample std over the latest `window` values (latest included)."""
    if len(values)<window:return None
    w=values[-window:];sd=statistics.stdev(w)
    return (w[-1]-statistics.mean(w))/sd if sd>0 else None

def sma_slope_up(closes,window=200,lag=20):
    """True when the long SMA today is above the same SMA `lag` sessions ago."""
    if len(closes)<window+lag:return None
    return sma(closes,window)>sma(closes[:-lag],window)

def true_ranges(bars):
    return [max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars,bars[1:])]

def atr(bars,period=14):
    """Wilder ATR over all supplied bars (needs period+1 bars)."""
    tr=true_ranges(bars)
    if len(tr)<period:return None
    value=sum(tr[:period])/period
    for x in tr[period:]:value=(value*(period-1)+x)/period
    return value

def atr_shares(equity,atr_value,risk_pct=.01,atr_mult=2.):
    """Shares so that an `atr_mult` x ATR move costs `risk_pct` of total equity."""
    if not (finite(atr_value) and atr_value>0 and equity>0):return 0
    return math.floor(equity*risk_pct/(atr_mult*atr_value))

def donchian_breakout(bars,window=50):
    """Completed close above the highest high of the prior `window` sessions."""
    if len(bars)<window+1:return None
    return bars[-1]['close']>max(b['high'] for b in bars[-window-1:-1])

def volume_surge(bars,window=20,ratio=1.5):
    """Latest volume against the average of the prior `window` sessions; returns the multiple."""
    if len(bars)<window+1:return None
    avg=sum(b['volume'] for b in bars[-window-1:-1])/window
    return bars[-1]['volume']/avg if avg>0 else None

def beta(closes,bench,window=TRADING_DAYS):
    """OLS beta of daily log returns against the benchmark over aligned closes."""
    n=min(len(closes),len(bench))
    if n<60:return None
    a=log_returns(closes[-min(n,window+1):]);b=log_returns(bench[-min(n,window+1):])
    m=min(len(a),len(b));a,b=a[-m:],b[-m:]
    if m<40:return None
    mb=statistics.mean(b);var=sum((x-mb)**2 for x in b)
    if var<=0:return None
    ma=statistics.mean(a)
    return sum((x-ma)*(y-mb) for x,y in zip(a,b))/var

def zscores(values,clip=3.):
    """Cross-sectional z-scores for {key: raw}; None/non-finite inputs are skipped.
    Winsorized at +-clip so one outlier cannot dominate a composite."""
    good={k:v for k,v in values.items() if finite(v)}
    if len(good)<3:return {}
    mu=statistics.mean(good.values());sd=statistics.pstdev(good.values())
    if sd<=0:return {k:0. for k in good}
    return {k:max(-clip,min(clip,(v-mu)/sd)) for k,v in good.items()}

def composite(parts,weights,min_coverage=.5):
    """Weighted mean of factor z-score maps {factor: {key: z}}, renormalized over the factors each
    key actually has. Keys covering less than `min_coverage` of the total weight are dropped."""
    total=sum(weights.values());keys=set().union(*parts.values()) if parts else set();out={}
    for k in keys:
        have={f:w for f,w in weights.items() if k in parts.get(f,{})}
        cover=sum(have.values())
        if cover/total>=min_coverage:out[k]=sum(parts[f][k]*w for f,w in have.items())/cover
    return out

def inverse_vol_weights(vols):
    """w_i = (1/sigma_i) / sum_j(1/sigma_j). Non-positive or missing sigmas are excluded."""
    inv={k:1/v for k,v in vols.items() if finite(v) and v>0}
    total=sum(inv.values())
    return {k:x/total for k,x in inv.items()} if total>0 else {}

def cap_weights(weights,groups,cap):
    """Scale down any group above `cap` of total weight and redistribute the excess to the
    uncapped names pro rata. Iterates until stable; returns weights summing to the input total."""
    w=dict(weights);total=sum(w.values())
    if total<=0:return w
    for _ in range(50):
        sums={}
        for k,x in w.items():sums[groups.get(k)]=sums.get(groups.get(k),0)+x
        over={g for g,s in sums.items() if s>cap*total+1e-12}
        if not over:break
        free=[k for k in w if groups.get(k) not in over and sums[groups.get(k)]<cap*total-1e-12]
        excess=0.
        for g in over:
            scale=cap*total/sums[g]
            for k in w:
                if groups.get(k)==g:excess+=w[k]*(1-scale);w[k]*=scale
        base=sum(w[k] for k in free)
        if not free or base<=0:break   # nowhere to put it: leave the remainder in cash
        for k in free:w[k]+=excess*w[k]/base
    return w

def pick_with_sector_cap(ranked,sectors,n,cap):
    """Walk an ordered candidate list and take up to n names with at most floor(cap*n) per sector."""
    limit=max(1,math.floor(cap*n+1e-9));count={};out=[]
    for k in ranked:
        s=sectors.get(k) or 'Unclassified'
        if count.get(s,0)>=limit:continue
        out.append(k);count[s]=count.get(s,0)+1
        if len(out)>=n:break
    return out

def regime(bench_closes,ma_window=200,vol_window=20,vol_mean_window=20,vix=None):
    """'bull' | 'choppy' | 'bear' from the index trend and realized volatility.

    bear:   close < 200-DMA
    choppy: close > 200-DMA and today's 20-day realized vol > its own 20-day mean (or VIX > its 20-day mean)
    bull:   close > 200-DMA and vol <= that mean"""
    need=max(ma_window,vol_window+vol_mean_window)+1
    if len(bench_closes)<need:return None
    close=bench_closes[-1];ma=sma(bench_closes,ma_window)
    if vix and len(vix)>=vol_mean_window:
        vol,mean=vix[-1],sum(vix[-vol_mean_window:])/vol_mean_window
    else:
        series=[realized_vol(bench_closes[:len(bench_closes)-i],vol_window) for i in range(vol_mean_window)][::-1]
        vol,mean=series[-1],sum(series)/len(series)
    label='bear' if close<ma else ('choppy' if vol>mean else 'bull')
    return dict(regime=label,index_close=close,dma200=ma,volatility=vol,volatility_mean=mean)

def confirmed_regime(bench_closes,current=None,confirm=3,vix=None):
    """Regime with hysteresis: switch away from `current` only after `confirm` consecutive sessions
    classify the same new regime. Returns the regime() dict plus 'raw' (today's unconfirmed label)."""
    today=regime(bench_closes,vix=vix)
    if not today:return None
    out=dict(today,raw=today['regime'])
    if current is None or today['regime']==current or confirm<=1:return out
    n=len(bench_closes)
    labels=[regime(bench_closes[:n-i],vix=vix[:len(vix)-i] if vix else None) for i in range(1,confirm)]
    if all(r and r['regime']==today['regime'] for r in labels):return out
    out['regime']=current;return out

def typical_price(bar):return (bar['high']+bar['low']+bar['close'])/3
