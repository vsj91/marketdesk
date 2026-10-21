"""Vinay Intraday Trader: pure scan, sizing and exit rules for an intraday momentum-leader basket.

Designed separately from the EMA basket, which waits for a rare EMA9/21 crossover and then rejects almost every
one on structural risk-reward. This basket instead ranks the whole liquid universe on every completed 5-minute
candle and buys the strongest stocks of the day while their move is confirmed, with fixed, reachable exits.

Leader scan on the newest completed candle (all required, entries 09:35-13:30 IST):
  1  Up on the day: +min_day_change_pct .. +max_day_change_pct vs the previous close (moving, not near a circuit)
  2  Open gap at most max_gap_open_pct (big gaps tend to fade)
  3  Close above the 15-minute opening range high (the morning's buyers are winning)
  4  Close above session VWAP, but at most max_vwap_extension_pct above it (no chasing)
  5  Relative volume: today's volume so far >= min_rvol x the average of prior sessions at the same time
  6  The candle is green and closes in the top half of its range (buyers in control at the close)
Score = day change % x min(RVOL, 3); the best scores are bought first.

Exits (checked on every live price and every completed candle):
  stop     entry - clamp(atr_stop_mult x ATR14(5m), min_stop_pct, max_stop_pct)  (1R)
  target   optional: entry + target_r x R; half is sold there to lock profit, the rest keeps trailing
  breakeven optional: at +breakeven_r R the stop moves to entry plus round-trip costs
  trail    optional: from +trail_start_r R the stop follows the best price at trail_r x R behind (never loosens)
  VWAP     optional: a completed candle closing below session VWAP exits
  time     optional: no +0.5R within time_stop_candles candles exits
  clock    everything is squared off at square_off IST
Fees: round-trip Upstox intraday charges must be at most fee_max_pct of the position (so positions stay large), and
new entries stop for the month once that month's charges reach monthly_fee_budget.
The live engine and any replay call the same functions, so traded and tested rules cannot drift."""
import math
from datetime import datetime
from .indicators import atr_series,round_tick,finite
from . import ema_rules

# Defaults are the least-bad version of the 2022-2026 backtests (scripts/backtest_vinay_intraday.py): one trade a day in a
# stock that opened 1-2.5% higher and broke above its opening range and VWAP, a 2.5% stop, held to the square-off.
# Every version tested lost money after Upstox intraday charges; tighter stops and profit targets lost the most because
# these stocks swing about 1-1.5% either way within the day. A 0 for target_r, breakeven_r, trail_start_r or
# time_stop_candles switches that exit off.
DEFAULTS=dict(
    timeframe_minutes=5,opening_range_minutes=15,min_day_change_pct=.8,max_day_change_pct=4.,min_gap_open_pct=1.,max_gap_open_pct=2.5,
    max_vwap_extension_pct=10.,min_rvol=0.,rvol_sessions=5,min_close_location=0.,
    atr_period=14,atr_stop_mult=1.5,min_stop_pct=2.5,max_stop_pct=2.5,target_r=0.,breakeven_r=0.,trail_start_r=0.,trail_r=1.,
    time_stop_candles=0,time_stop_progress_r=.5,vwap_exit=False,max_chase_pct=.4,max_slippage_pct=.25,tick_size=.05,fee_max_pct=.1,
    monthly_fee_budget=500.,risk_pct=3.,max_position_pct=98.,max_positions=1,max_open_risk_pct=6.,max_entries_per_day=1,
    max_daily_loss_pct=2.,max_losing_trades=3,candidate_expiry_candles=1,
    entry_start='09:35',entry_cutoff='13:30',square_off='15:10',universe_size=60,min_price=50.,min_turnover_cr=20.,slippage_bps=5.)

LIMITS=dict(min_day_change_pct=(0.,3.),max_day_change_pct=(2.,8.),min_gap_open_pct=(-5.,3.),max_gap_open_pct=(.5,5.),max_vwap_extension_pct=(.3,10.),
    min_rvol=(0.,3.),atr_stop_mult=(.5,3.),min_stop_pct=(.3,3.),max_stop_pct=(.6,5.),target_r=(0.,4.),breakeven_r=(0.,2.),
    trail_start_r=(0.,3.),trail_r=(.5,2.),time_stop_candles=(0,36),vwap_exit=(True,False),max_chase_pct=(.1,1.),fee_max_pct=(.05,.5),
    monthly_fee_budget=(0.,5000.),risk_pct=(.1,3.),max_position_pct=(5.,100.),max_positions=(1,10),max_open_risk_pct=(.5,6.),
    max_entries_per_day=(1,20),max_daily_loss_pct=(.5,5.),max_losing_trades=(1,10),entry_cutoff=('11:00','14:30'),square_off=('15:00','15:20'),universe_size=(10,120))

