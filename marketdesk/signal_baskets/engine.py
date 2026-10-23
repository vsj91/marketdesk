"""Live paper engine for the signal baskets. Called every 15 seconds by the auto-trading loop while the
connected feed reports an open NSE session, exactly like the other baskets.

Every order goes through paper.trade with an automatic guard: a fresh trade price (<= 30 s), real bid/ask depth
(<= 5 s, 10% participation, 0.5% price band), data-quality and Nifty 500 checks for buys, and the per-basket
fee limit. The guard runs inside the fill transaction and writes the position state and state-machine transition
there, so a fill, its stop/target and its audit row commit together or not at all. Paper only: nothing here
calls a broker order endpoint.

Pipeline: data (signal_baskets.runner) -> validation -> strategy signal (sb_signals) -> risk checks (this module)
-> paper order (paper.trade) -> fill simulation (depth IOC) -> exit monitoring (this module) -> portfolio update."""
import json,math,uuid
from datetime import datetime,timedelta,timezone
from ..storage import connect,get_state,set_state,now
from ..quote_health import fresh,refresh_held_quotes
from ..buy_universe import require_buy_universe,allowed_symbols
from ..execution import intraday_charges,charges
from .. import drawdown
from ..sectors import sector_group
from . import ema_rules,news_rules,vinay_rules,live_bars,intraday_rules,sector_rules
from .names import NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION,VINAY_INTRADAY,SIGNAL_BASKETS,TITLES,STRATEGY_VERSIONS,NEW_INTRADAY_BASKETS,LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION,ORB_VOLUME_BREAKOUT,SECTOR_RELATIVE_STRENGTH
from .schema import init as init_schema

IST=timezone(timedelta(hours=5,minutes=30))
DEFAULTS={NEWS_MOMENTUM:news_rules.MOMENTUM_DEFAULTS,NEWS_CATALYST:news_rules.CATALYST_DEFAULTS,EMA_PRICE_ACTION:ema_rules.DEFAULTS,VINAY_INTRADAY:vinay_rules.DEFAULTS}
LIMITS={NEWS_MOMENTUM:news_rules.MOMENTUM_LIMITS,NEWS_CATALYST:news_rules.CATALYST_LIMITS,EMA_PRICE_ACTION:ema_rules.LIMITS,VINAY_INTRADAY:vinay_rules.LIMITS}
for _n in NEW_INTRADAY_BASKETS:DEFAULTS[_n]=intraday_rules.DEFAULTS[_n];LIMITS[_n]=intraday_rules.LIMITS[_n]
DEFAULTS[SECTOR_RELATIVE_STRENGTH]=sector_rules.DEFAULTS;LIMITS[SECTOR_RELATIVE_STRENGTH]=sector_rules.LIMITS
LATEST_EXIT='15:14'   # Upstox's MIS auto square-off (Rs 75 + GST) starts at 15:15

def final_exit(p):
    """Last-chance market exit: 4 minutes after the basket's square-off, never later than 15:14."""
    h,m=map(int,p['square_off'].split(':'));t=h*60+m+4
    return min(f'{t//60:02d}:{t%60:02d}',LATEST_EXIT)
WINDOWS={NEWS_MOMENTUM:('09:20','15:15'),NEWS_CATALYST:('09:20','15:15')}

def session(clock):return clock.astimezone(IST).date().isoformat()
def local_hm(clock):return clock.astimezone(IST).strftime('%H:%M')

def params(name,cfg):
    """Strategy parameters: defaults overlaid with the saved, validated `params` of this basket only."""
    out=dict(DEFAULTS[name]);out.update({k:v for k,v in ((cfg or {}).get('params') or {}).items() if k in out})
    return out

def validate_params(name,changes):
    """Reject unknown keys and values outside LIMITS; returns the cleaned dict."""
    if not isinstance(changes,dict):raise ValueError('Parameters must be an object.')
    limits=LIMITS[name];clean={}
    for k,v in changes.items():
        if k not in limits:raise ValueError(f'{k} is not configurable for {TITLES[name]}.')
        allowed=limits[k]
        if isinstance(allowed[0],(bool,str)) or name==EMA_PRICE_ACTION and k=='timeframe_minutes':
            if k in ('entry_cutoff','square_off'):
                if not (isinstance(v,str) and len(v)==5 and allowed[0]<=v<=allowed[1]):raise ValueError(f'{k} must be HH:MM between {allowed[0]} and {allowed[1]}.')
            elif v not in allowed:raise ValueError(f'{k} must be one of {", ".join(map(str,allowed))}.')
        else:
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not allowed[0]<=v<=allowed[1]:raise ValueError(f'{k} must be between {allowed[0]} and {allowed[1]}.')
            if isinstance(allowed[0],int) and not isinstance(allowed[0],bool):v=int(v)
        clean[k]=v
    if name==EMA_PRICE_ACTION and 'allow_short' in changes:raise ValueError(ema_rules.SHORT_LIVE_REASON)
    return clean

RULE_TEXT={
 NEWS_MOMENTUM:dict(family='Event-driven swing trading (1-20 sessions)',
   entry='A verified, positive NSE filing (order win, rating upgrade, capacity, approval, acquisition, insider buying) with a News Impact Score at or above the threshold becomes a watchlist candidate. The first completed session at or after the news must confirm it: positive but not excessive price reaction, relative volume, close > EMA20 > EMA50, ATR band, relative strength against Nifty 500, the market regime and room to the next resistance. Confirmed candidates buy during the next session at a live price inside the gap limit. The score alone never trades.',
   exit='Stop 2 ATR below the fill, target 4 ATR or the next resistance, chandelier trail 2.5 ATR under the best close, exit on a completed close below EMA20, on a verified negative filing, after 20 sessions, or after 5 sessions without a +1R close.',
   risk='1% of basket equity at risk per trade from the actual stop distance, at most 15% of equity per position, 6 positions, 2 per sector and 3 new buys a session; a 15% fall from the equity peak pauses new buys.'),
 NEWS_CATALYST:dict(family='Fundamental event-driven investing (3-24 months)',
   entry='A verified business catalyst (orders, capacity, approvals, rating upgrade, debt reduction, acquisition, results) plus an explainable Investor Opportunity Score (catalyst 25%, earnings 20%, financial quality 20%, valuation 15%, management/execution 10%, risk 10%; initial hypotheses). Requires the score threshold, ROE, debt/equity, interest cover, P/E limits, liquidity, no critical data gaps and sector room. Buys from the session after the catalyst.',
   exit='Reviewed weekly: governance events (auditor resignation, default, fraud, regulatory action), thesis failure in a newer reported period, score deterioration, excessive valuation, a catalyst that does not show up in results within 270 days, sector concentration above 40%, or a 25% loss from cost. Negative sentiment alone never sells.',
   risk='Up to 10% of starting capital per company, 10 holdings, 30% per sector for new buys, 2 new buys a session; the drawdown breaker pauses new buys.'),
 EMA_PRICE_ACTION:dict(family='Technical momentum / price action (intraday, MIS-style)',
   entry='Completed 5-minute candles: EMA9 crosses above EMA21, close above EMA200, RSI14 55-70, ATR14 % of price inside the band, and a confirmed swing support below the trigger candle. Entry is a paper stop-limit 0.05% above the trigger candle high with limit protection, gap protection and expiry after 3 candles.',
   exit='Hybrid initial stop MIN(structural support - 3 ticks, entry - 1.5 ATR); target MIN(entry + 3 ATR, next resistance - 2 ticks) with RR >= 1.8 (configurable 1.5-2.0). Breakeven after the later of 1.5 ATR and 1R, 50% partial at 2.5 ATR, then EMA20 trailing for the rest. Square-off of everything at 15:00 IST.',
   risk='1% of basket equity at risk per trade including costs and slippage, 25% of equity per position, 4 positions, 3% total open risk, 6 entries a session; no new entries after 14:45 IST. Long only: short rules exist for research but are not traded live.'),
 VINAY_INTRADAY:dict(family='Intraday gap-up leaders (minutes to hours, MIS-style, flat by the close)',
   entry='Every completed 5-minute candle from 09:35 to 14:30 IST checks every Nifty 500 stock on 5-minute candles built from the live price feed. A stock qualifies when it opened 1-2.5% above the previous close, is up 0.8-4% on the day, and closes above its 15-minute opening range high and above session VWAP. The strongest one is bought at the live price, within 0.4% of the signal close. At most one trade a day and one stock per day.',
   exit='Stop loss 2.5% below the entry. No fixed profit target by default: the position is sold at 15:00 IST (backtests showed targets and tighter stops lost more, because these stocks swing about 1-1.5% either way within the day). Optional settings add a profit target, breakeven and trailing stops, a VWAP exit and a time stop.',
   risk='One position of up to 98% of basket equity, so the flat ₹20 per-order brokerage stays near 0.08% of the trade. Round-trip Upstox intraday charges must be under 0.1% of the position, and new entries stop for the month once that month\'s charges reach ₹500 (about ₹6,000 a year). New entries also stop for the day after a 2% fall from the day\'s opening equity. Backtest 2022-2026: lost money every year after fees (-5% to -24%); paper experiment only.'),
 LIQUIDITY_SWEEP:dict(family='Intraday liquidity sweep and reclaim reversal (MIS-style)',entry='From 10:00–14:00 IST, a completed 5-minute candle sweeps below the 15-minute opening-range low or a rolling 30-candle low, then reclaims the level on that candle or the next. Candle RVOL must be at least 2.0x. One long per session, chased by at most 0.3%.',exit='Stop is sweep low minus 0.4 ATR, clamped to 0.8–1.8%; target 2.5R; breakeven at 1.2R, then trail by 1 ATR; square-off 15:00.',risk='Cash-only paper execution; position cap 95% and default risk budget 1% of equity per trade (both configurable), one entry/session, 2% daily-loss breaker, round-trip fee cap 0.1%, monthly fees capped at ₹500. Experimental; no profitability is implied.'),
 ORDER_FLOW_REVERSAL:dict(family='Intraday trend pullback with live order-book confirmation (MIS-style)',entry='From 10:00–14:00 IST, price must be above daily EMA50 and session VWAP; a completed 5-minute candle must close within 0.3% of VWAP without closing below it, while the live top-five bid/ask quantity ratio is at least 3:1. One long per session, chased by at most 0.25%.',exit='Stop below the pullback swing low or 1.2 ATR, clamped to 0.7–1.5%; target 2R; breakeven at 1R, then trail by 0.8 ATR; square-off 15:00.',risk='Order-book depth is sampled from the live Upstox full quote at the candle scan. Historical candles do not contain order-book depth, so backtests can only use a disclosed proxy. Cash-only; 95% position cap and default 1% equity risk budget (configurable), 2% daily breaker, fees ≤0.1% round trip and ₹500/month.'),
 VOLATILITY_COMPRESSION:dict(family='Intraday volatility squeeze and expansion breakout (MIS-style)',entry='From 10:00–14:00 IST, 20-period Bollinger width must be at its lowest 15th percentile over 30 candles and ATR14 flat/declining; a 5-minute close must break above the upper band and VWAP on at least 2.5x rolling volume. One long per session, chased by at most 0.35%.',exit='Stop at the lower band or 1.5 ATR, clamped to 0.8–2.0%; target 2R; breakeven at 1R; square-off 15:00.',risk='Cash-only paper execution; position cap 95% and default risk budget 1% of equity per trade (both configurable), one entry/session, 2% daily-loss breaker, round-trip fee cap 0.1%, monthly fees capped at ₹500. Experimental; no profitability is implied.'),
 ORB_VOLUME_BREAKOUT:dict(family='Opening range breakout with volume confirmation (MIS-style)',entry='The first 15 minutes (09:15–09:30 IST) define the range. From 09:30 until 10:00, buy a completed 5-minute close strictly above the opening-range high and session VWAP with at least 3x rolling 20-candle volume. One trade per day, chased by at most 0.3%.',exit='Stop at the higher of opening-range midpoint and close minus 1 ATR, clamped to 0.6–1.5%; target 1.5R; stop moves to entry at 1R; square-off 15:00.',risk='Cash-only paper execution; up to 95% position value, 1% default risk budget, 2% daily-loss breaker, round-trip fee cap 0.1%, and monthly fees capped at ₹500. Experimental; no profitability is implied.'),
 SECTOR_RELATIVE_STRENGTH:dict(family='Weekly sector relative-strength rotation (delivery)',entry='At the Monday review, rank broad sectors by equal-weight constituent 4-week return relative to the Nifty 500; select the top two outperformers and up to five positive-momentum stocks per sector with average daily turnover ≥₹2 crore. Buy at most 10 holdings, no more than 10% of starting capital each.',exit='Exit at the weekly review if a sector leaves the top two. Also trail at the higher of 8% below cost or two weekly ATR below the best completed weekly close.',risk='Cash-only delivery paper execution; maximum ten holdings across two sectors, estimated round-trip charges under 0.1%, monthly fees capped at ₹500, and a 2% daily loss breaker. Local sector returns use equal-weight constituent proxies because this archive has no sector-index histories.'),
}

