"""Pure 5-minute setup rules for the three experimental intraday paper baskets.

Inputs are completed bars oldest first. Order-flow depth and the daily EMA are explicit inputs so
missing live evidence cannot silently become a signal. Historical tests must label depth proxies.
"""
from .indicators import atr_series,ema_series,finite,round_tick
from . import vinay_rules as common
from .names import LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION,ORB_VOLUME_BREAKOUT

COMMON=dict(timeframe_minutes=5,atr_period=14,entry_start='10:00',entry_cutoff='14:00',square_off='15:10',
    max_positions=1,max_entries_per_day=1,max_position_pct=95.,risk_pct=1.,max_open_risk_pct=6.,max_daily_loss_pct=2.,
    fee_max_pct=.1,monthly_fee_budget=500.,max_chase_pct=.3,max_slippage_pct=.25,
    target_r=2.,breakeven_r=1.,tick_size=.05,slippage_bps=5.,min_price=50.)
DEFAULTS={
 LIQUIDITY_SWEEP:{**COMMON,'min_stop_pct':.8,'max_stop_pct':1.8,'atr_sweep_mult':.4,'min_rvol':2.,'target_r':2.5,'breakeven_r':1.2,'trail_atr_mult':1.,'max_chase_pct':.3,'max_vwap_extension_pct':10.},
 ORDER_FLOW_REVERSAL:{**COMMON,'min_stop_pct':.7,'max_stop_pct':1.5,'atr_stop_mult':1.2,'min_rvol':0.,'target_r':2.,'breakeven_r':1.,'trail_atr_mult':.8,'max_chase_pct':.25,'max_depth_ratio':3.,'pullback_vwap_pct':.3},
 VOLATILITY_COMPRESSION:{**COMMON,'min_stop_pct':.8,'max_stop_pct':2.,'atr_stop_mult':1.5,'min_rvol':2.5,'target_r':2.,'breakeven_r':1.,'trail_atr_mult':0.,'max_chase_pct':.35,'bb_period':20,'bb_std':2.,'compression_percentile':15.},
 ORB_VOLUME_BREAKOUT:{**COMMON,'entry_start':'09:30','entry_cutoff':'10:00','min_stop_pct':.6,'max_stop_pct':1.5,'atr_stop_mult':1.,'min_rvol':3.,'target_r':1.5,'breakeven_r':1.,'trail_atr_mult':0.,'max_chase_pct':.3},
}
LIMITS={n:{'min_stop_pct':(.5,2.5),'max_stop_pct':(.6,3.),'target_r':(1.,4.),'min_rvol':(0.,5.),
    'max_position_pct':(5.,95.),'risk_pct':(.1,2.),'monthly_fee_budget':(0.,5000.),'fee_max_pct':(.05,.1),
    'max_daily_loss_pct':(.5,2.),'max_chase_pct':(.1,.6)} for n in DEFAULTS}
LIMITS[LIQUIDITY_SWEEP].update(atr_sweep_mult=(.1,1.),breakeven_r=(.5,2.),trail_atr_mult=(.3,2.))
LIMITS[ORDER_FLOW_REVERSAL].update(atr_stop_mult=(.5,2.),breakeven_r=(.5,2.),trail_atr_mult=(.3,1.5),max_depth_ratio=(2.,10.),pullback_vwap_pct=(.1,.5))
LIMITS[VOLATILITY_COMPRESSION].update(atr_stop_mult=(.5,2.5),breakeven_r=(.5,2.),bb_period=(15,30),bb_std=(1.,3.),compression_percentile=(5.,30.))
LIMITS[ORB_VOLUME_BREAKOUT].update(atr_stop_mult=(.5,1.5),breakeven_r=(.5,1.5))

def merged(name,cfg=None):return {**DEFAULTS[name],**(cfg or {})}

def _rvol(bars,i):
    v=bars[i].get('volume');prior=[x.get('volume') for x in bars[max(0,i-20):i]]
    if len(prior)<20 or not finite(v):return None
    avg=sum(x for x in prior if finite(x))/len([x for x in prior if finite(x)]) if any(finite(x) for x in prior) else 0
    return v/avg if avg>0 else None