def merged(cfg=None):return {**DEFAULTS,**(cfg or {})}

def day(bar):return bar['ts'][:10]
def hm(bar):return bar['ts'][11:16]

def vwap(bars):
    """Session VWAP from typical price; None without volume."""
    v=sum(b['volume'] for b in bars)
    return sum((b['high']+b['low']+b['close'])/3*b['volume'] for b in bars)/v if v>0 else None

def rvol(bars,today,cfg):
    """Today's cumulative volume through the newest candle's time vs the average of up to `rvol_sessions` prior
    sessions through the same time. None when fewer than 3 prior sessions are available."""
    upto=hm(bars[-1]);now=sum(b['volume'] for b in bars if day(b)==today)
    prior={}
    for b in bars:
        d=day(b)
        if d<today and hm(b)<=upto:prior[d]=prior.get(d,0)+b['volume']
    days=sorted(prior)[-cfg['rvol_sessions']:]
    if len(days)<3:return None
    avg=sum(prior[d] for d in days)/len(days)
    return now/avg if avg>0 else None

def evaluate(bars,cfg=None,prev_close=None):
    """Leader scan for the newest completed candle. Returns None when the candle is not today's tradeable session
    data (before the opening range completes or without history); otherwise status BUY_CANDIDATE or REJECTED with
    every failed rule in `reasons`, the evidence, the score and the planned stop distance."""
    cfg=merged(cfg)
    if len(bars)<cfg['atr_period']+2:return None
    today=day(bars[-1]);session=[b for b in bars if day(b)==today]
    or_bars=cfg['opening_range_minutes']//cfg['timeframe_minutes']
    if len(session)<=or_bars:return None
    if prev_close is None:
        before=[b for b in bars if day(b)<today]
        prev_close=before[-1]['close'] if before else None
    if not (finite(prev_close) and prev_close>0):return None
    bar=bars[-1];close=bar['close'];reasons=[]
    or_high=max(b['high'] for b in session[:or_bars]);or_low=min(b['low'] for b in session[:or_bars])
    vw=vwap(session);rv=rvol(bars,today,cfg);atr=atr_series(bars,cfg['atr_period'])[-1]
    change=(close/prev_close-1)*100;gap=(session[0]['open']/prev_close-1)*100
    rng=bar['high']-bar['low'];location=(close-bar['low'])/rng if rng>0 else .5
    out=dict(trigger_ts=bar['ts'],close=close,prev_close=prev_close,day_change_pct=change,gap_pct=gap,or_high=or_high,or_low=or_low,
        vwap=vw,vwap_extension_pct=(close/vw-1)*100 if vw else None,rvol=rv,atr14=atr,close_location=location)
    if change<cfg['min_day_change_pct']:reasons.append(f"Day change {change:+.2f}% below +{cfg['min_day_change_pct']:g}%")
    if change>cfg['max_day_change_pct']:reasons.append(f"Day change {change:+.2f}% above +{cfg['max_day_change_pct']:g}% (extended)")
    if gap<cfg['min_gap_open_pct']:reasons.append(f"Opened {gap:+.2f}% gapped, below {cfg['min_gap_open_pct']:+g}%")
    if gap>cfg['max_gap_open_pct']:reasons.append(f"Opened {gap:+.2f}% gapped up, above {cfg['max_gap_open_pct']:g}%")
    if not close>or_high:reasons.append(f"Close {close:.2f} not above the opening range high {or_high:.2f}")
    if vw is None:reasons.append('VWAP unavailable (no volume)')
    elif close<=vw:reasons.append(f"Close {close:.2f} not above VWAP {vw:.2f}")
    elif out['vwap_extension_pct']>cfg['max_vwap_extension_pct']:reasons.append(f"{out['vwap_extension_pct']:.2f}% above VWAP (chasing; max {cfg['max_vwap_extension_pct']:g}%)")
    if rv is None:reasons.append('Relative volume needs 3 prior sessions of candles')
    elif rv<cfg['min_rvol']:reasons.append(f"Relative volume {rv:.2f}x below {cfg['min_rvol']:g}x")
    if not close>bar['open']:reasons.append('Signal candle is not green')
    if location<cfg['min_close_location']:reasons.append(f"Closed in the lower {location*100:.0f}% of the candle (selling into strength)")
    if not (finite(atr) and atr>0):reasons.append('ATR14 unavailable');atr=None
    stop_pct=min(cfg['max_stop_pct'],max(cfg['min_stop_pct'],cfg['atr_stop_mult']*atr/close*100)) if atr else None
    out.update(stop_pct=stop_pct,score=change*min(rv or 0,3.),status='REJECTED' if reasons else 'BUY_CANDIDATE',reasons=reasons)
    return out