def rules(name,cfg):
    p=params(name,cfg or {});t=RULE_TEXT[name]
    caps={NEWS_MOMENTUM:(p.get('max_positions'),p.get('max_position_pct'),p.get('max_entries_per_day')),
        NEWS_CATALYST:(p.get('max_positions'),p.get('position_pct'),p.get('max_entries_per_day')),
        EMA_PRICE_ACTION:(p.get('max_positions'),p.get('max_position_pct'),p.get('max_entries_per_day')),
        VINAY_INTRADAY:(p.get('max_positions'),p.get('max_position_pct'),p.get('max_entries_per_day')),
        **{n:(p.get('max_positions'),p.get('max_position_pct'),p.get('max_entries_per_day')) for n in NEW_INTRADAY_BASKETS},
        SECTOR_RELATIVE_STRENGTH:(p.get('max_positions'),p.get('max_position_pct'),p.get('max_positions'))}[name]
    return dict(name=TITLES[name]+' · paper basket',family=t['family'],entry=t['entry'],exit=t['exit'],risk=t['risk'],
        max_positions=caps[0],position_pct=caps[1],max_buys_per_day=caps[2],version=STRATEGY_VERSIONS[name],params=p,paper_only=True)

def transition(db,name,symbol,signal_id,old,new,reason,details=None,dedup=None,clock=None):
    db.execute('INSERT OR IGNORE INTO sb_transitions(portfolio,symbol,signal_id,from_stage,to_stage,reason,details,created_at,dedup) VALUES(?,?,?,?,?,?,?,?,?)',
        (name,symbol,signal_id,old,new,reason,json.dumps(details or {},default=str),(clock or datetime.now(timezone.utc)).isoformat(),dedup))

def set_status(db,signal_id,status,reason,clock,**fields):
    row=db.execute('SELECT portfolio,symbol,status FROM sb_signals WHERE id=?',(signal_id,)).fetchone()
    if not row or row['status']==status:return
    sets=', '.join(f'{k}=?' for k in fields)
    db.execute(f"UPDATE sb_signals SET status=?,updated_at=?{', '+sets if sets else ''} WHERE id=?",(status,clock.isoformat(),*fields.values(),signal_id))
    transition(db,row['portfolio'],row['symbol'],signal_id,row['status'],status,reason,clock=clock,dedup=f'sig:{signal_id}:{status}')

def valuation(db,name):
    """(cash, equity, invested) at last trade prices; equity is None if a holding has no price."""
    cash=db.execute('SELECT cash FROM portfolios WHERE name=?',(name,)).fetchone()[0];invested=0.
    for p in db.execute('SELECT p.quantity,q.price FROM positions p LEFT JOIN quotes q ON q.symbol=p.symbol WHERE p.portfolio=? AND p.quantity>0',(name,)):
        if p['price'] is None or not p['price']>0:return cash,None,None
        invested+=p['quantity']*p['price']
    return cash,cash+invested,invested

def daily_bars(db,symbol,before,limit=300):
    rows=db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? AND substr(ts,1,10)<? ORDER BY ts DESC LIMIT ?',(symbol,before,limit)).fetchall()
    return [dict(r) for r in reversed(rows)]

def benchmark(db,before):
    return [r[0] for r in db.execute('SELECT close FROM claude_benchmark WHERE day<? ORDER BY day',(before,))]

def intraday_bars(db,symbol,interval,clock,limit=400):
    """Completed intraday candles only: a candle starting at t is complete once t + interval <= now."""
    cutoff=(clock-timedelta(minutes=interval)).astimezone(IST).isoformat()
    rows=db.execute('SELECT ts,open,high,low,close,volume FROM sb_intraday WHERE symbol=? AND interval=? AND ts<=? ORDER BY ts DESC LIMIT ?',(symbol,interval,cutoff,limit)).fetchall()
    return [dict(r) for r in reversed(rows)]

