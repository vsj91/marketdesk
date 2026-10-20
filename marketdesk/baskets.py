"""Live paper engine for the additional baskets (momentum, 12-1 momentum, alpha, Stocks on the Move, the research baskets, low volatility, overnight momentum, gap-up drift, sector rotation, 52-week high, mean reversion).

Rules live in basket_rules and are shared with scripts/backtest_baskets.py. Orders go through
paper.trade with an automatic guard, so the same fresh-quote, depth, data-quality, universe and
fee checks apply as for Trader and Investor. No broker order APIs."""
import collections,math,time,uuid
from datetime import datetime,timezone
from .storage import connect,get_state,set_state,now
from .sectors import sector_group
from .basket_rules import BASKETS,RETIRED,stats,ranking,exit_ranking,rebalance_exit,daily_exit,stop_exit
from .paper import trade
from .quote_health import fresh,refresh_held_quotes
from .buy_universe import allowed_symbols,require_buy_universe
from .working_universe import symbol_filter
from .price_reconciliation import confirmed,jumps
from . import research_live
from . import drawdown

# The server sets buy_universe to Nifty 500 for every basket while the working universe is enabled.
# A flat ₹20 brokerage plus DP charges make a delivery round trip about ₹80-90, so 1.5% blocks orders under ~₹5,800.
DEFAULT_CONFIG={'full_session_buying':True,'max_round_trip_fee_pct':1.5}
# Fee-dominated at ₹10,000 positions in the backtest; created paused so the reader opts in knowingly.
START_PAUSED={'reversion'}
# One-month live comparison (owner's request, 21 Oct 2026): baskets that failed their backtest and had been switched to
# exits-only or retired trade again on paper so every tested strategy has a live record beside its backtest. Applied once.
COMPARISON=dict(started='2026-10-21',baskets=('trader','lowvol','stat_arb','regime','news_momentum','ema_price_action','sector_rotation','reversion','high52'),
    reason='Comparison-only: failed its backtest; trading on paper from 21 Oct 2026 for a one-month live comparison.')