MISS_LABELS=(('Day change','day change outside the band'),('gapped','gap too big'),('opening range','not above the opening range'),
    ('chasing','too far above VWAP'),('VWAP','below VWAP'),('Relative volume','volume too low'),('green','red candle'),('lower','weak close'),('ATR','no ATR'))

def miss_label(reason):
    """Short bucket for a rejection reason, for the near-miss summary."""
    return next((label for key,label in MISS_LABELS if key in reason),'other')

def plan(entry,stop_pct,cfg):
    """Stop and target for an entry price: risk is stop_pct of the entry, the target is target_r x that risk."""
    cfg=merged(cfg);tick=cfg['tick_size']
    stop=round_tick(entry*(1-stop_pct/100),tick,'down');risk=entry-stop
    target=round_tick(entry+cfg['target_r']*risk,tick,'down') if cfg['target_r']>0 else None
    return dict(stop=stop,target=target,risk=risk,risk_reward=(target-entry)/risk if target and risk>0 else None)

def fee_check(entry,qty,cost,cfg,month_fees=0.):
    """None when the round-trip charges are a small enough share of the position and fit this month's fee budget."""
    cfg=merged(cfg)
    if qty<1:return 'Quantity zero'
    value=entry*qty;fees=cost(value)
    if fees>value*cfg['fee_max_pct']/100:return f"Round-trip charges ₹{fees:.0f} are {fees/value*100:.2f}% of a ₹{value:,.0f} position, above {cfg['fee_max_pct']:g}%"
    if month_fees+fees>cfg['monthly_fee_budget']:return f"Monthly fee budget: ₹{month_fees:.0f} spent + ₹{fees:.0f} would pass ₹{cfg['monthly_fee_budget']:.0f}"
    return None

def size(equity,cash,entry,stop,cfg,open_risk=0.,cost=None):
    """Same MIN(risk per trade, capital, portfolio heat) sizing as the EMA basket, with this basket's limits."""
    return ema_rules.size(equity,cash,entry,stop,merged(cfg),open_risk,cost)

def arm(fill,qty,stop_pct,cfg,cost_per_share=0.,entry_at=None):
    cfg=merged(cfg);levels=plan(fill,stop_pct,cfg);r=levels['risk'];tick=cfg['tick_size']
    return dict(side='long',entry=fill,qty=qty,original_qty=qty,stop=levels['stop'],initial_stop=levels['stop'],target=levels['target'],
        target_active=levels['target'] is not None,initial_risk=r,be_trigger=fill+cfg['breakeven_r']*r if cfg['breakeven_r']>0 else None,
        trail_trigger=fill+cfg['trail_start_r']*r if cfg['trail_start_r']>0 else None,progress_level=fill+cfg['time_stop_progress_r']*r,effective_breakeven=round_tick(fill+cost_per_share,tick,'up'),
        stage='INITIAL_EXITS_ARMED',trail_active=False,partial_done=False,best_price=fill,entry_at=entry_at,candles_held=0,exit_now=None)

def adopt(avg,qty,cfg):
    """Conservative exits for a holding without basket state (e.g. a manual buy)."""
    st=arm(avg,qty,merged(cfg)['max_stop_pct'],cfg);st.update(adopted=True,average_cost=avg,stage='ADOPTED')
    return st

