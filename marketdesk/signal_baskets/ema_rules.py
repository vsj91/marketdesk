"""EMA + Price Action Trader: pure signal, sizing and exit-state-machine rules.

The live engine (signal_baskets.engine) and the backtest (scripts/backtest_signal_baskets.py) call the
same functions, so tested and traded rules cannot drift. Inputs are completed candles only; the
newest bar in `bars` is the trigger candle and must already be closed when this runs.

Long setup (all required):
  A  EMA9 crosses above EMA21: EMA9[prev] <= EMA21[prev] and EMA9[now] > EMA21[now]
  B  close > EMA200
  C  rsi_min <= RSI14 <= rsi_max (default 55-70)
  D  Wilder ATR14 valid and ATR% of price inside [min_atr_pct, max_atr_pct]
  E  confirmed swing structure: a support below the trigger candle for the stop, and enough room
     below the next overhead resistance for the target to clear min_rr
Entry is a stop-limit: trigger = high x (1 + entry_buffer_pct), limit = trigger x (1 + limit_protection_pct).
Shorts mirror these rules with their own RSI band and are research-only (see SHORT_LIVE_REASON)."""
import math
from .indicators import ema_series,rsi_series,atr_series,structure,round_tick,finite

DEFAULTS=dict(
    timeframe_minutes=5,ema_fast=9,ema_slow=21,ema_trend=200,ema_trail=20,rsi_period=14,rsi_min=55.,rsi_max=70.,
    atr_period=14,min_atr_pct=.10,max_atr_pct=1.20,
    entry_buffer_pct=.05,limit_protection_pct=.30,signal_expiry_candles=3,max_gap_pct=.50,max_slippage_pct=.25,
    tick_size=.05,stop_method='hybrid',stop_tick_buffer=3,atr_stop_mult=1.5,max_stop_atr=3.,max_stop_pct=3.,
    atr_target_mult=3.,target_tick_buffer=2,allow_no_resistance=True,min_rr=1.8,   # spec default 2.0 is unreachable: see LIMITATIONS
    breakeven_atr_mult=1.5,partial_atr_mult=2.5,partial_fraction=.5,min_partial_qty=2,cancel_target_after_partial=True,
    trail_buffer_mode='ticks',trail_buffer_ticks=2,trail_buffer_atr=.25,
    risk_pct=1.,max_position_pct=25.,max_positions=4,max_open_risk_pct=3.,max_entries_per_day=6,
    entry_start='09:30',entry_cutoff='14:45',square_off='15:12',swing_k=3,structure_lookback=150,
    universe_size=50,min_price=50.,min_turnover_cr=20.,slippage_bps=5.,
    allow_short=False,short_rsi_min=30.,short_rsi_max=45.)

# Allowed ranges for configurable parameters; the API rejects anything outside them.
LIMITS=dict(timeframe_minutes=(5,15,30),entry_buffer_pct=(.05,.10),stop_tick_buffer=(2,5),min_rr=(1.5,2.),
    stop_method=('structural','atr','hybrid'),rsi_min=(40.,70.),rsi_max=(55.,85.),min_atr_pct=(.01,2.),max_atr_pct=(.05,5.),
    limit_protection_pct=(.05,1.),signal_expiry_candles=(1,12),max_gap_pct=(.1,3.),risk_pct=(.1,2.),max_position_pct=(5.,50.),
    max_positions=(1,10),max_open_risk_pct=(.5,6.),universe_size=(5,120),trail_buffer_mode=('ticks','atr'),
    trail_buffer_ticks=(1,10),trail_buffer_atr=(.05,1.),cancel_target_after_partial=(True,False),allow_no_resistance=(True,False),
    entry_cutoff=('14:00','15:00'),square_off=('15:00','15:14'),slippage_bps=(0.,50.))

SHORT_LIVE_REASON=('Short entries are research-only: the paper ledger holds long positions only (quantity >= 0, '
    'no borrow or MIS short product), so live short selling is not supported safely.')

STATES=('SIGNAL_DETECTED','ENTRY_PENDING','ENTRY_FILLED','INITIAL_EXITS_ARMED','BREAKEVEN_ACTIVE',
    'PARTIAL_PROFIT_BOOKED','EMA_TRAILING_ACTIVE','CLOSED','EXPIRED','REJECTED')

def merged(cfg=None):return {**DEFAULTS,**(cfg or {})}