def cost_per_share(entry,qty):
    if qty<=0:return 0.
    v=entry*qty;return (intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total'])/qty

class SignalEngine:
    def __init__(self,path):
        self.path=path;init_schema(path);self.contexts={}

    # ---- shared plumbing --------------------------------------------------------------------------
    def config(self,db,name):return get_state(db,'auto_config',{}).get(name,{})

    def execute(self,name,symbol,side,qty,guard,clock,realtime):
        from ..paper import trade
        from ..autotrading import event
        try:
            return trade(self.path,dict(portfolio=name,symbol=symbol,side=side,quantity=qty,request_id=f'sb-{name}-{uuid.uuid4().hex[:20]}'),
                None if realtime else clock,auto_guard=guard)
        except ValueError as error:
            with connect(self.path) as db:event(db,name,'blocked',str(error),symbol,clock=clock,details=getattr(error,'order_audit',None))
            return None

    def final_market_exit(self,name,symbol,st,p,clock,realtime):
        """Square-off still unfilled at 15:14: sell at the last traded price before the broker's 15:15 auto square-off."""
        from ..paper import final_market_exit
        from ..autotrading import event
        intent=dict(kind='CLOCK_EXIT',quantity=st['qty'],order='market',level=None,reason=f"Final market exit at {final_exit(p)} IST, before the broker's 15:15 auto square-off")
        try:return final_market_exit(self.path,name,symbol,st['qty'],datetime.now(timezone.utc) if realtime else clock,self.ema_exit_guard(name,intent,p))
        except ValueError as error:
            with connect(self.path) as db:event(db,name,'blocked',str(error),symbol,clock=clock)
            return None

    def guard(self,name,on_fill,product='delivery',side='BUY',signal=None):
        """Wrap `on_fill(db, order, quote, position, execution_clock)` with the shared pause/window/universe checks."""
        def check(db,order,quote,account,position):
            cfg=self.config(db,name)
            if not cfg.get('enabled'):raise ValueError('Basket automation was paused.')
            execution=datetime.fromisoformat(quote['execution_time']) if quote.get('execution_time') else datetime.now(timezone.utc)
            if order['side']=='BUY':require_buy_universe(db,name,order['symbol'],execution)
            return on_fill(db,order,quote,position,execution)
        check.product=product;check.audit_strategy=STRATEGY_VERSIONS[name];check.audit_signal=signal
        return check

    def slippage_ok(self,db,symbol,fill,side,limit_pct):
        ltp=db.execute('SELECT price FROM quotes WHERE symbol=?',(symbol,)).fetchone()
        if not ltp:raise ValueError('No last trade price to measure slippage.')
        slip=(fill/ltp[0]-1)*100*(1 if side=='BUY' else -1)
        if slip>limit_pct:raise ValueError(f'Estimated slippage {slip:.2f}% exceeds {limit_pct:g}%; no paper fill.')
        return slip

    def sync_positions(self,db,name,clock):
        """Reconcile state rows with the ledger (the source of truth) and adopt unmanaged holdings."""
        held={r['symbol']:dict(r) for r in db.execute('SELECT symbol,quantity,cost FROM positions WHERE portfolio=? AND quantity>0',(name,))}
        states={r['symbol']:dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))}
        for symbol,row in states.items():
            if symbol not in held:
                db.execute('DELETE FROM sb_positions WHERE portfolio=? AND symbol=?',(name,symbol))
                transition(db,name,symbol,row['signal_id'],row['stage'],'CLOSED','Position no longer held in the paper ledger',clock=clock)
            else:
                st=json.loads(row['state'])
                if st.get('qty')!=held[symbol]['quantity']:
                    st['qty']=held[symbol]['quantity'];db.execute('UPDATE sb_positions SET state=? WHERE portfolio=? AND symbol=?',(json.dumps(st),name,symbol))
        for symbol,row in held.items():
            if symbol in states:continue
            avg=row['cost']/row['quantity'];st=dict(entry=avg,qty=row['quantity'],original_qty=row['quantity'],adopted=True,average_cost=avg,best_close=avg)
            if name==NEWS_MOMENTUM:st.update(stop=round(avg*.92,2),initial_stop=round(avg*.92,2),target=round(avg*1.16,2),atr=avg*.04,entry_session=session(clock))
            if name==EMA_PRICE_ACTION:st.update(side='long',stop=0.,target=None,target_active=False,stage='ADOPTED',trail_active=False,partial_done=True,entry_atr=0,best_price=avg)
            if name==VINAY_INTRADAY:st=vinay_rules.adopt(avg,row['quantity'],params(name,self.config(db,name)))
            if name in NEW_INTRADAY_BASKETS:
                p=params(name,self.config(db,name));fake=dict(initial_stop=avg*(1-p['max_stop_pct']/100),initial_target=avg*(1-p['max_stop_pct']/100)+avg*p['target_r']*p['max_stop_pct']/100,trigger_ts=None)
                st=intraday_rules.arm(avg,row['quantity'],fake,p,entry_at=None);st.update(adopted=True,stage='ADOPTED',average_cost=avg)
            if name==SECTOR_RELATIVE_STRENGTH:
                raw=(db.execute('SELECT sector FROM instruments WHERE symbol=?',(symbol,)).fetchone() or [None])[0]
                st=dict(entry=avg,qty=row['quantity'],original_qty=row['quantity'],adopted=True,average_cost=avg,
                    sector=sector_group(raw,symbol=symbol),stop=avg*(1-p['stop_loss_pct']/100),
                    initial_stop=avg*(1-p['stop_loss_pct']/100),best_close=avg,weekly_atr=None,stage='ROTATION_HOLD')
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,symbol,None,STRATEGY_VERSIONS[name],session(clock),clock.isoformat(),'ADOPTED',json.dumps(st),clock.isoformat()))
            transition(db,name,symbol,None,None,'ADOPTED','Holding without basket state (e.g. a manual buy while paused) adopted with conservative exits',clock=clock)
        return held

    def snapshot(self,db,name,clock):
        cash,equity,invested=valuation(db,name)
        if equity is None:return None
        t=clock.astimezone(IST);bucket=t.replace(minute=t.minute-t.minute%5,second=0,microsecond=0).isoformat()
        db.execute('INSERT INTO sb_equity VALUES(?,?,?,?,?) ON CONFLICT(portfolio,ts) DO UPDATE SET equity=excluded.equity,cash=excluded.cash,invested=excluded.invested',(name,bucket,equity,cash,invested))
        return equity

    def scan(self,name,clock,realtime=False):
        if realtime:refresh_held_quotes(self.path,name)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if realtime:clock=datetime.now(timezone.utc)
            cfg=self.config(db,name);p=params(name,cfg)
            self.sync_positions(db,name,clock)
            equity=self.snapshot(db,name,clock)
            initial=db.execute('SELECT initial_cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
            halted,info=drawdown.gate(db,name,equity,initial,cfg)
        handler={EMA_PRICE_ACTION:self.scan_ema,VINAY_INTRADAY:self.scan_vinay,NEWS_MOMENTUM:self.scan_momentum,NEWS_CATALYST:self.scan_catalyst,
            **{n:self.scan_experimental for n in NEW_INTRADAY_BASKETS},SECTOR_RELATIVE_STRENGTH:self.scan_sector_rotation}[name]
        status=handler(name,clock,realtime,p,cfg,halted,equity)
        if halted:status.update(state='risk_wait',message=drawdown.message(info)+' '+status.get('message',''))
        return status

    def open_risk(self,db,name):
        total=0.
        for r in db.execute('SELECT state FROM sb_positions WHERE portfolio=?',(name,)):
            st=json.loads(r['state']);stop=st.get('stop')
            if stop and st.get('entry'):total+=max(0,(st['entry']-stop))*st.get('qty',0)
        return total

    def data_block(self,db,name,clock,p):
        """Message when the basket's signal data provider has failed recently (new entries wait; exits continue)."""
        if name in (EMA_PRICE_ACTION,VINAY_INTRADAY,*NEW_INTRADAY_BASKETS):
            key,what=('signal_ema_checkpoint','intraday candles') if name==EMA_PRICE_ACTION else ('signal_live_checkpoint','live-feed candles and volume')
            last=get_state(db,key,{}).get('last_success');limit=3*p['timeframe_minutes']*60
            if local_hm(clock)<'09:35':return None   # the first completed candles of the session are still arriving
        else:last=get_state(db,'signal_news_checkpoint',{}).get('last_success');limit=1800;what='NSE announcements'
        if not last or (clock-datetime.fromisoformat(last)).total_seconds()>limit:
            return f'New entries blocked: {what} have not been refreshed successfully in the last {limit//60} minutes. Exits stay active.'
        return None

    def counts(self,db,name,clock):
        start=clock.astimezone(IST).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        positions=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
        buys=db.execute("SELECT COUNT(*) FROM trades WHERE portfolio=? AND source='automatic' AND side='BUY' AND created_at>=?",(name,start)).fetchone()[0]
        return positions,buys,start

    # ---- EMA + price action ----------------------------------------------------------------------
    def ema_exit_guard(self,name,intent,p):
        def on_fill(db,order,quote,position,execution):
            row=db.execute('SELECT * FROM sb_positions WHERE portfolio=? AND symbol=?',(name,order['symbol'])).fetchone()
            held=position['quantity'] if position else 0
            if not row or order['quantity']>held:raise ValueError('Position state changed; exit rechecked next scan.')
            st=json.loads(row['state']);fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            if intent['order']=='limit' and fill<intent['level']-1e-9:raise ValueError(f"Limit exit at {intent['level']:.2f} not reachable: best bid fill {fill:.2f}.")
            if intent['kind']=='PARTIAL' and st.get('partial_done'):raise ValueError('Partial profit already booked.')
            before=st.get('stage');notes=(vinay_rules if name==VINAY_INTRADAY else ema_rules).apply_fill(st,intent,qty,fill,p) if intent['kind'] not in ('CLOCK_EXIT','CARRIED_OVER') else []
            if intent['kind'] in ('CLOCK_EXIT','CARRIED_OVER'):
                st['qty']=held-qty
                if st['qty']<=0:st['stage']='CLOSED';notes=[('CLOSED',intent['reason'])]
            for stage,why in notes:transition(db,name,order['symbol'],row['signal_id'],before,stage,why,dict(fill=fill,quantity=qty,kind=intent['kind']),clock=execution);before=stage
            if st['qty']<=0:db.execute('DELETE FROM sb_positions WHERE portfolio=? AND symbol=?',(name,order['symbol']))
            else:db.execute('UPDATE sb_positions SET state=?,stage=?,updated_at=? WHERE portfolio=? AND symbol=?',(json.dumps(st),st['stage'],execution.isoformat(),name,order['symbol']))
            return f"{TITLES[name]}: {intent['reason']}; PAPER"
        return on_fill

    def ema_entry_guard(self,name,signal,p):
        def on_fill(db,order,quote,position,execution):
            current=db.execute('SELECT status FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='ENTRY_PENDING':raise ValueError('Entry signal is no longer pending.')
            if not p['entry_start']<=local_hm(execution)<p['entry_cutoff']:raise ValueError(f"New EMA entries run {p['entry_start']}-{p['entry_cutoff']} IST.")
            if position and position['quantity']>0:raise ValueError('Already holding this stock; no averaging.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            if fill>signal['limit_price']+1e-9:raise ValueError(f"Fill {fill:.2f} above stop-limit protection {signal['limit_price']:.2f}.")
            slip=self.slippage_ok(db,order['symbol'],fill,'BUY',p['max_slippage_pct'])
            positions,buys,_=self.counts(db,name,execution)
            if positions>=p['max_positions']:raise ValueError('EMA basket position limit reached.')
            if buys>=p['max_entries_per_day']:raise ValueError('EMA basket daily entry limit reached.')
            cash,equity,_=valuation(db,name)
            if equity is None:raise ValueError('Basket equity cannot be marked; new entries wait.')
            sized=ema_rules.size(equity,cash+qty*fill,fill,signal['initial_stop'],p,self.open_risk(db,name),lambda v:intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total'])
            if qty>sized['quantity']:raise ValueError(f"Risk limits allow {sized['quantity']} shares at the actual fill {fill:.2f}; resized next scan.")
            details=json.loads(signal['details']);st=ema_rules.arm(details,fill,qty,p,cost_per_share(fill,qty))
            st['last_bar_ts']=details.get('trigger_ts')
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],session(execution),execution.isoformat(),st['stage'],json.dumps(st),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f'Filled {qty} at {fill:.2f} ({slip:+.2f}% vs last trade)',execution,quantity=qty)
            transition(db,name,order['symbol'],signal['id'],'ENTRY_FILLED','INITIAL_EXITS_ARMED',
                f"Stop {st['stop']:.2f} / target {st['target']:.2f} armed (OCO); breakeven at {st['be_trigger']:.2f}, partial at {st['partial_trigger']:.2f}",dict(state=st),clock=execution)
            return (f"{TITLES[name]}: EMA9/21 crossover {details.get('trigger_ts','')}, RSI {details['rsi14']:.1f}, ATR {details['atr14']:.2f}; "
                f"stop-limit trigger {signal['entry_trigger']:.2f}; SL {st['stop']:.2f}, TP {st['target']:.2f}, RR {signal['risk_reward']:.2f}; PAPER")
        return on_fill

    def scan_ema(self,name,clock,realtime,p,cfg,halted,equity):
        fills=0;notes=[];hm=local_hm(clock);today=session(clock);tf=p['timeframe_minutes']
        with connect(self.path) as db:
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            states=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
            pending=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='ENTRY_PENDING'",(name,))]
        squaring=hm>=p['square_off']
        if squaring:
            with connect(self.path) as db:
                for s in pending:set_status(db,s['id'],'CANCELLED',f"Session square-off at {p['square_off']} IST: pending entry cancelled",clock)
            pending=[]
        for row in states:
            st=json.loads(row['state']);symbol=row['symbol'];q=quotes.get(symbol)
            carried=row['entry_session']<today
            if squaring or carried:
                kind='CARRIED_OVER' if carried and not squaring else 'CLOCK_EXIT'
                intent=dict(kind=kind,quantity=st['qty'],order='market',level=None,
                    reason=('Intraday square-off at '+p['square_off']+' IST') if kind=='CLOCK_EXIT' else f"Recovery exit: intraday position from {row['entry_session']} was not squared off")
                if kind=='CLOCK_EXIT' and local_hm(clock)>=final_exit(p):
                    if self.final_market_exit(name,symbol,st,p,clock,realtime):fills+=1
                    continue
                if not fresh(q,clock):
                    with connect(self.path) as db:transition(db,name,symbol,row['signal_id'],row['stage'],'SQUARE_OFF_BLOCKED',
                        'No executable live price for the square-off; retrying every scan. The position is still open.',clock=clock,dedup=f'sqoff:{name}:{symbol}:{today}')
                    notes.append(f'{symbol}: square-off waiting for a live price');continue
                if self.execute(name,symbol,'SELL',st['qty'],self.guard(name,self.ema_exit_guard(name,intent,p),'intraday','SELL'),clock,realtime):fills+=1
                continue
            if not fresh(q,clock):notes.append(f'{symbol}: exits wait for a fresh price');continue
            # Ratchet the EMA20 trail on newly completed candles, then check the OCO levels against the live price.
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                bars=intraday_bars(db,symbol,tf,clock)
                if bars and bars[-1]['ts']!=st.get('last_bar_ts'):
                    for stage,why in ema_rules.on_bar(st,bars,p):transition(db,name,symbol,row['signal_id'],st['stage'],stage,why,clock=clock)
                    st['last_bar_ts']=bars[-1]['ts']
                before=st['stage'];intent,moved=ema_rules.on_price(st,q['price'],p)
                for stage,why in moved:transition(db,name,symbol,row['signal_id'],before,stage,why,clock=clock,dedup=f'{name}:{symbol}:{row["entry_at"]}:{stage}');before=stage
                db.execute('UPDATE sb_positions SET state=?,stage=?,updated_at=? WHERE portfolio=? AND symbol=?',(json.dumps(st),st['stage'],clock.isoformat(),name,symbol))
            if intent and self.execute(name,symbol,'SELL',intent['quantity'],self.guard(name,self.ema_exit_guard(name,intent,p),'intraday','SELL'),clock,realtime):fills+=1
        if squaring:return dict(state='monitoring',message=f"Square-off time ({p['square_off']} IST): no new entries this session. "+' '.join(notes),fills_this_scan=fills)
        # Pending stop-limit entries: expire, gap-cancel or trigger on the live price.
        with connect(self.path) as db:positions,buys,_=self.counts(db,name,clock);blocked=self.data_block(db,name,clock,p)
        window=p['entry_start']<=hm<p['entry_cutoff'] and not blocked
        for s in pending:
            q=quotes.get(s['symbol']);expires=s['expires_at']
            with connect(self.path) as db:
                if expires and clock.isoformat()>=expires:set_status(db,s['id'],'EXPIRED',f"Not triggered within {p['signal_expiry_candles']} candles",clock);continue
                bars=intraday_bars(db,s['symbol'],tf,clock)
                details=json.loads(s['details'])
                if bars and bars[-1]['ts']>details.get('trigger_ts',''):
                    why=ema_rules.still_valid(bars,p)
                    if why:set_status(db,s['id'],'CANCELLED',why,clock);continue
            if not window:
                if hm>=p['entry_cutoff'] and not blocked:
                    with connect(self.path) as db:set_status(db,s['id'],'CANCELLED',f"Entry cutoff {p['entry_cutoff']} IST reached",clock)
                continue
            if halted or not fresh(q,clock) or q['price']<s['entry_trigger']:continue
            if q['price']>s['entry_trigger']*(1+p['max_gap_pct']/100):
                with connect(self.path) as db:set_status(db,s['id'],'CANCELLED',f"Gap protection: {q['price']:.2f} is more than {p['max_gap_pct']:g}% above the trigger {s['entry_trigger']:.2f}",clock)
                continue
            if q['price']>s['limit_price'] or positions>=p['max_positions'] or buys>=p['max_entries_per_day']:continue
            with connect(self.path) as db:
                cash,equity_now,_=valuation(db,name)
                if equity_now is None:continue
                estimate=max(q['price'],s['entry_trigger'])*(1+p['max_slippage_pct']/100)
                sized=ema_rules.size(equity_now,cash,estimate,s['initial_stop'],p,self.open_risk(db,name),lambda v:intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total'])
            if sized['quantity']<1:
                with connect(self.path) as db:set_status(db,s['id'],'REJECTED',f"Quantity zero after risk/capital limits (binding: {sized['binding']})",clock,rejection_reason='quantity zero')
                continue
            if self.execute(name,s['symbol'],'BUY',sized['quantity'],self.guard(name,self.ema_entry_guard(name,s,p),'intraday','BUY',details),clock,realtime):
                fills+=1;positions+=1;buys+=1
        with connect(self.path) as db:
            open_=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='ENTRY_PENDING'",(name,)).fetchone()[0]
            positions,buys,_=self.counts(db,name,clock)
        note=f"{open_} pending stop-limit entr{'y' if open_==1 else 'ies'}; {positions}/{p['max_positions']} positions; {buys}/{p['max_entries_per_day']} entries today."
        if blocked:note+=' '+blocked
        elif not window:note+=f" New entries run {p['entry_start']}-{p['entry_cutoff']} IST; square-off {p['square_off']}."
        return dict(state='monitoring',message=note+(' '+' '.join(notes[:4]) if notes else ''),fills_this_scan=fills,position_count=positions,buy_count=buys)

    # ---- Vinay intraday ----------------------------------------------------------------------------
    def day_loss_block(self,db,name,clock,p):
        """Reason new entries stop for the session (daily loss or losing-trade limit), else None. Exits continue."""
        from .report import round_trips
        today=session(clock);_,_,start=self.counts(db,name,clock)
        _,equity,_=valuation(db,name)
        baselines=get_state(db,'signal_day_open_equity',{});baseline=baselines.get(name,{})
        if equity is not None and baseline.get('session')!=today:
            baseline=dict(session=today,equity=equity);baselines[name]=baseline;set_state(db,'signal_day_open_equity',baselines)
        opening=baseline.get('equity')
        if opening and equity is not None and equity<=opening*(1-p['max_daily_loss_pct']/100):
            return f"Day loss {(1-equity/opening)*100:.2f}% reached the {p['max_daily_loss_pct']:g}% limit: no new entries today."
        trades=[dict(r) for r in db.execute('SELECT symbol,side,quantity,price,fee,created_at FROM trades WHERE portfolio=? AND created_at>=? ORDER BY created_at',(name,start))]
        losers=sum(1 for t in round_trips(trades) if t['pnl']<0)
        losing_limit=p.get('max_losing_trades')
        if losing_limit is not None and losers>=losing_limit:return f"{losers} losing trades today (limit {losing_limit}): no new entries today."
        return None

    def month_fees(self,db,name,clock):
        """Charges paid this calendar month (IST), buys and sells, for the monthly fee budget."""
        start=clock.astimezone(IST).replace(day=1,hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        return db.execute('SELECT COALESCE(SUM(fee),0) FROM trades WHERE portfolio=? AND created_at>=?',(name,start)).fetchone()[0]

    def vinay_entry_guard(self,name,signal,p):
        details=json.loads(signal['details'])
        def on_fill(db,order,quote,position,execution):
            current=db.execute('SELECT status FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='BUY_CANDIDATE':raise ValueError('Candidate is no longer open.')
            if not p['entry_start']<=local_hm(execution)<p['entry_cutoff']:raise ValueError(f"New entries run {p['entry_start']}-{p['entry_cutoff']} IST.")
            if position and position['quantity']>0:raise ValueError('Already holding this stock; no averaging.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            if fill>details['close']*(1+p['max_chase_pct']/100)+1e-9:raise ValueError(f"Fill {fill:.2f} is more than {p['max_chase_pct']:g}% above the signal close {details['close']:.2f}; not chasing.")
            slip=self.slippage_ok(db,order['symbol'],fill,'BUY',p['max_slippage_pct'])
            positions,buys,_=self.counts(db,name,execution)
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:raise ValueError('Position or daily entry limit reached.')
            levels=vinay_rules.plan(fill,details['stop_pct'],p);cost=lambda v:intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total']
            why=vinay_rules.fee_check(fill,qty,cost,p,self.month_fees(db,name,execution))
            if why:raise ValueError(why+' at the actual fill; not bought.')
            cash,equity,_=valuation(db,name)
            if equity is None:raise ValueError('Basket equity cannot be marked; new entries wait.')
            sized=vinay_rules.size(equity,cash+qty*fill,fill,levels['stop'],p,self.open_risk(db,name),cost)
            if qty>sized['quantity']:raise ValueError(f"Risk limits allow {sized['quantity']} shares at the actual fill {fill:.2f}; resized next scan.")
            st=vinay_rules.arm(fill,qty,details['stop_pct'],p,cost_per_share(fill,qty),execution.isoformat())
            st['last_bar_ts']=details.get('trigger_ts')
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],session(execution),execution.isoformat(),st['stage'],json.dumps(st),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f'Bought {qty} at {fill:.2f} ({slip:+.2f}% vs last trade)',execution,quantity=qty)
            transition(db,name,order['symbol'],signal['id'],'ENTRY_FILLED','INITIAL_EXITS_ARMED',
                f"Stop {st['stop']:.2f}"+(f" / target {st['target']:.2f}" if st['target_active'] else '')+(f"; breakeven after fees at {st['be_trigger']:.2f}" if st['be_trigger'] else '')
                +(f"; trailing from {st['trail_trigger']:.2f}" if st['trail_trigger'] else '')+f"; square-off {p['square_off']} IST",dict(state=st),clock=execution)
            volume_note=f" on {details['rvol']:.1f}x volume" if details.get('rvol') is not None else ''
            return (f"{TITLES[name]}: up {details['day_change_pct']:+.2f}%{volume_note}, above opening range {details['or_high']:.2f} and VWAP {details['vwap']:.2f}; "
                f"SL {st['stop']:.2f}"+(f", TP {st['target']:.2f}" if st['target_active'] else f", sell by {p['square_off']}")+"; PAPER")
        return on_fill

    def scan_vinay(self,name,clock,realtime,p,cfg,halted,equity):
        fills=0;notes=[];hm=local_hm(clock);today=session(clock);tf=p['timeframe_minutes']
        cost=lambda v:intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total']
        with connect(self.path) as db:
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            states=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
            for s in db.execute("SELECT id FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND expires_at<=?",(name,clock.isoformat())).fetchall():
                set_status(db,s['id'],'EXPIRED','Not bought before the next candle closed',clock)
        squaring=hm>=p['square_off']
        if squaring:
            with connect(self.path) as db:
                for s in db.execute("SELECT id FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE'",(name,)).fetchall():
                    set_status(db,s['id'],'CANCELLED',f"Session square-off at {p['square_off']} IST",clock)
        for row in states:
            st=json.loads(row['state']);symbol=row['symbol'];q=quotes.get(symbol)
            carried=row['entry_session']<today
            if squaring or carried:
                kind='CARRIED_OVER' if carried and not squaring else 'CLOCK_EXIT'
                intent=dict(kind=kind,quantity=st['qty'],order='market',level=None,
                    reason=('Intraday square-off at '+p['square_off']+' IST') if kind=='CLOCK_EXIT' else f"Recovery exit: intraday position from {row['entry_session']} was not squared off")
                if kind=='CLOCK_EXIT' and local_hm(clock)>=final_exit(p):
                    if self.final_market_exit(name,symbol,st,p,clock,realtime):fills+=1
                    continue
                if not fresh(q,clock):
                    with connect(self.path) as db:transition(db,name,symbol,row['signal_id'],row['stage'],'SQUARE_OFF_BLOCKED',
                        'No executable live price for the square-off; retrying every scan. The position is still open.',clock=clock,dedup=f'sqoff:{name}:{symbol}:{today}')
                    notes.append(f'{symbol}: square-off waiting for a live price');continue
                if self.execute(name,symbol,'SELL',st['qty'],self.guard(name,self.ema_exit_guard(name,intent,p),'intraday','SELL'),clock,realtime):fills+=1
                continue
            if not fresh(q,clock):notes.append(f'{symbol}: exits wait for a fresh price');continue
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                bars=live_bars.bars(db,symbol,clock,tf)
                if bars and bars[-1]['ts']!=st.get('last_bar_ts'):
                    for stage,why in vinay_rules.on_bar(st,bars,p):transition(db,name,symbol,row['signal_id'],st['stage'],stage,why,clock=clock)
                    st['last_bar_ts']=bars[-1]['ts']
                before=st['stage'];intent,moved=vinay_rules.on_price(st,q['price'],p)
                for stage,why in moved:
                    transition(db,name,symbol,row['signal_id'],before,stage,why,clock=clock,dedup=None if stage=='TRAIL_UPDATE' else f'{name}:{symbol}:{row["entry_at"]}:{stage}');before=stage
                db.execute('UPDATE sb_positions SET state=?,stage=?,updated_at=? WHERE portfolio=? AND symbol=?',(json.dumps(st),st['stage'],clock.isoformat(),name,symbol))
            if intent and self.execute(name,symbol,'SELL',intent['quantity'],self.guard(name,self.ema_exit_guard(name,intent,p),'intraday','SELL'),clock,realtime):fills+=1
        if squaring:return dict(state='monitoring',message=f"Square-off time ({p['square_off']} IST): no new entries this session. "+' '.join(notes),fills_this_scan=fills)
        with connect(self.path) as db:
            blocked=self.data_block(db,name,clock,p) or self.day_loss_block(db,name,clock,p)
            positions,buys,start=self.counts(db,name,clock)
            traded={r[0] for r in db.execute("SELECT DISTINCT symbol FROM trades WHERE portfolio=? AND created_at>=?",(name,start))}
            candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND session=? ORDER BY score DESC",(name,today))]
        window=p['entry_start']<=hm<p['entry_cutoff']
        for s in candidates:
            if blocked or halted or not window or positions>=p['max_positions'] or buys>=p['max_entries_per_day']:break
            q=quotes.get(s['symbol']);d=json.loads(s['details'])
            if s['symbol'] in traded or not fresh(q,clock):continue
            if q['price']>d['close']*(1+p['max_chase_pct']/100) or q['price']<=d['vwap']:continue    # ran away, or lost VWAP: wait for the next candle
            with connect(self.path) as db:
                cash,equity_now,_=valuation(db,name)
                if equity_now is None:continue
                estimate=q['price']*(1+p['max_slippage_pct']/100);levels=vinay_rules.plan(estimate,d['stop_pct'],p)
                sized=vinay_rules.size(equity_now,cash,estimate,levels['stop'],p,self.open_risk(db,name),cost)
                why='Quantity zero after risk/capital limits (binding: '+sized['binding']+')' if sized['quantity']<1 else vinay_rules.fee_check(estimate,sized['quantity'],cost,p,self.month_fees(db,name,clock))
                if why:set_status(db,s['id'],'REJECTED',why,clock,rejection_reason=why);continue
            if self.execute(name,s['symbol'],'BUY',sized['quantity'],self.guard(name,self.vinay_entry_guard(name,s,p),'intraday','BUY',d),clock,realtime):
                fills+=1;positions+=1;buys+=1;traded.add(s['symbol'])
        with connect(self.path) as db:
            open_=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE'",(name,)).fetchone()[0]
            positions,buys,_=self.counts(db,name,clock)
        note=f"{open_} candidate(s) waiting; {positions}/{p['max_positions']} positions; {buys}/{p['max_entries_per_day']} entries today."
        if blocked:note+=' '+blocked
        elif not window:note+=f" New entries run {p['entry_start']}-{p['entry_cutoff']} IST; square-off {p['square_off']}."
        return dict(state='monitoring',message=note+(' '+' '.join(notes[:4]) if notes else ''),fills_this_scan=fills,position_count=positions,buy_count=buys)

    # ---- Experimental live 5-minute baskets -------------------------------------------------------
    def experimental_entry_guard(self,name,signal,p):
        from . import intraday_rules
        details=json.loads(signal['details'])
        def on_fill(db,order,quote,position,execution):
            current=db.execute('SELECT status FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='BUY_CANDIDATE':raise ValueError('Candidate is no longer open.')
            if not p['entry_start']<=local_hm(execution)<p['entry_cutoff']:raise ValueError(f"New entries run {p['entry_start']}-{p['entry_cutoff']} IST.")
            if position and position['quantity']>0:raise ValueError('Already holding this stock; no averaging.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            if fill>details['close']*(1+p['max_chase_pct']/100)+1e-9:raise ValueError(f"Fill exceeds the {p['max_chase_pct']:g}% chase limit.")
            if fill<=details['initial_stop']:raise ValueError('Live fill is at or below the planned stop; no entry.')
            if name in (ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION) and fill<=details.get('vwap',0):raise ValueError('Live price lost VWAP after the signal; no entry.')
            self.slippage_ok(db,order['symbol'],fill,'BUY',p['max_slippage_pct'])
            positions,buys,_=self.counts(db,name,execution)
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:raise ValueError('Position or daily entry limit reached.')
            cost=lambda value:intraday_charges(value,'BUY')['total']+intraday_charges(value,'SELL')['total']
            why=vinay_rules.fee_check(fill,qty,cost,p,self.month_fees(db,name,execution))
            if why:raise ValueError(why)
            cash,equity,_=valuation(db,name)
            if equity is None:raise ValueError('Basket equity cannot be marked; new entries wait.')
            sized=ema_rules.size(equity,cash+qty*fill,fill,details['initial_stop'],p,self.open_risk(db,name),cost)
            if qty>sized['quantity']:raise ValueError(f"Risk limits allow {sized['quantity']} shares at the actual fill; resized next scan.")
            st=intraday_rules.arm(fill,qty,details,p,cost_per_share(fill,qty),execution.isoformat())
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],session(execution),execution.isoformat(),st['stage'],json.dumps(st),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f'Bought {qty} at {fill:.2f}; stop {st["stop"]:.2f}, target {st["target"]:.2f}',execution,quantity=qty)
            transition(db,name,order['symbol'],signal['id'],'ENTRY_FILLED','INITIAL_EXITS_ARMED',
                f"Initial stop {st['stop']:.2f}; target {st['target']:.2f} ({p['target_r']:g}R); breakeven trigger {st['be_trigger']:.2f}; square-off {p['square_off']} IST",dict(state=st),clock=execution)
            return f"{TITLES[name]}: {details['trigger_ts']} entry at {fill:.2f}; stop {st['stop']:.2f}, target {st['target']:.2f}; PAPER"
        return on_fill

    def scan_experimental(self,name,clock,realtime,p,cfg,halted,equity):
        fills=0;hm=local_hm(clock);today=session(clock);tf=p['timeframe_minutes']
        cost=lambda value:intraday_charges(value,'BUY')['total']+intraday_charges(value,'SELL')['total']
        with connect(self.path) as db:
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            states=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
            candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND session=? ORDER BY score DESC",(name,today))]
        for row in states:
            st=json.loads(row['state']);symbol=row['symbol'];q=quotes.get(symbol)
            if hm>=p['square_off'] or row['entry_session']<today:
                kind='CLOCK_EXIT' if row['entry_session']==today else 'CARRIED_OVER'
                intent=dict(kind=kind,quantity=st['qty'],order='market',level=None,reason=f"Intraday square-off at {p['square_off']} IST" if kind=='CLOCK_EXIT' else 'Recovery exit: carried intraday paper position')
                if kind=='CLOCK_EXIT' and hm>=final_exit(p):
                    if self.final_market_exit(name,symbol,st,p,clock,realtime):fills+=1
                    continue
                if fresh(q,clock) and self.execute(name,symbol,'SELL',st['qty'],self.guard(name,self.ema_exit_guard(name,intent,p),'intraday','SELL'),clock,realtime):fills+=1
                continue
            if not fresh(q,clock):continue
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE');bars=live_bars.bars(db,symbol,clock,tf)
                atr=intraday_rules.atr_series(bars,p['atr_period'])[-1] if len(bars)>=p['atr_period']+2 else None
                intent,moved=intraday_rules.on_price(st,q['price'],p,atr)
                for stage,why in moved:transition(db,name,symbol,row['signal_id'],st['stage'],stage,why,clock=clock,dedup=f'{name}:{symbol}:{row["entry_at"]}:{stage}');
                db.execute('UPDATE sb_positions SET state=?,stage=?,updated_at=? WHERE portfolio=? AND symbol=?',(json.dumps(st),st['stage'],clock.isoformat(),name,symbol))
            target_level=intent.get('level') if intent and intent['kind']=='TARGET' else None
            if intent and self.execute(name,symbol,'SELL',intent['quantity'],self.guard(name,self.exit_guard(name,symbol,intent['kind'],intent['reason'],target_level),'intraday','SELL'),clock,realtime):fills+=1
        with connect(self.path) as db:
            blocked=self.data_block(db,name,clock,p) or self.day_loss_block(db,name,clock,p)
            positions,buys,start=self.counts(db,name,clock)
            traded={r[0] for r in db.execute("SELECT DISTINCT symbol FROM trades WHERE portfolio=? AND created_at>=?",(name,start))}
            for s in db.execute("SELECT id,expires_at FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE'",(name,)).fetchall():
                if s['expires_at'] and clock.isoformat()>=s['expires_at']:set_status(db,s['id'],'EXPIRED','Not bought within the two-candle signal window',clock)
            candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND session=? ORDER BY score DESC",(name,today))]
        window=p['entry_start']<=hm<p['entry_cutoff'] and hm<p['square_off']
        for signal in candidates:
            if blocked or halted or not window or positions>=p['max_positions'] or buys>=p['max_entries_per_day']:break
            symbol=signal['symbol'];q=quotes.get(symbol);d=json.loads(signal['details'])
            if symbol in traded or not fresh(q,clock) or q['price']>d['close']*(1+p['max_chase_pct']/100):continue
            if name==ORDER_FLOW_REVERSAL and q['price']<d.get('vwap',0):continue
            with connect(self.path) as db:
                cash,equity_now,_=valuation(db,name)
                if equity_now is None:continue
                estimate=q['price']*(1+p['max_slippage_pct']/100)
                sized=ema_rules.size(equity_now,cash,estimate,d['initial_stop'],p,self.open_risk(db,name),cost)
                why='Quantity zero after risk/capital limits' if sized['quantity']<1 else vinay_rules.fee_check(estimate,sized['quantity'],cost,p,self.month_fees(db,name,clock))
                if why:set_status(db,signal['id'],'REJECTED',why,clock,rejection_reason=why);continue
            if self.execute(name,symbol,'BUY',sized['quantity'],self.guard(name,self.experimental_entry_guard(name,signal,p),'intraday','BUY',d),clock,realtime):
                fills+=1;positions+=1;buys+=1;traded.add(symbol)
        with connect(self.path) as db:positions,buys,_=self.counts(db,name,clock)
        note=f"{positions}/{p['max_positions']} positions; {buys}/{p['max_entries_per_day']} entries today."
        if blocked:note+=' '+blocked
        elif not window:note+=f" New entries run {p['entry_start']}-{p['entry_cutoff']} IST; square-off {p['square_off']}."
        return dict(state='monitoring',message=note,fills_this_scan=fills,position_count=positions,buy_count=buys)

    def sector_entry_guard(self,name,signal,p,review_week):
        details=json.loads(signal['details'])
        def on_fill(db,order,quote,position,execution):
            cfg=self.config(db,name)
            if not cfg.get('enabled'):raise ValueError('Basket automation was paused.')
            current=db.execute('SELECT status FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='BUY_CANDIDATE':raise ValueError('Sector candidate is no longer pending.')
            local=execution.astimezone(IST)
            if local.weekday()!=0 or not ('09:15'<=local.strftime('%H:%M')<'10:00') or review_week!=sector_rules.weekly_key(local.date().isoformat()):
                raise ValueError('Sector rotation buys run at the Monday open after the weekly review.')
            require_buy_universe(db,name,order['symbol'],execution)
            if position and position['quantity']>0:raise ValueError('Already holding; sector basket does not average down.')
            held=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
            if held>=p['max_positions']:raise ValueError('Sector basket has reached its ten-holding limit.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity']);value=fill*qty
            initial=db.execute('SELECT initial_cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
            if value+quote.get('fill_fee',0)>initial*p['max_position_pct']/100+1:raise ValueError('Position exceeds the 10% per-stock allocation limit.')
            roundtrip=charges(value,'BUY')['total']+charges(value,'SELL',True)['total']
            if roundtrip>value*p['max_round_trip_fee_pct']/100:raise ValueError(f'Estimated round-trip delivery charges {roundtrip/value*100:.2f}% exceed the {p["max_round_trip_fee_pct"]:g}% fee cap.')
            if self.month_fees(db,name,execution)+roundtrip>p['monthly_fee_budget']:raise ValueError('Monthly ₹500 fee budget would be exceeded.')
            blocked=self.day_loss_block(db,name,execution,p)
            if blocked:raise ValueError(blocked)
            halted,_=drawdown.gate(db,name,valuation(db,name)[1],initial,cfg)
            if halted:raise ValueError('Portfolio drawdown breaker is active; no new sector buys.')
            stop=fill*(1-p['stop_loss_pct']/100);weekly_atr=details.get('weekly_atr')
            state=dict(entry=fill,qty=qty,original_qty=qty,sector=details['sector'],stop=stop,initial_stop=stop,
                best_close=details.get('close',fill),weekly_atr=weekly_atr,stage='ROTATION_HOLD',last_review_week=review_week,
                momentum=details.get('momentum'),sector_excess_return=details.get('sector_excess_return'))
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],
                session(execution),execution.isoformat(),'ROTATION_HOLD',json.dumps(state),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f"Bought {qty} at {fill:.2f}; {details['sector']} 4-week relative strength; stop {stop:.2f}",execution,quantity=qty)
            transition(db,name,order['symbol'],signal['id'],'ENTRY_FILLED','ROTATION_HOLD',
                f"10% allocation; initial 8% stop {stop:.2f}; weekly review {review_week}",dict(state=state),clock=execution)
            return f"Sector Relative Strength: {details['sector']} leader, {details['momentum']*100:+.1f}% 4-week momentum; paper only"
        return on_fill

    def scan_sector_rotation(self,name,clock,realtime,p,cfg,halted,equity):
        """Weekly portfolio review, live depth fills and daily stop monitoring; delivery paper only."""
        from datetime import date
        p=sector_rules.DEFAULTS|p;today=session(clock);local=clock.astimezone(IST);week=sector_rules.weekly_key(today)
        with connect(self.path) as db:
            allstate=get_state(db,'sector_rotation_state',{});state=allstate.get(name)
            positions=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
        due=not state or state.get('review_week')!=week
        if due:
            if local.weekday()!=0 or local.strftime('%H:%M')<'09:20':
                return dict(state='monitoring',message='Waiting for the Monday 09:20 IST sector review; no new allocation until then.',fills_this_scan=0)
            try:
                with connect(self.path) as db:
                    allowed=allowed_symbols(db,name,clock)
                    if allowed is None:raise ValueError('Sector rotation requires the verified Nifty 500 buy universe.')
                    ranked=sector_rules.rank(db,clock,p,allowed)
                    state={**ranked,'review_week':week,'reviewed_at':clock.isoformat()}
                    allstate=get_state(db,'sector_rotation_state',{});allstate[name]=state;set_state(db,'sector_rotation_state',allstate)
                    expiry=(local.date()+timedelta(days=7)).isoformat()
                    for item in ranked['picks']:
                        key=f"{week}:{item['symbol']}"
                        upsert_signal(db,name,key,item['symbol'],'BUY_CANDIDATE',clock,valid_from=today,expires_at=expiry,
                            score=item['momentum']*100+item['sector_excess_return']*100,
                            explanation=f"{item['sector']} sector +{item['sector_excess_return']*100:.2f}% vs Nifty 500; stock momentum {item['momentum']*100:.2f}%; average turnover ₹{item['avg_turnover']:,.0f}/day",
                            details=item)
            except ValueError as error:
                return dict(state='data_wait',message=str(error),fills_this_scan=0)
            with connect(self.path) as db:positions=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
        if not state:return dict(state='data_wait',message='No completed sector ranking is available yet.',fills_this_scan=0)
        sectors={x['sector'] for x in state.get('sectors',[])};stock_stats={x['symbol']:x for x in state.get('stocks',[])}
        with connect(self.path) as db:quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
        fills=0
        for row in positions:
            st=json.loads(row['state']);symbol=row['symbol'];q=quotes.get(symbol)
            if due and st.get('sector') in sectors and st.get('last_review_week')!=week:
                metric=stock_stats.get(symbol,{})
                peak=max(st.get('best_close',st['entry']),metric.get('close') or st.get('best_close',st['entry']))
                st['best_close']=peak;st['weekly_atr']=metric.get('weekly_atr') or st.get('weekly_atr')
                st['stop']=sector_rules.stop(st['entry'],peak,st.get('weekly_atr'),p);st['last_review_week']=week
                with connect(self.path) as db:db.execute('UPDATE sb_positions SET state=?,stage=?,updated_at=? WHERE portfolio=? AND symbol=?',
                    (json.dumps(st),'ROTATION_HOLD',clock.isoformat(),name,symbol))
            rotation_exit=st.get('sector') not in sectors and st.get('last_review_week')!=week
            stop_exit=q is not None and q.get('price',0)>0 and q['price']<=st.get('stop',st['entry']*(1-p['stop_loss_pct']/100))
            if rotation_exit or stop_exit:
                if not fresh(q,clock):continue
                reason=f"Sector {st.get('sector')} left the leading two" if rotation_exit else f"Weekly trailing stop {st['stop']:.2f} reached"
                intent=dict(kind='SECTOR_ROTATION' if rotation_exit else 'WEEKLY_ATR_STOP',quantity=st['qty'],order='market',level=None,reason=reason)
                guard=self.guard(name,self.exit_guard(name,symbol,intent['kind'],reason),'delivery','SELL')
                if self.execute(name,symbol,'SELL',st['qty'],guard,clock,realtime):fills+=1
        buys=0
        monday_open=local.weekday()==0 and '09:15'<=local.strftime('%H:%M')<'10:00'
        if monday_open and not halted:
            with connect(self.path) as db:
                blocked=self.day_loss_block(db,name,clock,p)
                candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND session=? ORDER BY score DESC",(name,today))]
            if not blocked:
                for sig in candidates:
                    with connect(self.path) as db:
                        held=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
                        if held>=p['max_positions'] or db.execute('SELECT 1 FROM positions WHERE portfolio=? AND symbol=? AND quantity>0',(name,sig['symbol'])).fetchone():continue
                        account=db.execute('SELECT initial_cash,cash FROM portfolios WHERE name=?',(name,)).fetchone()
                    q=quotes.get(sig['symbol'])
                    if not fresh(q,clock):continue
                    value=min(account['cash'],account['initial_cash']*p['max_position_pct']/100)
                    qty=math.floor(value/(q['price']*1.003))
                    if qty<1:continue
                    d=json.loads(sig['details']);guard=self.guard(name,self.sector_entry_guard(name,sig,p,week),'delivery','BUY',d)
                    if self.execute(name,sig['symbol'],'BUY',qty,guard,clock,realtime):fills+=1;buys+=1
        with connect(self.path) as db:
            held=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
            bought=db.execute("SELECT COUNT(*) FROM trades WHERE portfolio=? AND side='BUY' AND source='automatic' AND created_at>=?",(name,local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat())).fetchone()[0]
        msg=f"Sectors: {', '.join(sorted(sectors)) or 'none ranked'}; {held}/{p['max_positions']} holdings; {bought} buys today."
        if local.weekday()!=0:msg+=' Weekly review runs Monday at the open.'
        elif not monday_open:msg+=' New sector buys run 09:15–10:00 IST.'
        return dict(state='monitoring',message=msg,fills_this_scan=fills,position_count=held,buy_count=bought,
            sectors=state.get('sectors',[]),as_of=state.get('as_of'),review_week=week)

    # ---- News momentum ---------------------------------------------------------------------------
    def context(self,db,symbol,day):
        key=(symbol,day)
        if key not in self.contexts:
            if len(self.contexts)>2000:self.contexts.clear()
            self.contexts[key]=news_rules.daily_context(daily_bars(db,symbol,day),benchmark(db,day))
        return self.contexts[key]

    def sector_counts(self,db,name):
        out={}
        for r in db.execute("SELECT p.symbol,i.sector FROM positions p JOIN instruments i USING(symbol) WHERE p.portfolio=? AND p.quantity>0",(name,)):
            g=sector_group(r['sector'],symbol=r['symbol']);out[g]=out.get(g,0)+1
        return out

    def negative_news(self,db,symbol,since,min_impact,governance_only=False):
        row=db.execute('''SELECT n.headline,n.published_at,a.label,a.category,a.impact_score,a.details FROM sb_news n JOIN sb_news_analysis a ON a.news_id=n.id
            WHERE n.symbol=? AND n.published_at>? AND n.verification='verified' AND n.duplicate_of IS NULL AND a.label IN ('Negative','Strongly Negative') AND a.impact_score>=?
            ORDER BY n.published_at DESC LIMIT 1''',(symbol,since,min_impact)).fetchone()
        if not row:return None
        if governance_only and not json.loads(row['details']).get('governance'):return None
        return f"{row['label']} {row['category'].replace('_',' ')} filing at {row['published_at'][:16]}: {row['headline'][:140]}"

    def exit_guard(self,name,symbol,kind,reason,level=None):
        def on_fill(db,order,quote,position,execution):
            held=position['quantity'] if position else 0
            if order['quantity']>held:raise ValueError('Held quantity changed; exit rechecked next scan.')
            if level is not None and quote['price']<level-1e-9:raise ValueError(f'Target exit at {level:.2f} not reachable at the bid.')
            row=db.execute('SELECT * FROM sb_positions WHERE portfolio=? AND symbol=?',(name,symbol)).fetchone()
            left=held-quote.get('fill_quantity',order['quantity'])
            if row:
                if left<=0:db.execute('DELETE FROM sb_positions WHERE portfolio=? AND symbol=?',(name,symbol))
                else:
                    st=json.loads(row['state']);st['qty']=left;db.execute('UPDATE sb_positions SET state=? WHERE portfolio=? AND symbol=?',(json.dumps(st),name,symbol))
                transition(db,name,symbol,row['signal_id'],row['stage'],'CLOSED' if left<=0 else row['stage'],reason,dict(kind=kind,fill=quote['price'],quantity=quote.get('fill_quantity')),clock=execution)
            return f'{TITLES[name]}: {kind.replace("_"," ").lower()}: {reason}; PAPER'
        return on_fill

    def scan_momentum(self,name,clock,realtime,p,cfg,halted,equity):
        fills=0;today=session(clock);hm=local_hm(clock)
        with connect(self.path) as db:
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            states=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
            sessions=get_state(db,'quality_sessions',[])
        for row in states:
            symbol=row['symbol'];st=json.loads(row['state']);q=quotes.get(symbol)
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                ctx=self.context(db,symbol,today)
                trail=news_rules.momentum_trail(st,ctx,p)
                if trail:transition(db,name,symbol,row['signal_id'],row['stage'],'TRAIL_UPDATE',trail,clock=clock,dedup=f'{name}:{symbol}:trail:{today}')
                db.execute('UPDATE sb_positions SET state=? WHERE portfolio=? AND symbol=?',(json.dumps(st),name,symbol))
                neg=self.negative_news(db,symbol,row['entry_at'],p['min_exit_impact'])
            if not fresh(q,clock):continue
            held=sum(1 for d in sessions if row['entry_session']<=d<today)
            exit_=news_rules.momentum_exit(st,q['price'],ctx,held,neg,p)
            if exit_:
                kind,why=exit_
                if self.execute(name,symbol,'SELL',st['qty'],self.guard(name,self.exit_guard(name,symbol,kind,why,st['target'] if kind=='TARGET' else None),'delivery','SELL'),clock,realtime):fills+=1
        start,end=WINDOWS[name]
        with connect(self.path) as db:blocked=self.data_block(db,name,clock,p)
        if blocked:return dict(state='data_wait',message=blocked,fills_this_scan=fills)
        if halted or not start<=hm<end:
            return dict(state='monitoring',message=f'Monitoring exits. New entries run {start}-{end} IST.' if not halted else '',fills_this_scan=fills)
        with connect(self.path) as db:
            for s in db.execute("SELECT id FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND expires_at<?",(name,today)).fetchall():
                set_status(db,s['id'],'EXPIRED','Confirmed candidate not bought before its expiry session',clock)
            candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND valid_from<=? ORDER BY score DESC",(name,today))]
            positions,buys,_=self.counts(db,name,clock);sectors=self.sector_counts(db,name)
        for s in candidates:
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:break
            q=quotes.get(s['symbol']);details=json.loads(s['details']);plan=details.get('plan') or {}
            if not fresh(q,clock):continue
            with connect(self.path) as db:
                if db.execute('SELECT 1 FROM positions WHERE portfolio=? AND symbol=? AND quantity>0',(name,s['symbol'])).fetchone():continue
                sector=sector_group((db.execute('SELECT sector FROM instruments WHERE symbol=?',(s['symbol'],)).fetchone() or [None])[0],symbol=s['symbol'])
                if sectors.get(sector,0)>=p['max_per_sector']:continue
                gap=(q['price']/plan['reference']-1)*100
                if abs(gap)>p['max_gap_pct']:
                    set_status(db,s['id'],'CANCELLED',f"Gap protection: live {q['price']:.2f} is {gap:+.1f}% from the confirmation close {plan['reference']:.2f}",clock);continue
                cash,eq,_=valuation(db,name)
                if eq is None:continue
                entry=q['price']*(1+p['slippage_bps']/1e4);stop=entry-p['stop_atr_mult']*plan['atr']
                risk=entry-stop;qty=min(math.floor(eq*p['risk_pct']/100/risk),math.floor(min(cash,eq*p['max_position_pct']/100)/(entry*1.003)))
            if qty<1:continue
            if self.execute(name,s['symbol'],'BUY',qty,self.guard(name,self.momentum_entry_guard(name,s,p,sector),'delivery','BUY',details),clock,realtime):
                fills+=1;positions+=1;buys+=1;sectors[sector]=sectors.get(sector,0)+1
        with connect(self.path) as db:
            counts={r['status']:r['n'] for r in db.execute("SELECT status,COUNT(*) n FROM sb_signals WHERE portfolio=? AND session=? GROUP BY status",(name,today))}
            watch=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='WATCHLIST'",(name,)).fetchone()[0]
            ready=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE'",(name,)).fetchone()[0]
            positions,buys,_=self.counts(db,name,clock)
        return dict(state='monitoring',message=f"{ready} confirmed candidate(s), {watch} on the watchlist awaiting a completed session; {positions}/{p['max_positions']} positions; {buys}/{p['max_entries_per_day']} buys today. Today's decisions: "+(', '.join(f'{v} {k.lower()}' for k,v in counts.items()) or 'none')+'.',
            fills_this_scan=fills,position_count=positions,buy_count=buys)

    def momentum_entry_guard(self,name,signal,p,sector):
        details=json.loads(signal['details']);plan=details['plan']
        def on_fill(db,order,quote,position,execution):
            current=db.execute('SELECT status FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='BUY_CANDIDATE':raise ValueError('Candidate no longer valid.')
            start,end=WINDOWS[name]
            if not start<=local_hm(execution)<end:raise ValueError(f'News momentum entries run {start}-{end} IST.')
            if position and position['quantity']>0:raise ValueError('Already holding; no averaging.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            self.slippage_ok(db,order['symbol'],fill,'BUY',p['max_slippage_pct'])
            positions,buys,_=self.counts(db,name,execution)
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:raise ValueError('Position or daily entry limit reached.')
            stop=fill-p['stop_atr_mult']*plan['atr'];target=fill+p['target_atr_mult']*plan['atr']
            if plan.get('resistance') and plan['resistance']<target:target=plan['resistance']
            rr=(target-fill)/(fill-stop)
            if rr<p['min_rr']-1e-9:raise ValueError(f"Risk-reward {rr:.2f} below {p['min_rr']:g} at the actual fill; not bought.")
            cash,eq,_=valuation(db,name)
            if eq is None or qty*(fill-stop)>eq*p['risk_pct']/100*1.02:raise ValueError('Per-trade risk above the limit at the actual fill.')
            st=dict(entry=fill,qty=qty,original_qty=qty,stop=round(stop,2),initial_stop=round(stop,2),target=round(target,2),atr=plan['atr'],
                entry_session=session(execution),best_close=fill,news_id=signal['news_id'],sector=sector)
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],session(execution),execution.isoformat(),'OPEN',json.dumps(st),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f'Bought {qty} at {fill:.2f}; stop {stop:.2f}, target {target:.2f}, RR {rr:.2f}',execution,quantity=qty)
            return f"{TITLES[name]}: {signal['explanation'][:160]}; stop {stop:.2f}, target {target:.2f}, RR {rr:.2f}; PAPER"
        return on_fill

    # ---- News catalyst investor ------------------------------------------------------------------
    def scan_catalyst(self,name,clock,realtime,p,cfg,halted,equity):
        fills=0;today=session(clock);hm=local_hm(clock)
        with connect(self.path) as db:
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            states=[dict(r) for r in db.execute('SELECT * FROM sb_positions WHERE portfolio=?',(name,))]
        from .runner import catalyst_inputs
        for row in states:
            symbol=row['symbol'];st=json.loads(row['state']);q=quotes.get(symbol)
            if not fresh(q,clock):continue
            due=st.get('pending_exit') or (st.get('last_review') or '')<=(datetime.fromisoformat(today)-timedelta(days=p['review_days'])).date().isoformat()
            if not due:
                if q['price']<=st['average_cost']*(1-p['hard_stop_pct']/100):st['pending_exit']=['RISK_STOP',f"Down {p['hard_stop_pct']:g}% from average cost"]
                else:continue
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                if not st.get('pending_exit'):
                    profile,result,ctx,_=catalyst_inputs(db,symbol,today,q['price'],None,p)
                    gov=self.negative_news(db,symbol,row['entry_at'],0,governance_only=True)
                    sector_pct=self.sector_pct(db,name,symbol,equity)
                    held_days=(datetime.fromisoformat(today)-datetime.fromisoformat(row['entry_session'])).days
                    verdict=news_rules.catalyst_review(st,profile,result,q['price'],gov,held_days,p,sector_pct)
                    st['last_review']=today;st['last_score']=result['score'] if result else None
                    transition(db,name,symbol,row['signal_id'],row['stage'],'REVIEWED' if not verdict else 'EXIT_REDUCE',
                        (verdict[1] if verdict else f"Thesis intact; score {st['last_score']}"),dict(score=st['last_score']),clock=clock,dedup=f'{name}:{symbol}:review:{today}')
                    if verdict:st['pending_exit']=list(verdict)
                db.execute('UPDATE sb_positions SET state=? WHERE portfolio=? AND symbol=?',(json.dumps(st),name,symbol))
            if st.get('pending_exit'):
                kind,why=st['pending_exit']
                if self.execute(name,symbol,'SELL',st['qty'],self.guard(name,self.exit_guard(name,symbol,kind,why),'delivery','SELL'),clock,realtime):fills+=1
        start,end=WINDOWS[name]
        with connect(self.path) as db:blocked=self.data_block(db,name,clock,p)
        if blocked:return dict(state='data_wait',message=blocked,fills_this_scan=fills)
        if halted or not start<=hm<end:return dict(state='monitoring',message=f'Monitoring holdings. New entries run {start}-{end} IST.' if not halted else '',fills_this_scan=fills)
        with connect(self.path) as db:
            for s in db.execute("SELECT id FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND expires_at<?",(name,today)).fetchall():
                set_status(db,s['id'],'EXPIRED','Candidate not bought before its expiry session',clock)
            candidates=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE' AND valid_from<=? ORDER BY score DESC",(name,today))]
            positions,buys,_=self.counts(db,name,clock)
        for s in candidates:
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:break
            q=quotes.get(s['symbol'])
            if not fresh(q,clock):continue
            with connect(self.path) as db:
                if db.execute('SELECT 1 FROM positions WHERE portfolio=? AND symbol=? AND quantity>0',(name,s['symbol'])).fetchone():continue
                cash,eq,_=valuation(db,name);initial=db.execute('SELECT initial_cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
                if eq is None or self.sector_pct(db,name,s['symbol'],eq)>=p['max_sector_pct']:continue
            budget=min(cash,initial*p['position_pct']/100);qty=math.floor(max(0,budget-30)/(q['price']*1.006))
            if qty<1:continue
            if self.execute(name,s['symbol'],'BUY',qty,self.guard(name,self.catalyst_entry_guard(name,s,p),'delivery','BUY',json.loads(s['details'])),clock,realtime):
                fills+=1;positions+=1;buys+=1
        with connect(self.path) as db:
            ready=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='BUY_CANDIDATE'",(name,)).fetchone()[0]
            watch=db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio=? AND status='WATCHLIST'",(name,)).fetchone()[0]
            positions,buys,_=self.counts(db,name,clock)
        return dict(state='monitoring',message=f"{ready} buy candidate(s), {watch} on the watchlist; {positions}/{p['max_positions']} holdings; {buys}/{p['max_entries_per_day']} buys today.",
            fills_this_scan=fills,position_count=positions,buy_count=buys)

    def sector_pct(self,db,name,symbol,equity):
        if not equity:return 0.
        group=sector_group((db.execute('SELECT sector FROM instruments WHERE symbol=?',(symbol,)).fetchone() or [None])[0],symbol=symbol)
        value=sum(r['value'] for r in db.execute('''SELECT p.symbol,i.sector,COALESCE(p.quantity*q.price,0) value FROM positions p JOIN instruments i USING(symbol) LEFT JOIN quotes q USING(symbol)
            WHERE p.portfolio=? AND p.quantity>0''',(name,)) if sector_group(r['sector'],symbol=r['symbol'])==group)
        return value/equity*100

    def catalyst_entry_guard(self,name,signal,p):
        def on_fill(db,order,quote,position,execution):
            current=db.execute('SELECT status,details FROM sb_signals WHERE id=?',(signal['id'],)).fetchone()
            if not current or current['status']!='BUY_CANDIDATE':raise ValueError('Candidate no longer valid.')
            if position and position['quantity']>0:raise ValueError('Already holding; no averaging.')
            fill=quote['price'];qty=quote.get('fill_quantity',order['quantity'])
            self.slippage_ok(db,order['symbol'],fill,'BUY',p['max_slippage_pct'])
            initial=db.execute('SELECT initial_cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
            if fill*qty+quote.get('fill_fee',0)>initial*p['position_pct']/100+1:raise ValueError('Position budget exceeded at the fill price.')
            positions,buys,_=self.counts(db,name,execution)
            if positions>=p['max_positions'] or buys>=p['max_entries_per_day']:raise ValueError('Holding or daily entry limit reached.')
            d=json.loads(current['details']);st=dict(entry=fill,average_cost=fill,qty=qty,original_qty=qty,entry_period=(d.get('profile') or {}).get('period'),
                entry_score=signal['score'],last_review=session(execution),news_id=signal['news_id'])
            db.execute('INSERT INTO sb_positions VALUES(?,?,?,?,?,?,?,?,?)',(name,order['symbol'],signal['id'],STRATEGY_VERSIONS[name],session(execution),execution.isoformat(),'OPEN',json.dumps(st),execution.isoformat()))
            set_status(db,signal['id'],'ENTRY_FILLED',f'Bought {qty} at {fill:.2f}',execution,quantity=qty)
            return f"{TITLES[name]}: {signal['explanation'][:180]}; PAPER"
        return on_fill