def on_price(state,price,cfg):
    """One live price. Returns (intent, notes); stop upgrades are applied to `state` immediately and never loosen."""
    cfg=merged(cfg);notes=[];r=state['initial_risk']
    if price>state['best_price']:state['best_price']=price
    if state.get('exit_now'):
        kind,why=state['exit_now']
        return dict(kind=kind,quantity=state['qty'],order='market',level=None,reason=why),notes
    if price<=state['stop']:
        kind='TRAILING_STOP' if state['trail_active'] else 'BREAKEVEN_STOP' if state['stage']=='BREAKEVEN_ACTIVE' else 'STOP_LOSS'
        return dict(kind=kind,quantity=state['qty'],order='stop',level=state['stop'],reason=f"{kind.replace('_',' ').title()} at {state['stop']:.2f} (observed {price:.2f})"),notes
    if state['target_active'] and price>=state['target']:
        half=math.floor(state['qty']/2)
        if not state['partial_done'] and half>=1:
            return dict(kind='PARTIAL',quantity=half,order='limit',level=state['target'],reason=f"Profit target {state['target']:.2f} ({cfg['target_r']:g}R): booking {half} of {state['qty']}, the rest trails (observed {price:.2f})"),notes
        return dict(kind='TARGET',quantity=state['qty'],order='limit',level=state['target'],reason=f"Profit target {state['target']:.2f} ({cfg['target_r']:g}R) reached (observed {price:.2f})"),notes
    if state['stage'] in ('INITIAL_EXITS_ARMED','ADOPTED') and state['be_trigger'] is not None and price>=state['be_trigger']:
        if state['effective_breakeven']>state['stop']:state['stop']=state['effective_breakeven']
        state['stage']='BREAKEVEN_ACTIVE'
        notes.append(('BREAKEVEN_ACTIVE',f"Price {price:.2f} reached +{cfg['breakeven_r']:g}R ({state['be_trigger']:.2f}); stop moved to breakeven after costs {state['stop']:.2f}"))
    if state['trail_trigger'] is not None and state['best_price']>=state['trail_trigger']:
        candidate=round_tick(state['best_price']-cfg['trail_r']*r,cfg['tick_size'],'down')
        if not state['trail_active']:
            state['trail_active']=True;state['stage']='PROFIT_TRAILING'
            notes.append(('PROFIT_TRAILING',f"+{cfg['trail_start_r']:g}R reached: stop now trails {cfg['trail_r']:g}R ({r:.2f}) behind the best price"))
        if candidate>state['stop']:
            notes.append(('TRAIL_UPDATE',f"Trailing stop {state['stop']:.2f} -> {candidate:.2f} (best {state['best_price']:.2f})"));state['stop']=candidate
    return None,notes

def on_bar(state,bars,cfg):
    """After each completed candle since entry: VWAP-break and time-stop checks. Sets state['exit_now'] so the
    next price observation exits at market; returns transition notes."""
    cfg=merged(cfg);notes=[]
    if state.get('exit_now') or not bars or state['qty']<=0:return notes
    entered=datetime.fromisoformat(state['entry_at']) if state.get('entry_at') else None
    today=day(bars[-1]);session=[b for b in bars if day(b)==today]
    after=[b for b in session if entered is None or datetime.fromisoformat(b['ts'])>=entered.replace(second=0,microsecond=0)]
    state['candles_held']=len(after)
    vw=vwap(session);close=bars[-1]['close']
    if cfg['vwap_exit'] and after and vw and close<vw:
        state['exit_now']=['VWAP_EXIT',f"Candle {bars[-1]['ts'][11:16]} closed {close:.2f} below session VWAP {vw:.2f}: intraday trend broke"]
    elif cfg['time_stop_candles'] and len(after)>=cfg['time_stop_candles'] and state['best_price']<state['progress_level']:
        state['exit_now']=['TIME_STOP',f"No +{cfg['time_stop_progress_r']:g}R ({state['progress_level']:.2f}) within {cfg['time_stop_candles']} candles: momentum did not follow through"]
    if state.get('exit_now'):notes.append(('EXIT_SIGNALLED',state['exit_now'][1]))
    return notes

def apply_fill(state,intent,filled,price,cfg):
    filled=min(filled,state['qty']);state['qty']-=filled
    if intent['kind']=='PARTIAL':
        state['partial_done']=True;state['target_active']=False
        if state['qty']>0:
            state['stage']='TARGET_HALF_BOOKED'
            return [('TARGET_HALF_BOOKED',f"Booked {filled} at {price:.2f}; {state['qty']} left trailing with stop {state['stop']:.2f}")]
    if state['qty']<=0:state['stage']='CLOSED';return [('CLOSED',f"{intent['kind'].replace('_',' ').title()}: {filled} at {price:.2f}")]
    return [(state['stage'],f"Sold {filled} at {price:.2f}; {state['qty']} left under the same exits")]