def indicators(bars,cfg):
    closes=[b['close'] for b in bars]
    return dict(fast=ema_series(closes,cfg['ema_fast']),slow=ema_series(closes,cfg['ema_slow']),trend=ema_series(closes,cfg['ema_trend']),
        trail=ema_series(closes,cfg['ema_trail']),rsi=rsi_series(closes,cfg['rsi_period']),atr=atr_series(bars,cfg['atr_period']))

def crossover(ind,i,side='long'):
    f0,s0,f1,s1=ind['fast'][i-1],ind['slow'][i-1],ind['fast'][i],ind['slow'][i]
    if None in (f0,s0,f1,s1):return False
    return f0<=s0 and f1>s1 if side=='long' else f0>=s0 and f1<s1

def evaluate(bars,cfg=None,side='long'):
    """Decision for the newest completed bar. Returns None when there is no crossover (nothing to log),
    otherwise a dict with status SIGNAL_DETECTED or REJECTED, every failed condition in `reasons`,
    and the full audit trail (EMAs, RSI, ATR, structure, trigger, stop, target, RR)."""
    cfg=merged(cfg);n=len(bars)
    if n<cfg['ema_trend']+2:return None
    ind=indicators(bars,cfg);i=n-1;bar=bars[i]
    if not crossover(ind,i,side):return None
    long=side=='long';tick=cfg['tick_size'];reasons=[]
    atr=ind['atr'][i];rsi=ind['rsi'][i];close=bar['close'];trend=ind['trend'][i]
    out=dict(side=side,trigger_ts=bar.get('ts'),trigger_high=bar['high'],trigger_low=bar['low'],close=close,
        ema9=ind['fast'][i],ema21=ind['slow'][i],ema200=trend,ema20=ind['trail'][i],rsi14=rsi,atr14=atr,
        atr_pct=atr/close*100 if finite(atr) and close>0 else None,crossover=True)
    if long and not close>trend:reasons.append(f'Close {close:.2f} is not above EMA200 {trend:.2f}')
    if not long and not close<trend:reasons.append(f'Close {close:.2f} is not below EMA200 {trend:.2f}')
    lo,hi=(cfg['rsi_min'],cfg['rsi_max']) if long else (cfg['short_rsi_min'],cfg['short_rsi_max'])
    if rsi is None:reasons.append('RSI14 unavailable')
    elif rsi<lo:reasons.append(f'RSI14 {rsi:.1f} below {lo:g}')
    elif rsi>hi:reasons.append(f'RSI14 {rsi:.1f} above {hi:g}')
    if not (finite(atr) and atr>0):
        reasons.append('ATR14 unavailable or invalid');out.update(status='REJECTED',reasons=reasons);return out
    if out['atr_pct']<cfg['min_atr_pct']:reasons.append(f"ATR {out['atr_pct']:.2f}% of price below {cfg['min_atr_pct']:g}% (too quiet)")
    if out['atr_pct']>cfg['max_atr_pct']:reasons.append(f"ATR {out['atr_pct']:.2f}% of price above {cfg['max_atr_pct']:g}% (too volatile)")
    if long:
        entry=round_tick(bar['high']*(1+cfg['entry_buffer_pct']/100),tick,'up');limit=round_tick(entry*(1+cfg['limit_protection_pct']/100),tick,'up')
    else:
        entry=round_tick(bar['low']*(1-cfg['entry_buffer_pct']/100),tick,'down');limit=round_tick(entry*(1-cfg['limit_protection_pct']/100),tick,'down')
    s=structure(bars,cfg['swing_k'],cfg['structure_lookback'])
    out.update(entry_trigger=entry,limit_price=limit,structure_tolerance=s['tolerance'])
    # Stop: structural level beyond the trigger candle, ATR reference, or the more conservative of both.
    if long:
        support=max((z for z in s['supports'] if z['low']<bar['low']),key=lambda z:z['low'],default=None)
        structural=round_tick(support['low']-cfg['stop_tick_buffer']*tick,tick,'down') if support else None
        atr_stop=round_tick(entry-cfg['atr_stop_mult']*atr,tick,'down')
    else:
        support=min((z for z in s['resistances'] if z['high']>bar['high']),key=lambda z:z['high'],default=None)
        structural=round_tick(support['high']+cfg['stop_tick_buffer']*tick,tick,'up') if support else None
        atr_stop=round_tick(entry+cfg['atr_stop_mult']*atr,tick,'up')
    out.update(structural_level=support['low' if long else 'high'] if support else None,structural_touches=support['touches'] if support else 0,
        structural_stop=structural,atr_stop=atr_stop)
    method=cfg['stop_method']
    if method in ('structural','hybrid') and structural is None:
        reasons.append('No confirmed swing '+('low/support' if long else 'high/resistance')+' beyond the trigger candle for the structural stop')
        stop=atr_stop if method=='hybrid' else None
    elif method=='structural':stop=structural
    elif method=='atr':stop=atr_stop
    else:stop=min(structural,atr_stop) if long else max(structural,atr_stop)
    out.update(stop_method=method,initial_stop=stop)
    if stop is None:out.update(status='REJECTED',reasons=reasons);return out
    risk=(entry-stop) if long else (stop-entry)
    if risk<=0:reasons.append('Stop is not beyond the entry trigger')
    else:
        if risk/atr>cfg['max_stop_atr']:reasons.append(f"Stop distance {risk/atr:.2f} ATR exceeds {cfg['max_stop_atr']:g} ATR")
        if risk/entry*100>cfg['max_stop_pct']:reasons.append(f"Stop distance {risk/entry*100:.2f}% exceeds {cfg['max_stop_pct']:g}%")
    # Target: ATR reference, capped below the next verified overhead resistance (above support for shorts).
    atr_target=round_tick(entry+cfg['atr_target_mult']*atr,tick,'down') if long else round_tick(entry-cfg['atr_target_mult']*atr,tick,'up')
    if long:
        zone=min((z for z in s['resistances'] if z['low']>entry),key=lambda z:z['low'],default=None)
        structural_target=round_tick(zone['low']-cfg['target_tick_buffer']*tick,tick,'down') if zone else None
        target=min(atr_target,structural_target) if zone else atr_target
    else:
        zone=max((z for z in s['supports'] if z['high']<entry),key=lambda z:z['high'],default=None)
        structural_target=round_tick(zone['high']+cfg['target_tick_buffer']*tick,tick,'up') if zone else None
        target=max(atr_target,structural_target) if zone else atr_target
    if zone is None and not cfg['allow_no_resistance']:reasons.append('No verified '+('overhead resistance' if long else 'support')+' and the configuration requires one')
    reward=(target-entry) if long else (entry-target)
    rr=reward/risk if risk>0 else None
    out.update(atr_target=atr_target,resistance_level=zone['low' if long else 'high'] if zone else None,structural_target=structural_target,
        initial_target=target,risk_per_share=risk,reward_per_share=reward,risk_reward=rr)
    if reward<=0:reasons.append(('Overhead resistance' if long else 'Support')+f" at {out['resistance_level']} is at or inside the entry; no room for a target")
    elif rr is not None and rr<cfg['min_rr']-1e-9:
        why='resistance too close' if zone and target==structural_target and structural_target!=atr_target else 'stop too wide for the ATR target'
        reasons.append(f"Risk-reward {rr:.2f} below {cfg['min_rr']:g} ({why})")
    out.update(status='REJECTED' if reasons else 'SIGNAL_DETECTED',reasons=reasons)
    return out

