"""Live execution for the quant baskets: queued orders fill at live prices during market hours.

The daily runner (quant.runner) still computes signals from completed daily candles and queues orders in
the quant ledger. This module works those orders off inside the auto-trading loop, every scan, through
paper.trade with an automatic guard, so the same checks apply as for Trader and Investor: connected feed
and open NSE session, trade price within 30 seconds, fresh bid/ask depth with the participation and price
limits, data-quality and Nifty 500 universe checks for buys, delivery charges and the round-trip fee limit.

Fills are recorded in the main paper ledger first. Each paper order's request id carries the quant order
id, so the quantity still to fill is always derived from the main ledger: a crash between the two
databases can delay the quant ledger's bookkeeping but can never fill the same order twice."""
import json,math,uuid
from datetime import datetime,timezone
from ..storage import connect,get_state,set_state
from ..quote_health import fresh,refresh_held_quotes
from ..buy_universe import require_buy_universe
from .. import drawdown
from .engine import BasketEngine
from .names import LIVE_BASKETS,TITLES
from .runner import ledger_path

# A flat ₹20 brokerage plus DP charges make a delivery round trip about ₹80-90, so 1.5% blocks orders under ~₹5,800.
DEFAULT_CONFIG={'buy_universe':'nifty500','full_session_buying':True,'max_round_trip_fee_pct':1.5}
MAX_SIGNAL_DRIFT=.15     # skip a buy when the live price is more than 15% away from the signal close
CASH_BUFFER=50           # rupees kept back for rounding of charges

SUMMARY={
 'smart_beta':('Multi-factor smart beta · daily signals · live fills','Multi-factor (value, quality, 12-1 momentum, low volatility)',
   'Top 15 Nifty 500 stocks by a value/quality/momentum/low-volatility composite, ROE above 15% and cash-backed profit, at most 30% per sector. Equal weight. Rebalanced monthly.',
   'Sold at the monthly rebalance when a stock leaves the top 15 or fails the quality screen. A 25% loss from average cost (completed close) also exits, checked every session.'),
 'risk_parity':('Risk parity · daily signals · live fills','Inverse-volatility weighting',
   'Top 20 multi-factor names weighted by 1 / 60-day volatility, at most 30% per sector. Rebalanced every two weeks; weights are only changed when they drift more than 2 points.',
   'Sold at a rebalance when a stock leaves the selection. A 25% loss from average cost (completed close) also exits, checked every session.'),
 'stat_arb':('Stat-arb mean reversion · daily signals · live fills','Statistical mean reversion inside an uptrend',
   'Large and mid caps with positive ROE and operating cash flow whose close is 2+ standard deviations below its 20-day mean while the 200-day average rises. 10 slots of 10%.',
   'Sold when the close returns to its 20-day mean (Z ≥ 0) or stops out below Z = −3.2. Checked on every completed session.'),
 'regime':('Regime switching · daily signals · live fills','Macro regime allocation',
   'Nifty 500 trend and volatility pick the mix: bull 70% momentum / 30% quality, choppy 50% quality / 50% low volatility, bear 60% low-beta defensive / 40% deep value. A regime change needs 3 sessions of confirmation.',
   'Sold at the monthly rebalance or on a confirmed regime change when a stock leaves the new sleeves. A 25% loss from average cost (completed close) also exits, checked every session.'),
}

def rules(name,config=None):
    title,family,entry,exit_=SUMMARY[name]
    return dict(name=title,family=family,entry=entry+' Orders are queued from completed daily candles and fill during the next market session at live prices and real depth.',
        exit=exit_,risk='Paper only. Buys need the Nifty 500 universe, data checks, fresh depth and estimated round-trip fees within the configured limit; a 15% fall from the equity peak pauses new buys while exits continue.',
        max_positions={'smart_beta':15,'risk_parity':20,'stat_arb':10,'regime':16}[name],position_pct=None,max_buys_per_day=None,rebalance=True)

def uneconomic(cfg,value):
    from ..execution import uneconomic as check
    return check(cfg,value)

def init_config(path):
    with connect(path) as db:
        cfg=get_state(db,'auto_config',{})
        for name in LIVE_BASKETS:
            if name not in cfg:cfg[name]={**DEFAULT_CONFIG,'enabled':True}
        set_state(db,'auto_config',cfg)

