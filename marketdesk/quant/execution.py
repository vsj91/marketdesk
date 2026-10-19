"""ExecutionSimulator: turns paper orders into fills with realistic friction.

Fill model (per order, on the session after the signal):
  base price   typical price (H+L+C)/3 of the fill session, a VWAP proxy for a full-day working order
  slippage     liquidity tier: 5 bps large cap, 15 bps mid cap, 25 bps small/unknown
  impact       square-root model: coefficient x daily vol x sqrt(order shares / 20-day ADV), in bps
  participation orders above `max_participation` of the fill session's volume are partially filled
  charges      marketdesk.execution.charges (brokerage, STT, exchange, SEBI, stamp, DP on sells)
Buys pay up, sells receive less. Nothing here reads quotes or calls a broker."""
import math
from ..execution import charges
from .calc import typical_price,finite

TIER_BPS={'Large cap':5.,'Mid cap':15.,'Small cap':25.}
UNKNOWN_BPS=25.

class ExecutionSimulator:
    def __init__(self,tier_bps=None,unknown_bps=UNKNOWN_BPS,impact_coefficient=.5,max_participation=.10,max_impact_bps=100.):
        self.tier_bps={**TIER_BPS,**(tier_bps or {})};self.unknown_bps=unknown_bps
        self.impact_coefficient=impact_coefficient;self.max_participation=max_participation;self.max_impact_bps=max_impact_bps

    def slippage_bps(self,size_label,qty,adv,daily_vol):
        base=self.tier_bps.get(size_label,self.unknown_bps);impact=0.
        if finite(adv) and adv>0 and finite(daily_vol) and daily_vol>0:
            impact=min(self.max_impact_bps,1e4*self.impact_coefficient*daily_vol*math.sqrt(qty/adv))
        return base,impact

    def fill(self,side,qty,bar,size_label=None,adv=None,daily_vol=None):
        """Simulated fill of `qty` shares on `bar`. Returns None when nothing can fill."""
        if qty<=0 or not bar or not (finite(bar.get('volume')) and bar['volume']>0):return None
        cap=math.floor(bar['volume']*self.max_participation)
        filled=min(qty,cap)
        if filled<=0:return None
        base,impact=self.slippage_bps(size_label,filled,adv,daily_vol);bps=base+impact
        ref=typical_price(bar);sign=1 if side=='BUY' else -1
        price=ref*(1+sign*bps/1e4)
        price=min(max(price,bar['low']),bar['high'])     # a fill can never print outside the session's range
        value=round(filled*price,2)
        fee=charges(value,side,dp=side=='SELL')['total']
        return dict(side=side,requested=qty,quantity=filled,partial=filled<qty,reference_price=round(ref,4),price=round(price,4),
            value=value,fee=fee,slippage_bps=round(base,2),impact_bps=round(impact,2),
            cash_delta=round(-(value+fee) if side=='BUY' else value-fee,2))

    def affordable(self,cash,price,size_label=None):
        """Whole shares a buy can afford at an estimated slipped price, leaving room for charges."""
        bps=self.tier_bps.get(size_label,self.unknown_bps)+self.max_impact_bps
        est=price*(1+bps/1e4)
        qty=math.floor(max(0,cash-25)/(est*1.0025))
        while qty>0 and qty*est+charges(round(qty*est,2),'BUY')['total']>cash:qty-=1
        return qty

def drift_orders(targets,holdings,prices,equity,threshold=.02):
    """Weight/share targets -> share deltas, skipping resizes inside the +-threshold band.

    targets  {symbol: {weight|shares|keep, ...}}
    holdings {symbol: qty}
    Returns [(symbol, delta_shares, weight_before, weight_after, kind)] where kind is
    'add' (new name), 'raise', 'trim'. Full exits are handled by the caller and never banded."""
    out=[]
    for s,t in targets.items():
        if t.get('keep'):continue
        px=prices.get(s)
        if not (finite(px) and px>0) or equity<=0:continue
        held=holdings.get(s,0);before=held*px/equity
        want=math.floor(t['weight']*equity/px) if 'weight' in t else int(t['shares'])
        after=want*px/equity;delta=want-held
        if delta==0:continue
        if held==0:
            if want>0:out.append((s,delta,before,after,'add'))
            continue
        if abs(after-before)<=threshold:continue
        out.append((s,delta,before,after,'raise' if delta>0 else 'trim'))
    return out