def still_valid(bars,cfg,side='long'):
    """Re-check a pending entry on each new completed candle: the EMA9/21 order and the EMA200 side must hold."""
    cfg=merged(cfg);ind=indicators(bars,cfg);i=len(bars)-1
    f,s,t=ind['fast'][i],ind['slow'][i],ind['trend'][i]
    if None in (f,s,t):return 'Indicators unavailable'
    close=bars[-1]['close']
    if side=='long' and not (f>s and close>t):return 'EMA9 fell back below EMA21 or close below EMA200 before the entry filled'
    if side=='short' and not (f<s and close<t):return 'EMA9 rose back above EMA21 or close above EMA200 before the entry filled'
    return None

def size(equity,cash,entry,stop,cfg,open_risk=0.,cost=None):
    """Quantity = MIN(risk, capital, portfolio-heat) limits. Risk per share includes slippage on both legs;
    fixed per-order charges come from `cost(value)` (round-trip charges in rupees), so costs reduce size."""
    cfg=merged(cfg);risk=abs(entry-stop)
    if not (finite(equity) and equity>0 and risk>0 and entry>0):return dict(quantity=0,binding='invalid inputs')
    slip=entry*cfg['slippage_bps']/1e4*2;per=risk+slip
    budget=equity*cfg['risk_pct']/100;heat=equity*cfg['max_open_risk_pct']/100-open_risk
    cap=min(cash,equity*cfg['max_position_pct']/100)
    def fits(q,limit):return q*per+(cost(q*entry) if cost else 0)<=limit
    def largest(limit):
        q=max(0,math.floor(limit/per))
        while q>0 and not fits(q,limit):q-=1
        return q
    by_risk=largest(budget);by_heat=largest(heat) if heat>0 else 0
    by_capital=max(0,math.floor(cap/(entry*(1+cfg['slippage_bps']/1e4)*1.001)))
    qty=min(by_risk,by_capital,by_heat)
    binding=min((('risk_per_trade',by_risk),('capital',by_capital),('portfolio_risk',by_heat)),key=lambda x:x[1])[0]
    return dict(quantity=qty,by_risk=by_risk,by_capital=by_capital,by_heat=by_heat,binding=binding,risk_budget=budget,
        risk_per_share=per,planned_risk=qty*per+(cost(qty*entry) if cost and qty else 0))

