"""Offline market-breadth risk gate; not a profitability claim or live setting."""
import math

NAME='breadth_guard_v1'
SPEC={'minimum_stocks':500,'majority':.5,'description':'New buys require strictly more than half the valid prior-day universe above each of its 50- and 200-day averages. Exits remain active.'}


def breadth(signals):
    values=[]
    for s in signals.values():
        numbers=[s.get(k) for k in ('close','dma50','dma200')]
        if all(isinstance(v,(float,int)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in numbers):values.append(numbers)
    n=len(values);above50=sum(c>d50 for c,d50,d200 in values);above200=sum(c>d200 for c,d50,d200 in values)
    return {'stocks':n,'above50_pct':round(100*above50/n,3) if n else None,'above200_pct':round(100*above200/n,3) if n else None,
            'permits_buys':n>=SPEC['minimum_stocks'] and above50>n*SPEC['majority'] and above200>n*SPEC['majority']}