def _vwap(session):
    vol=sum((b.get('volume') or 0) for b in session)
    return sum((b['high']+b['low']+b['close'])/3*(b.get('volume') or 0) for b in session)/vol if vol>0 else None

def _stop(entry,raw,cfg):
    pct=min(cfg['max_stop_pct'],max(cfg['min_stop_pct'],(entry-raw)/entry*100))
    stop=round_tick(entry*(1-pct/100),cfg['tick_size'],'down')
    return stop,pct

def _bb(closes,period,mult):
    if len(closes)<period:return None
    vals=closes[-period:];mean=sum(vals)/period
    sd=(sum((x-mean)**2 for x in vals)/period)**.5
    return mean+mult*sd,mean-mult*sd,((2*mult*sd)/mean*100 if mean else None)

def evaluate(name,bars,cfg=None,*,depth_ratio=None,daily_ema50=None):
    """Return audited candidate/none for the latest completed candle."""
    p=merged(name,cfg)
    if len(bars)<max(35,p['atr_period']+2):return None
    bar=bars[-1];today=bar['ts'][:10];session=[b for b in bars if b['ts'][:10]==today]
    if len(session)<4 or session[0]['ts'][11:16]!='09:15':return None
    atrs=atr_series(bars,p['atr_period']);atr=atrs[-1];rv=_rvol(bars,len(bars)-1)
    if not finite(atr) or atr<=0:return None
    close=bar['close'];vw=_vwap(session)
    out=dict(trigger_ts=bar['ts'],close=close,vwap=vw,atr14=atr,rvol=rv,depth_ratio=depth_ratio)
    reasons=[];raw_stop=None
    if name==ORB_VOLUME_BREAKOUT:
        opening=session[:3]
        if len(opening)<3 or opening[-1]['ts'][11:16]!='09:25':return None
        or_high=max(x['high'] for x in opening);or_low=min(x['low'] for x in opening);mid=(or_high+or_low)/2
        hm=bar['ts'][11:16]
        out.update(opening_range_high=or_high,opening_range_low=or_low,opening_range_mid=mid)
        if not '09:30'<=hm<'10:00':reasons.append('Outside the 09:30–10:00 breakout window')
        if close<=or_high:reasons.append('Close did not break strictly above the 09:15–09:30 opening range high')
        if vw is None or close<=vw:reasons.append('Close is not strictly above session VWAP')
        if rv is None or rv<p['min_rvol']:reasons.append(f"RVOL {rv if rv is not None else 'unavailable'} below {p['min_rvol']:.1f}x")
        raw_stop=max(mid,close-p['atr_stop_mult']*atr)
    elif name==LIQUIDITY_SWEEP:
        or_low=min(x['low'] for x in session[:3]);prior=bars[max(0,len(bars)-31):-1]
        swing=min((x['low'] for x in prior),default=or_low);levels=(or_low,swing)
        # A sweep may reclaim either level on the sweep candle or on exactly the next completed candle.
        hit=next((level for level in levels if bar['low']<level and close>level),None);sweep_low=bar['low'] if hit is not None else None
        if hit is None and len(bars)>1:
            prev=bars[-2];hit=next((level for level in levels if prev['low']<level and close>level),None)
            if hit is not None:sweep_low=prev['low']
        swept=hit is not None;level=hit if hit is not None else min(levels)
        out.update(level=level,opening_range_low=or_low,swing_low=swing,sweep_low=sweep_low if sweep_low is not None else bar['low'])
        if not swept:reasons.append('No fresh 15-minute opening-range or 30-candle low sweep/reclaim')
        if rv is None or rv<p['min_rvol']:reasons.append(f"RVOL {rv if rv is not None else 'unavailable'} below {p['min_rvol']:.1f}x")
        raw_stop=out['sweep_low']-p['atr_sweep_mult']*atr
    elif name==ORDER_FLOW_REVERSAL:
        if vw is None:reasons.append('Session VWAP unavailable')
        elif close<vw:reasons.append('Pullback candle closed below session VWAP')
        elif abs(close/vw-1)*100>p['pullback_vwap_pct']:reasons.append('Close is outside the VWAP pullback band')
        if daily_ema50 is None or close<=daily_ema50:reasons.append('Price is not above the daily 50 EMA')
        if depth_ratio is None or depth_ratio<p['max_depth_ratio']:reasons.append(f"Top-five bid/ask depth ratio below {p['max_depth_ratio']:.1f}:1 or unavailable")
        swing=min(x['low'] for x in bars[-4:-1]);raw_stop=min(swing,close-p['atr_stop_mult']*atr)
        out.update(daily_ema50=daily_ema50,pullback_swing_low=swing)
    else:
        bb=_bb([x['close'] for x in bars],p['bb_period'],p['bb_std'])
        widths=[]
        start=max(0,len(bars)-30)
        for i in range(start,len(bars)):
            band=_bb([x['close'] for x in bars[:i+1]],p['bb_period'],p['bb_std'])
            if band and band[2] is not None:widths.append(band[2])
        compressed=bool(bb and widths and bb[2]<=sorted(widths)[max(0,int(len(widths)*p['compression_percentile']/100)-1)])
        prev_atr=atrs[-2] if len(atrs)>1 else None
        if not compressed:reasons.append('Bollinger width is not at its lowest 15th percentile over 30 candles')
        if not finite(prev_atr) or atr>prev_atr+1e-9:reasons.append('ATR14 is not flat or declining')
        if not bb or close<=bb[0]:reasons.append('Close did not break above the upper Bollinger Band')
        if vw is None or close<=vw:reasons.append('Close is not above session VWAP')
        if rv is None or rv<p['min_rvol']:reasons.append(f"RVOL {rv if rv is not None else 'unavailable'} below {p['min_rvol']:.1f}x")
        raw_stop=min(bb[1],close-p['atr_stop_mult']*atr) if bb else close-p['atr_stop_mult']*atr
        out.update(bb_upper=bb[0] if bb else None,bb_lower=bb[1] if bb else None,bb_width_pct=bb[2] if bb else None,compressed=compressed)
    stop,stop_pct=_stop(close,raw_stop,p);risk=close-stop
    target=round_tick(close+p['target_r']*risk,p['tick_size'],'down')
    out.update(initial_stop=stop,initial_target=target,risk_reward=(target-close)/risk if risk>0 else None,stop_pct=stop_pct,
        status='REJECTED' if reasons else 'BUY_CANDIDATE',reasons=reasons,score=(rv or 0)*100+close)
    return out