# ---- exit state machine ---------------------------------------------------------------------------

def arm(signal,fill_price,qty,cfg,cost_per_share=0.):
    """INITIAL_EXITS_ARMED state after an entry fill at the actual average price. The protective stop
    and target are the planned levels; risk is recomputed from the real fill."""
    cfg=merged(cfg);long=signal['side']=='long';d=1 if long else -1;atr=signal['atr14'];tick=cfg['tick_size']
    stop=signal['initial_stop'];risk=(fill_price-stop)*d
    be_trigger=fill_price+d*max(cfg['breakeven_atr_mult']*atr,risk)          # the later of 1.5 ATR and 1R
    partial=fill_price+d*cfg['partial_atr_mult']*atr
    partial=max(partial,be_trigger) if long else min(partial,be_trigger)     # partial needs breakeven eligibility
    effective_be=round_tick(fill_price+d*cost_per_share,tick,'up' if long else 'down')
    return dict(side=signal['side'],entry=fill_price,qty=qty,original_qty=qty,entry_atr=atr,initial_stop=stop,stop=stop,
        target=signal['initial_target'],target_active=True,initial_risk=risk,be_trigger=be_trigger,partial_trigger=partial,
        effective_breakeven=effective_be,stage='INITIAL_EXITS_ARMED',partial_done=False,trail_active=False,
        best_price=fill_price,history=[])

def _better(state,a,b):return a>b if state['side']=='long' else a<b

def on_price(state,price,cfg):
    """One price observation. Returns (intent, notes): intent is None or a dict(kind, quantity, reason,
    order='stop'|'limit') to execute; stop upgrades (breakeven) are applied to `state` immediately.
    Stops never move backward. A fill must be confirmed with apply_fill before quantities change."""
    cfg=merged(cfg);long=state['side']=='long';notes=[]
    if _better(state,price,state['best_price']):state['best_price']=price
    hit=lambda level:price<=level if long else price>=level
    reached=lambda level:price>=level if long else price<=level
    if hit(state['stop']):
        kind='BREAKEVEN_STOP' if state['stage']=='BREAKEVEN_ACTIVE' else 'TRAILING_STOP' if state['trail_active'] else 'STOP_LOSS'
        return dict(kind=kind,quantity=state['qty'],order='stop',level=state['stop'],reason=f"{kind.replace('_',' ').title()} at {state['stop']:.2f} (observed {price:.2f})"),notes
    if state['target_active'] and reached(state['target']):
        return dict(kind='TARGET',quantity=state['qty'],order='limit',level=state['target'],reason=f"Target {state['target']:.2f} reached (observed {price:.2f})"),notes
    if state['stage']=='INITIAL_EXITS_ARMED' and reached(state['be_trigger']):
        new=state['effective_breakeven'];moved=_better(state,new,state['stop'])
        if moved:state['stop']=new
        state['stage']='BREAKEVEN_ACTIVE';notes.append(('BREAKEVEN_ACTIVE',f"Price {price:.2f} reached {state['be_trigger']:.2f} (max of {cfg['breakeven_atr_mult']:g} ATR and 1R); stop "+(f"moved to effective breakeven {new:.2f}" if moved else f"kept at {state['stop']:.2f} (already beyond breakeven)")))
    if not state['partial_done'] and state['stage'] in ('BREAKEVEN_ACTIVE',) and reached(state['partial_trigger']):
        q=math.floor(state['original_qty']*cfg['partial_fraction'])
        q=min(q,state['qty'])
        if state['original_qty']<cfg['min_partial_qty'] or q<1:
            # Too small to split: no partial sale; trail the whole position from here (deterministic fallback).
            state['partial_done']=True;state['trail_active']=True;state['stage']='EMA_TRAILING_ACTIVE'
            if cfg['cancel_target_after_partial']:state['target_active']=False
            notes.append(('EMA_TRAILING_ACTIVE',f"{state['original_qty']} share(s) cannot be split; partial booking skipped, EMA{cfg['ema_trail']} trailing starts on the full position"))
        else:
            return dict(kind='PARTIAL',quantity=q,order='limit',level=state['partial_trigger'],reason=f"Partial profit: {q} of {state['original_qty']} at {cfg['partial_atr_mult']:g} ATR ({state['partial_trigger']:.2f})"),notes
    return None,notes