class QuantLive:
    def __init__(self,path,ledger=None):
        self.path=path;self.engine=BasketEngine(ledger or ledger_path(path),live_db=path);init_config(path)

    @staticmethod
    def prefix(name,order_id):return f'quant-{name}-{order_id}-'

    def filled(self,db,name,order_id):
        """Shares already filled for a quant order, from the main paper ledger (the source of truth)."""
        return db.execute('SELECT COALESCE(SUM(quantity),0) FROM trades WHERE portfolio=? AND request_id LIKE ?',(name,self.prefix(name,order_id)+'%')).fetchone()[0]

    def pending(self,name):
        with connect(self.engine.path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM qb_orders WHERE basket=? AND status='pending' ORDER BY side='BUY',id",(name,))]

    def guard(self,name,order,clock):
        def check(db,payload,quote,account,position):
            from ..autotrading import entry_window_open,entry_window_label
            cfg=get_state(db,'auto_config',{}).get(name,{})
            if not cfg.get('enabled'):raise ValueError('Basket automation was paused.')
            remaining=order['qty']-self.filled(db,name,order['id'])
            if payload['quantity']>remaining:raise ValueError('Quant order already filled; nothing left to trade.')
            execution=datetime.fromisoformat(quote['execution_time']) if quote.get('execution_time') else clock
            if order['side']=='BUY':
                if not entry_window_open(execution,cfg):raise ValueError('New entries run '+entry_window_label(cfg)+'.')
                require_buy_universe(db,name,order['symbol'],execution)
            return f"{TITLES[name]}: {order['reason']} (signal {order['signal_day']}); daily signal, live fill; paper only"
        check.audit_strategy='quant-'+name
        return check

    def record(self,name,order,trade_id,clock):
        """Book a main-ledger fill into the quant ledger: fill row, holdings mirror, cash, order status, audit log."""
        from ..autotrading import session
        with connect(self.path) as live:
            t=dict(live.execute('SELECT * FROM trades WHERE id=?',(trade_id,)).fetchone())
            pos=live.execute('SELECT quantity,cost FROM positions WHERE portfolio=? AND symbol=?',(name,order['symbol'])).fetchone()
            cash=live.execute('SELECT cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
            done=self.filled(live,name,order['id'])
        execution=json.loads(t.get('execution') or '{}');day=session(clock)
        bps=execution.get('slippage_vs_ltp_bps') or 0.;ref=t['price']/(1+(bps if t['side']=='BUY' else -bps)/1e4)
        with connect(self.engine.path) as db:
            db.execute('BEGIN IMMEDIATE')
            mirror=db.execute('SELECT qty,cost FROM qb_positions WHERE basket=? AND symbol=?',(name,order['symbol'])).fetchone()
            realized=None
            if t['side']=='SELL' and mirror and mirror['qty']>0:
                realized=round(t['quantity']*t['price']-t['fee']-mirror['cost']*t['quantity']/mirror['qty'],2)
            db.execute('INSERT INTO qb_fills(order_id,basket,day,symbol,side,qty,price,reference_price,slippage_bps,impact_bps,fee,realized_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (order['id'],name,day,order['symbol'],t['side'],t['quantity'],t['price'],round(ref,4),round(bps,2),0.,t['fee'],realized))
            if pos and pos['quantity']>0:
                details=json.loads(order['details'] or '{}')
                db.execute('''INSERT INTO qb_positions(basket,symbol,qty,cost,entry_price,entry_day,high_close,meta,score) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(basket,symbol) DO UPDATE SET qty=excluded.qty,cost=excluded.cost,entry_price=excluded.entry_price''',
                    (name,order['symbol'],pos['quantity'],pos['cost'],pos['cost']/pos['quantity'],day,t['price'],json.dumps(details.get('meta',{})),json.dumps(details.get('score',{}))))
            else:db.execute('DELETE FROM qb_positions WHERE basket=? AND symbol=?',(name,order['symbol']))
            db.execute('UPDATE qb_baskets SET cash=? WHERE name=?',(cash,name))
            if done>=order['qty']:db.execute("UPDATE qb_orders SET status='filled',fill_day=? WHERE id=?",(day,order['id']))
            verb='Bought' if t['side']=='BUY' else 'Sold'
            self.engine._log(db,name,day,order['symbol'],'FILL',None,None,
                f"{verb} {t['quantity']} {order['symbol']} @ {t['price']:.2f} live ({bps:+g} bps vs last trade, charges Rs {t['fee']:.2f})"
                +(f"; {order['qty']-done} of {order['qty']} still to fill" if done<order['qty'] else '')
                +(f"; realized P&L Rs {realized:,.2f}" if realized is not None else ''),dict(order_id=order['id'],trade_id=trade_id))

    def scan(self,name,clock,realtime=False):
        from ..paper import trade
        from ..autotrading import entry_window_open,event
        if not self.engine.live(name):return dict(state='data_wait',message='Waiting for the daily run to start this basket.')
        if realtime:refresh_held_quotes(self.path,name)
        orders=self.pending(name);fills=0;waiting=[]
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if realtime:clock=datetime.now(timezone.utc)
            cfg=get_state(db,'auto_config',{}).get(name,{})
            account=db.execute('SELECT * FROM portfolios WHERE name=?',(name,)).fetchone()
            held={r['symbol']:r['quantity'] for r in db.execute('SELECT symbol,quantity FROM positions WHERE portfolio=? AND quantity>0',(name,))}
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            marked=account['cash']+sum(q*quotes[s]['price'] for s,q in held.items() if s in quotes) if all(s in quotes for s in held) else None
            halted,info=drawdown.gate(db,name,marked,account['initial_cash'],cfg)
            remaining={o['id']:o['qty']-self.filled(db,name,o['id']) for o in orders}
        for o in orders:
            left=remaining[o['id']]
            if left<=0:
                self.record_done(name,o,clock);continue
            q=quotes.get(o['symbol'])
            if not fresh(q,clock):waiting.append(o['symbol']);continue
            if o['side']=='SELL':
                qty=min(left,held.get(o['symbol'],0))
                if qty<=0:
                    self.cancel(name,o,clock,'nothing left to sell');continue
            else:
                if halted or not entry_window_open(clock,cfg):continue
                signal=json.loads(o['details'] or '{}').get('signal_close')
                if signal and abs(q['price']/signal-1)>MAX_SIGNAL_DRIFT:waiting.append(o['symbol']);continue
                with connect(self.path) as db:cash=db.execute('SELECT cash FROM portfolios WHERE name=?',(name,)).fetchone()[0]
                qty=min(left,math.floor(max(0,cash-CASH_BUFFER)/(q['price']*1.006)))
                if qty<1:waiting.append(o['symbol']);continue
            try:
                result=trade(self.path,dict(portfolio=name,symbol=o['symbol'],side=o['side'],quantity=qty,request_id=self.prefix(name,o['id'])+uuid.uuid4().hex[:12]),
                    None if realtime else clock,auto_guard=self.guard(name,o,clock))
                self.record(name,o,result['id'],clock);fills+=1
                if o['side']=='SELL':held[o['symbol']]=held.get(o['symbol'],0)-result['quantity']
            except ValueError as error:
                with connect(self.path) as db:event(db,name,'blocked',str(error),o['symbol'],clock=clock,details=getattr(error,'order_audit',None))
                # A buy that fails the fee floor even at its full remaining size can never fill; stop retrying it every scan.
                if o['side']=='BUY' and str(error).startswith('Buy too small for fee floor') and uneconomic(cfg,left*q['price']):
                    self.cancel(name,o,clock,f"too small for the fee floor even at its full remaining size (Rs {left*q['price']:,.0f})")
        left=self.pending(name)
        with connect(self.path) as db:count=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone()[0]
        if halted:return dict(state='risk_wait',message=drawdown.message(info),fills_this_scan=fills,position_count=count)
        note=f" Waiting for a fresh price or a smaller gap from the signal close: {', '.join(sorted(set(waiting))[:8])}." if waiting else ''
        return dict(state='monitoring',message=f"{len(left)} queued order(s) from the daily plan; {count} holdings; {fills} filled this scan.{note}" if left else
            f"All queued orders done; {count} holdings. The next plan is built from the next completed session.",fills_this_scan=fills,position_count=count,queued_orders=len(left))

    def record_done(self,name,order,clock):
        """An order the main ledger shows as complete (e.g. after a crash before bookkeeping): close it in the quant ledger."""
        from ..autotrading import session
        with connect(self.path) as live:
            trades=[r[0] for r in live.execute('SELECT id FROM trades WHERE portfolio=? AND request_id LIKE ?',(name,self.prefix(name,order['id'])+'%'))]
        with connect(self.engine.path) as db:
            booked={json.loads(r[0]).get('trade_id') for r in db.execute("SELECT details FROM qb_rebalance_log WHERE basket=? AND action='FILL'",(name,))}
        for t in trades:
            if t not in booked:self.record(name,order,t,clock)
        with connect(self.engine.path) as db:db.execute("UPDATE qb_orders SET status='filled',fill_day=? WHERE id=? AND status='pending'",(session(clock),order['id']))

    def cancel(self,name,order,clock,why):
        from ..autotrading import session
        with connect(self.engine.path) as db:
            db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(session(clock),order['id']))
            self.engine._log(db,name,session(clock),order['symbol'],'CANCEL',None,None,f"{order['side'].title()} {order['symbol']} cancelled: {why}")