def arm(fill,qty,signal,p,cost_per_share=0.,entry_at=None):
    risk=fill-signal['initial_stop'];return dict(side='long',entry=fill,qty=qty,original_qty=qty,stop=min(signal['initial_stop'],fill-.0001),initial_stop=signal['initial_stop'],
        target=round_tick(fill+p['target_r']*risk,p['tick_size'],'down'),initial_risk=risk,be_trigger=fill+p['breakeven_r']*risk,
        effective_breakeven=round_tick(fill+cost_per_share,p['tick_size'],'up'),stage='INITIAL_EXITS_ARMED',trail_active=False,best_price=fill,entry_at=entry_at,
        trail_atr_mult=p.get('trail_atr_mult',0.),last_bar_ts=signal.get('trigger_ts'))

def on_price(state,price,p,atr=None):
    state['best_price']=max(state.get('best_price',state['entry']),price);risk=state['initial_risk'];notes=[]
    if price<=state['stop']:return dict(kind='STOP_LOSS',quantity=state['qty'],order='stop',level=state['stop'],reason=f"Stop at {state['stop']:.2f}"),notes
    if price>=state['target']:return dict(kind='TARGET',quantity=state['qty'],order='limit',level=state['target'],reason=f"Target {p['target_r']:g}R at {state['target']:.2f}"),notes
    if price>=state['be_trigger'] and state['stop']<state['effective_breakeven']:
        state['stop']=state['effective_breakeven'];state['stage']='BREAKEVEN_ACTIVE';notes.append(('BREAKEVEN_ACTIVE','Stop moved to entry after fees'))
    mult=state.get('trail_atr_mult',0.)
    if mult and state['best_price']>=state['be_trigger'] and finite(atr) and atr>0:
        candidate=round_tick(state['best_price']-mult*atr,p['tick_size'],'down')
        if candidate>state['stop']:state['stop']=candidate;state['stage']='ATR_TRAILING';notes.append(('ATR_TRAILING',f"Stop trailed to {candidate:.2f}"))
    return None,notes