def apply_fill(state,intent,filled,price,cfg):
    """Book an executed exit. Partial fills of a full exit leave the rest under the same protection."""
    cfg=merged(cfg);filled=min(filled,state['qty']);state['qty']-=filled;notes=[]
    if intent['kind']=='PARTIAL':
        state['partial_done']=True;state['trail_active']=True;state['stage']='PARTIAL_PROFIT_BOOKED'
        if cfg['cancel_target_after_partial']:state['target_active']=False
        notes.append(('PARTIAL_PROFIT_BOOKED',f"Sold {filled} at {price:.2f}; {state['qty']} left"))
        if state['qty']>0:
            state['stage']='EMA_TRAILING_ACTIVE';notes.append(('EMA_TRAILING_ACTIVE',f"Remaining {state['qty']} trail behind EMA{cfg['ema_trail']}"+('; fixed target cancelled' if not state['target_active'] else '')))
    if state['qty']<=0:state['stage']='CLOSED';notes.append(('CLOSED',f"{intent['kind'].replace('_',' ').title()}: last {filled} at {price:.2f}"))
    return notes

def on_bar(state,bars,cfg):
    """After each completed candle: ratchet the EMA20 trailing stop (never loosens)."""
    cfg=merged(cfg);long=state['side']=='long';notes=[]
    if not state['trail_active'] or state['qty']<=0:return notes
    ema=ema_series([b['close'] for b in bars],cfg['ema_trail'])[-1]
    if ema is None:return notes
    buffer=cfg['trail_buffer_ticks']*cfg['tick_size'] if cfg['trail_buffer_mode']=='ticks' else cfg['trail_buffer_atr']*state['entry_atr']
    candidate=round_tick(ema-buffer,cfg['tick_size'],'down') if long else round_tick(ema+buffer,cfg['tick_size'],'up')
    if _better(state,candidate,state['stop']):
        notes.append(('TRAIL_UPDATE',f"Trailing stop {state['stop']:.2f} -> {candidate:.2f} (EMA{cfg['ema_trail']} {ema:.2f} - buffer {buffer:.2f})"));state['stop']=candidate
    return notes

# ---- OHLC simulation for the backtest (conservative intrabar path) ----------------------------------

def simulate_bar(state,bar,cfg,slippage):
    """Apply one completed OHLC bar to an open position. Without the tick path the adverse ordering is assumed:
    a gap through the stop fills at the open; a stop touched in the same bar as a target counts as the stop;
    after an intrabar breakeven/partial upgrade, a low at or through the upgraded stop counts as hit.
    Returns a list of exits (kind, quantity, price) and notes."""
    cfg=merged(cfg);long=state['side']=='long';exits=[];notes=[]
    adverse=(bar['low'] if long else bar['high']);favor=(bar['high'] if long else bar['low'])
    def stop_fill(level,opened):
        base=min(level,opened) if long else max(level,opened)   # gap through the stop fills at the worse open
        return base*(1-slippage) if long else base*(1+slippage)
    if (adverse<=state['stop'] if long else adverse>=state['stop']):
        intent,_=on_price(state,state['stop'],cfg)
        price=stop_fill(state['stop'],bar['open']);exits.append((intent['kind'],state['qty'],price));notes+=apply_fill(state,intent,state['qty'],price,cfg)
        return exits,notes
    for level in sorted({state['be_trigger'],state['partial_trigger'],state['target'] if state['target_active'] else None}-{None},reverse=not long):
        if not (favor>=level if long else favor<=level) or state['qty']<=0:continue
        intent,n=on_price(state,level,cfg);notes+=n
        if intent and intent['kind'] in ('TARGET','PARTIAL'):
            price=level   # limit exits fill at their level, never at a better gap price
            exits.append((intent['kind'],intent['quantity'],price));notes+=apply_fill(state,intent,intent['quantity'],price,cfg)
    if state['qty']>0 and (adverse<=state['stop'] if long else adverse>=state['stop']):
        intent,_=on_price(state,state['stop'],cfg)
        price=state['stop']*(1-slippage) if long else state['stop']*(1+slippage)
        exits.append((intent['kind'],state['qty'],price));notes+=apply_fill(state,intent,state['qty'],price,cfg)
    return exits,notes