def init(path):
    with connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS basket_positions(portfolio TEXT NOT NULL,symbol TEXT NOT NULL,entry_session TEXT NOT NULL,
            reviewed_month TEXT NOT NULL,pending_exit TEXT,PRIMARY KEY(portfolio,symbol))''')
        cfg=get_state(db,'auto_config',{})
        for name in BASKETS:
            if name not in cfg:cfg[name]={**DEFAULT_CONFIG,'enabled':name not in START_PAUSED}
            if name in RETIRED:cfg[name].update(enabled=False,retired=True,retired_reason=RETIRED[name])
        if not get_state(db,'comparison_month'):
            for name in COMPARISON['baskets']:
                c=cfg.get(name)
                if not c or not (c.get('exits_only') or c.get('retired')):continue   # only lift what was switched off for a failed backtest
                c.update(enabled=True,exits_only=False,retired=False,comparison_only=True,comparison_reason=COMPARISON['reason'],comparison_at=now(),
                    previous=dict(enabled=c.get('enabled'),exits_only=c.get('exits_only'),exits_only_reason=c.get('exits_only_reason'),retired_reason=c.get('retired_reason')))
                c.pop('retired_reason',None)
            set_state(db,'comparison_month',dict(COMPARISON,applied_at=now()))
        set_state(db,'auto_config',cfg)

def rules(name,config):
    rule=dict(BASKETS[name])
    for key in ('max_positions','max_buys_per_day'):
        if type(config.get(key)) is int and config[key]>0:rule[key]=config[key]
    return rule

def valuation(db,portfolio):
    account=db.execute('SELECT * FROM portfolios WHERE name=?',(portfolio,)).fetchone();value=account['cash']
    for p in db.execute('SELECT symbol,quantity FROM positions WHERE portfolio=? AND quantity>0',(portfolio,)):
        q=db.execute('SELECT price FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
        if not q or not math.isfinite(q['price']) or q['price']<=0:return None
        value+=p['quantity']*q['price']
    return value

class BasketEngine:
    def __init__(self,path):
        self.path=path;init(path);self.signals={};self.loaded=0;self.loaded_day=None;self.research={};self.research_error=None

    def load(self,clock):
        from .autotrading import session
        today=session(clock)
        with connect(self.path) as db:
            groups=collections.defaultdict(list)
            for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db,include_holdings=True)+' AND substr(ts,1,10)<? ORDER BY symbol,ts',(today,)):
                groups[r['symbol']].append(dict(r))
            sectors={r[0]:sector_group(r[1],symbol=r[0]) for r in db.execute('SELECT symbol,sector FROM instruments')}
            has=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_benchmark'").fetchone()
            bench=dict(db.execute('SELECT day,close FROM claude_benchmark')) if has else {}   # Nifty 500 closes for alpha
            self.bad={r[0] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0")}
            self.bad.update(r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind IN ('candle','identity')"))
            # Large moves NSE verified as ordinary (the same proofs the data audit accepts) are genuine, not breaks.
            moves={s:confirmed(db,s,rows[-300:],clock) for s,rows in groups.items() if jumps(rows[-300:])}
        signals={s:stats(rows[-300:],moves.get(s,()),bench) for s,rows in groups.items()}
        for s,sig in signals.items():
            if sig:sig['sector']=sectors.get(s)                     # sector rotation groups by instrument sector
        latest=max((s['as_of'] for s in signals.values() if s),default='')
        # Only signals from the latest completed session are current; older ones are stale.
        self.signals={k:s for k,s in signals.items() if s and s['as_of']==latest}
        # Research baskets run the backtest's own rule code on the same completed candles (a few seconds, once per day).
        if not self.research or next(iter(self.research.values()))['as_of']!=latest:
            try:self.research=research_live.signals(self.path,today);self.research_error=None
            except Exception as error:self.research={};self.research_error=f'{type(error).__name__}: {error}'[:200]
        self.latest=latest;self.loaded=time.monotonic();self.loaded_day=today

    def research_signal(self,name):
        r=self.research.get(name)
        return r if r and r['as_of']==getattr(self,'latest',None) else None

    def candidates(self,name):
        if BASKETS[name].get('research'):
            r=self.research_signal(name)
            return [s for s in r['order'] if s not in self.bad] if r else []
        return ranking(name,{k:s for k,s in self.signals.items() if k not in self.bad})

    def stop_reason(self,name,symbol,average_cost,price,entry_session=None):
        if not BASKETS[name].get('research'):return stop_exit(name,average_cost,price)
        r=self.research_signal(name) or self.research.get(name)
        pct=research_live.entry_stop(r,symbol,entry_session or '9999') if r else None
        return f'{pct*100:g}% stop from average cost' if pct and price<=average_cost*(1-pct) else None

    def targets(self,db,name,clock,rule):
        """Buy list after the verified buy universe; the scan and the fill guard must agree."""
        allowed=allowed_symbols(db,name,clock)
        order=[s for s in self.candidates(name) if allowed is None or s in allowed]
        # Research baskets buy only from the top of today's ranking, as the backtest does, monthly or daily.
        return order,(order[:rule['max_positions']] if rule['rebalance']=='monthly' or rule.get('research') else order)

    def guard(self,name,clock):
        def check(db,order,quote,account,position):
            from .autotrading import entry_window_open,entry_window_label,day_start,session
            cfg=get_state(db,'auto_config',{}).get(name,{})
            if not cfg.get('enabled'):raise ValueError('Basket automation was paused.')
            execution=datetime.fromisoformat(quote['execution_time']) if quote.get('execution_time') else clock
            rule=rules(name,cfg);price=quote['price'];held=position['quantity'] if position else 0
            state=db.execute('SELECT * FROM basket_positions WHERE portfolio=? AND symbol=?',(name,order['symbol'])).fetchone()
            if order['side']=='SELL':
                reason=((state['pending_exit'] if state else None) or self.stop_reason(name,order['symbol'],position['cost']/held,price,state['entry_session'] if state else None)) if held else None
                if not reason or order['quantity']!=held:raise ValueError('Exit condition or held quantity changed; recheck next scan.')
                if quote.get('fill_quantity',held)==held:db.execute('DELETE FROM basket_positions WHERE portfolio=? AND symbol=?',(name,order['symbol']))
                return reason
            if not entry_window_open(execution,cfg):raise ValueError('New entries run '+entry_window_label(cfg)+'.')
            require_buy_universe(db,name,order['symbol'],execution)
            start=day_start(execution)
            if held or db.execute('SELECT 1 FROM trades WHERE portfolio=? AND symbol=? AND created_at>=?',(name,order['symbol'],start)).fetchone():raise ValueError('No averaging or same-session re-entry.')
            if db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]>=rule['max_positions']:raise ValueError('Position limit reached.')
            if db.execute("SELECT COUNT(*) FROM trades WHERE portfolio=? AND source='automatic' AND side='BUY' AND created_at>=?",(name,start)).fetchone()[0]>=rule['max_buys_per_day']:raise ValueError('Daily new-position limit reached.')
            _,targets=self.targets(db,name,execution,rule)
            signal=self.signals.get(order['symbol'])
            if order['symbol'] not in targets or not signal or abs(price/signal['close']-1)>.15 or price<20:raise ValueError('Basket entry conditions changed; re-evaluate next cycle.')
            r=self.research_signal(name) if rule.get('research') else None
            if r and r['stop_frame'] is not None and not (research_live.entry_stop(r,order['symbol'],session(execution)) or 0)>0:raise ValueError('No measurable stop for this entry.')
            value=price*quote.get('fill_quantity',order['quantity']);total=value+quote.get('fill_fee',0)
            if total>min(account['cash'],account['initial_cash']*rule['position_pct']/100):raise ValueError('Position budget exceeded at the latest price.')
            day=session(execution)
            db.execute('INSERT OR REPLACE INTO basket_positions VALUES(?,?,?,?,NULL)',(name,order['symbol'],day,day[:7]))
            return f"{rule['family']}: rank {targets.index(order['symbol'])+1} on completed session {signal['as_of']}; equal weight; paper only"
        return check

    def execute(self,name,symbol,side,qty,clock,realtime):
        from .autotrading import event
        if realtime:clock=datetime.now(timezone.utc)
        try:
            trade(self.path,dict(portfolio=name,symbol=symbol,side=side,quantity=qty,request_id=f'basket-{name}-{uuid.uuid4()}'),None if realtime else clock,auto_guard=self.guard(name,clock))
            return 1
        except ValueError as error:
            with connect(self.path) as db:event(db,name,'blocked',str(error),symbol,clock=clock,details=getattr(error,'order_audit',None))
            return 0

    def review(self,db,name,clock):
        """Mark monthly-rebalance and daily completed-close exits; returns holdings with a pending exit."""
        from .autotrading import session
        today=session(clock);month=today[:7]
        order=[] if BASKETS[name].get('research') else exit_ranking(name,{k:s for k,s in self.signals.items() if k not in self.bad})
        sessions=get_state(db,'quality_sessions',[])
        for p in db.execute('SELECT * FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchall():
            state=db.execute('SELECT * FROM basket_positions WHERE portfolio=? AND symbol=?',(name,p['symbol'])).fetchone()
            if not state:
                # A holding without basket state (manual buy while paused) is reviewed from today.
                db.execute('INSERT INTO basket_positions VALUES(?,?,?,?,NULL)',(name,p['symbol'],today,month));continue
            if state['pending_exit']:continue
            signal=self.signals.get(p['symbol']);reason=None
            if BASKETS[name].get('research'):
                r=self.research_signal(name)
                if not r:continue                                        # no current research signal: stops still run
                held=sum(1 for d in sessions if state['entry_session']<=d<today)
                if r['monthly'] and state['reviewed_month']<month:
                    if p['symbol'] not in r['keep_order'][:r['keep']]:reason=f"Outside the top {r['keep']} at monthly review" if r['keep']<10**6 else 'Left the ranked universe at monthly review'
                    else:db.execute('UPDATE basket_positions SET reviewed_month=? WHERE portfolio=? AND symbol=?',(month,name,p['symbol']))
                if not reason and p['symbol'] in r['exit_now'] and r['as_of']>=state['entry_session']:reason='Exit rule met on the completed session'
                if not reason and r['max_hold'] and held>=r['max_hold']:reason=f"Held {r['max_hold']} sessions"
            elif BASKETS[name]['rebalance']=='monthly':
                if state['reviewed_month']<month:
                    reason=rebalance_exit(name,p['symbol'],signal,order)
                    if not reason:db.execute('UPDATE basket_positions SET reviewed_month=? WHERE portfolio=? AND symbol=?',(month,name,p['symbol']))
            else:
                held=sum(1 for d in sessions if state['entry_session']<=d<today)
                if signal and signal['as_of']>=state['entry_session']:reason=daily_exit(name,signal,held)
            if reason:db.execute('UPDATE basket_positions SET pending_exit=? WHERE portfolio=? AND symbol=?',(reason,name,p['symbol']))

    def scan(self,name,clock,realtime=False):
        from .autotrading import entry_window_open,entry_window_label,daily_buy_count,event,session,day_start
        if self.loaded_day!=session(clock) or time.monotonic()-self.loaded>300:self.load(clock)
        if realtime:refresh_held_quotes(self.path,name)
        fills=0
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if realtime:clock=datetime.now(timezone.utc)
            cfg=get_state(db,'auto_config',{}).get(name,{});rule=rules(name,cfg)
            if self.signals:self.review(db,name,clock)
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            held=[dict(r) for r in db.execute('SELECT p.*,b.pending_exit,b.entry_session FROM positions p LEFT JOIN basket_positions b ON b.portfolio=p.portfolio AND b.symbol=p.symbol WHERE p.portfolio=? AND p.quantity>0',(name,))]
            account=db.execute('SELECT * FROM portfolios WHERE name=?',(name,)).fetchone()
            marked=valuation(db,name)
            halted,info=drawdown.gate(db,name,marked,account['initial_cash'],cfg)
        for p in held:
            q=quotes.get(p['symbol'])
            if fresh(q,clock) and (p['pending_exit'] or self.stop_reason(name,p['symbol'],p['cost']/p['quantity'],q['price'],p['entry_session'])):
                fills+=self.execute(name,p['symbol'],'SELL',p['quantity'],clock,realtime)
        def status(message,state='monitoring',**extra):return dict(state=state,message=message,fills_this_scan=fills,**extra)
        pending=[p['symbol'] for p in held if p['pending_exit']]
        if halted:return status(drawdown.message(info),'risk_wait')
        if not self.signals:return status('Waiting for completed daily history; new buys blocked. Exits remain active.','data_wait')
        if rule.get('research') and not self.research_signal(name):
            return status('Research signals unavailable'+(f' ({self.research_error})' if self.research_error else ' for the latest session')+'; new buys wait. Stops and exits still run.','data_wait')
        if not entry_window_open(clock,cfg):return status('Monitoring exits. New entries run '+entry_window_label(cfg)+'.')
        try:
            with connect(self.path) as db:order,targets=self.targets(db,name,clock,rule)
        except ValueError as error:return status(str(error),'data_wait')
        for symbol in targets:
            with connect(self.path) as db:
                account=db.execute('SELECT * FROM portfolios WHERE name=?',(name,)).fetchone()
                count=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
                if count>=rule['max_positions'] or daily_buy_count(db,name,clock)>=rule['max_buys_per_day']:break
                if db.execute('SELECT 1 FROM positions WHERE portfolio=? AND symbol=? AND quantity>0',(name,symbol)).fetchone():continue
                if db.execute('SELECT 1 FROM trades WHERE portfolio=? AND symbol=? AND created_at>=?',(name,symbol,day_start(clock))).fetchone():continue
            q=quotes.get(symbol)
            if not fresh(q,clock):continue
            budget=min(account['cash'],account['initial_cash']*rule['position_pct']/100)
            qty=max(0,math.floor((budget-25)/(q['price']*1.025)))
            if qty>0:fills+=self.execute(name,symbol,'BUY',qty,clock,realtime)
            else:
                with connect(self.path) as db:event(db,name,'blocked','No whole share fits the equal-weight position budget.',symbol,clock=clock,details=dict(blocked_by='position_cap',requested_quantity=0,filled_quantity=0,risk_used_inr=0,position_budget=budget))
        with connect(self.path) as db:
            positions=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0];buys=daily_buy_count(db,name,clock)
        note=f' Pending exits: {", ".join(pending)}.' if pending else ''
        return status(f"{len(order)} ranked candidates; {positions}/{rule['max_positions']} holdings; {buys}/{rule['max_buys_per_day']} new buys today."+note,
            candidates=len(order),position_count=positions,buy_count=buys,max_positions=rule['max_positions'],max_buys_per_day=rule['max_buys_per_day'])
