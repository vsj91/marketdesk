"""Performance metrics over a daily NAV series. Pure functions.

Conventions: simple daily returns, 252 sessions a year, risk-free rate as an annual decimal
(default 6.5%, roughly the Indian 91-day T-bill), Sortino downside deviation below the daily risk-free rate."""
import math,statistics
from .calc import TRADING_DAYS

RISK_FREE=.065

def returns(series):return [b/a-1 for a,b in zip(series,series[1:]) if a>0]

def drawdowns(series):
    peak=None;out=[]
    for x in series:
        peak=x if peak is None else max(peak,x);out.append(x/peak-1 if peak>0 else 0.)
    return out

def performance(nav,benchmark=None,risk_free=RISK_FREE):
    """Metrics dict for a NAV list (oldest first) and an optional aligned benchmark close list."""
    out=dict(sessions=len(nav),cumulative_return_pct=None,benchmark_return_pct=None,excess_return_pct=None,
        annualized_return_pct=None,annualized_volatility_pct=None,sharpe=None,sortino=None,max_drawdown_pct=None,ulcer_index=None)
    if len(nav)<2:return out
    r=returns(nav);rf=(1+risk_free)**(1/TRADING_DAYS)-1
    cum=nav[-1]/nav[0]-1;out['cumulative_return_pct']=round(100*cum,3)
    out['annualized_return_pct']=round(100*((1+cum)**(TRADING_DAYS/len(r))-1),3) if cum>-1 else -100.
    if benchmark and len(benchmark)>=2 and benchmark[0]:
        b=benchmark[-1]/benchmark[0]-1;out['benchmark_return_pct']=round(100*b,3);out['excess_return_pct']=round(100*(cum-b),3)
    dd=drawdowns(nav);out['max_drawdown_pct']=round(-100*min(dd),3)
    out['ulcer_index']=round(math.sqrt(sum((100*d)**2 for d in dd)/len(dd)),4)
    if len(r)>=2:
        sd=statistics.stdev(r);ex=[x-rf for x in r];mean=statistics.mean(ex)
        out['annualized_volatility_pct']=round(100*sd*math.sqrt(TRADING_DAYS),3)
        out['sharpe']=round(mean/sd*math.sqrt(TRADING_DAYS),3) if sd>0 else None
        down=math.sqrt(sum(min(0,x)**2 for x in ex)/len(ex))
        out['sortino']=round(mean/down*math.sqrt(TRADING_DAYS),3) if down>0 else None
    return out
