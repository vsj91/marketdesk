"""BasketEngine: paper ledger and end-of-day pipeline for the quant baskets.

One call to run_day(snapshot) per completed session, for every basket, in this order:
  1. fill orders queued on the previous session at today's typical price (ExecutionSimulator)
  2. update the highest close since entry for trailing stops
  3. if the basket's schedule says so, run its strategy on today's completed data and queue
     orders for the next session: exits always, new names always, resizes only outside the drift band
  4. snapshot NAV, cash and constituent weights; write the audit log; mark the run complete

Signals use completed closes and fill one session later, so no fill ever uses the price that
produced its own signal. Reruns of the same (basket, session) are no-ops."""
import json,math
from datetime import date
from ..storage import connect,now
from .schema import SCHEMA
from .strategies import STRATEGIES,config,features
from .execution import ExecutionSimulator,drift_orders
from .analytics import performance,RISK_FREE
from . import calc

def j(x):return json.dumps(x,sort_keys=True,default=float)

class BasketEngine:
    def __init__(self,ledger_path,simulator=None,drift_threshold=.02,risk_free=RISK_FREE):
        self.path=ledger_path;self.sim=simulator or ExecutionSimulator();self.drift=drift_threshold;self.risk_free=risk_free
        with connect(self.path) as db:db.executescript(SCHEMA)

    # ------------------------------------------------------------ definitions

    def create(self,name,strategy,capital=100_000,overrides=None,day=None):
        """Register a basket; an existing name is left unchanged. Returns True when created."""
        if strategy not in STRATEGIES:raise ValueError(f'Unknown strategy {strategy!r}; choose one of {", ".join(STRATEGIES)}.')
        if not (calc.finite(capital) and capital>0):raise ValueError('Capital must be a positive number.')
        config(strategy,overrides)
        with connect(self.path) as db:
            cur=db.execute('INSERT OR IGNORE INTO qb_baskets(name,strategy,config,capital,cash,created_day) VALUES(?,?,?,?,?,?)',
                (name,strategy,j(overrides or {}),float(capital),float(capital),day or date.today().isoformat()))
            return cur.rowcount==1

    def retire(self,name,day,reason):
        """Stop a basket for good without deleting history: cancel pending buys, queue exits for every
        holding (filled next session like any other order) and never run its strategy again.
        Returns False if it was already retired."""
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            b=db.execute('SELECT * FROM qb_baskets WHERE name=?',(name,)).fetchone()
            if not b:raise ValueError(f'No basket named {name!r}.')
            state=json.loads(b['state'])
            if state.get('retired'):return False
            for o in db.execute("SELECT * FROM qb_orders WHERE basket=? AND status='pending' AND side='BUY'",(name,)).fetchall():
                db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(day,o['id']))
                self._log(db,name,day,o['symbol'],'CANCEL',None,None,f"Buy {o['symbol']} cancelled: basket retired")
            selling={r[0] for r in db.execute("SELECT symbol FROM qb_orders WHERE basket=? AND status='pending' AND side='SELL'",(name,))}
            for p in db.execute('SELECT * FROM qb_positions WHERE basket=?',(name,)).fetchall():
                if p['symbol'] in selling:continue
                db.execute('INSERT INTO qb_orders(basket,signal_day,symbol,side,qty,kind,reason,details) VALUES(?,?,?,?,?,?,?,?)',
                    (name,day,p['symbol'],'SELL',p['qty'],'exit','Basket retired',j({})))
                self._log(db,name,day,p['symbol'],'EXIT',None,0.,f"Exit {p['symbol']}: basket retired")
            state.update(retired=True,retired_day=day,retired_reason=reason);state.pop('targets',None)
            db.execute('UPDATE qb_baskets SET state=? WHERE name=?',(j(state),name))
            self._log(db,name,day,None,'NOTE',None,None,f'Basket retired: {reason}')
            return True

    def names(self):
        with connect(self.path) as db:return [r[0] for r in db.execute('SELECT name FROM qb_baskets ORDER BY name')]

    # ------------------------------------------------------------ pipeline

    def run_day(self,snapshot):
        return {name:self.run_basket(name,snapshot) for name in self.names()}

    def run_basket(self,name,snapshot):
        day=snapshot['day']
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            basket=db.execute('SELECT * FROM qb_baskets WHERE name=?',(name,)).fetchone()
            if not basket:raise ValueError(f'No basket named {name!r}.')
            if day<basket['created_day']:return dict(skipped=f'before basket start {basket["created_day"]}')
            if db.execute('SELECT 1 FROM qb_runs WHERE basket=? AND day=?',(name,day)).fetchone():return dict(skipped='already ran')
            last=db.execute('SELECT MAX(day) FROM qb_runs WHERE basket=?',(name,)).fetchone()[0]
            if last and day<last:return dict(skipped=f'out of order: last run {last}')
            cfg=config(basket['strategy'],json.loads(basket['config']));state=json.loads(basket['state'])
            fills,turnover,fees=self._fill_pending(db,basket,snapshot)
            for p in db.execute('SELECT symbol,high_close FROM qb_positions WHERE basket=?',(name,)).fetchall():
                bar=self._bar(snapshot,p['symbol'])
                if bar:db.execute('UPDATE qb_positions SET high_close=MAX(COALESCE(high_close,0),?) WHERE basket=? AND symbol=?',(bar['close'],name,p['symbol']))
            book=self._book(db,name);prices=self._prices(snapshot,book);cash=self._cash(db,name)
            nav=cash+sum(p['qty']*prices[s] for s,p in book.items())
            due,why=self._due(basket,cfg,state,snapshot)
            if state.get('retired'):due=False      # retired: pending exits still fill and NAV is still marked
            targets={};queued=0;ran=False;reg=state.get('regime')
            if due:
                plan=self._plan(basket['strategy'],snapshot,book,dict(cfg,current_regime=state.get('regime')),nav)
                reg=plan['meta'].get('regime',reg)
                if basket['strategy']=='regime' and reg and reg!=state.get('regime'):
                    self._log(db,name,day,None,'REGIME',None,None,f"Regime {state.get('regime') or 'unset'} -> {reg}",plan['meta'].get('regime_detail'))
                for note in [plan['meta'].get('note'),*plan['meta'].get('notes',[])]:
                    if note:self._log(db,name,day,None,'NOTE',None,None,note)
                # An empty plan for an empty book (e.g. not enough history yet) does not use up the schedule.
                if not plan.get('hold') and (plan['targets'] or book):
                    targets,queued=self._queue(db,name,day,plan,book,prices,nav,snapshot,why,cash);ran=True
                    db.execute('UPDATE qb_baskets SET last_rebalance=? WHERE name=?',(day,name))
                state['regime']=reg
            if ran:state['targets']=targets
            else:targets=state.get('targets',{})      # between rebalances the last targets still apply
            db.execute('UPDATE qb_baskets SET state=? WHERE name=?',(j(state),name))
            book=self._book(db,name)
            invested=sum(p['qty']*prices.get(s,p['entry_price']) for s,p in book.items())
            bench=(snapshot.get('benchmark') or [None])[-1] if snapshot.get('benchmark_day',day)==day else None
            db.execute('INSERT INTO qb_nav VALUES(?,?,?,?,?,?,?,?,?)',(name,day,round(cash+invested,2),round(cash,2),round(invested,2),bench,reg,round(turnover,2),round(fees,2)))
            for s,p in book.items():
                px=prices.get(s,p['entry_price']);t=targets.get(s)
                db.execute('INSERT INTO qb_weights VALUES(?,?,?,?,?,?,?,?)',(name,day,s,p['qty'],px,(p['qty']*px)/(cash+invested),t,p['score_json']))
            summary=dict(nav=round(cash+invested,2),cash=round(cash,2),holdings=len(book),fills=fills,orders_queued=queued,
                rebalanced=ran,reason=why if ran else None,regime=reg)
            db.execute('INSERT INTO qb_runs VALUES(?,?,?,?)',(name,day,now(),j(summary)))
            return summary

    def _due(self,basket,cfg,state,snapshot):
        day=snapshot['day'];last=basket['last_rebalance'];sched=cfg['rebalance']
        if basket['strategy']=='regime':
            reg=calc.confirmed_regime(snapshot.get('benchmark') or [],state.get('regime'),cfg['confirm_days'],snapshot.get('vix'))
            if reg and state.get('regime') and reg['regime']!=state['regime']:return True,f"regime change {state['regime']} -> {reg['regime']} (confirmed {cfg['confirm_days']} sessions)"
        if last is None:return True,'initial construction'
        if sched=='daily':return True,'daily EOD scan'
        if sched=='monthly' and day[:7]!=last[:7]:return True,'monthly rebalance'
        if sched=='biweekly' and (date.fromisoformat(day)-date.fromisoformat(last)).days>=14:return True,'bi-weekly rebalance'
        return False,None

    def _plan(self,strategy,snapshot,book,cfg,nav):
        fn=STRATEGIES[strategy]
        return fn(snapshot,book,cfg,nav) if strategy=='trend' else fn(snapshot,book,cfg)

    def _queue(self,db,name,day,plan,book,prices,nav,snapshot,why,cash):
        feats=features(snapshot);size=snapshot.get('size',{})
        pending={r['symbol']:r['qty'] for r in db.execute("SELECT symbol,SUM(qty) qty FROM qb_orders WHERE basket=? AND status='pending' AND side='SELL' GROUP BY symbol",(name,))}
        def details(s,**extra):
            f=feats.get(s,{})
            return j(dict(size=size.get(s),adv20=f.get('adv20'),daily_vol=f.get('daily_vol'),signal_close=prices.get(s) or f.get('close'),**extra))
        def order(s,side,qty,kind,reason,**extra):
            db.execute('INSERT INTO qb_orders(basket,signal_day,symbol,side,qty,kind,reason,details) VALUES(?,?,?,?,?,?,?,?)',(name,day,s,side,qty,kind,reason,details(s,**extra)))
        queued=0;targets_w={}
        exits=dict(plan['exits'])
        for s in book:
            if s not in plan['targets'] and s not in exits:exits[s]='Dropped from the target basket'
        for s,reason in exits.items():
            if s not in book:continue
            qty=book[s]['qty']-pending.get(s,0);w=book[s]['qty']*prices[s]/nav;targets_w[s]=0.
            if qty>0:
                order(s,'SELL',qty,'exit',reason);queued+=1
                self._log(db,name,day,s,'EXIT',w,0.,f'Exit {s}: {reason}')
        targets={s:t for s,t in plan['targets'].items() if s not in exits}
        for s,t in targets.items():
            if s in feats and s not in prices:prices[s]=feats[s]['close']
        holdings={s:p['qty'] for s,p in book.items() if s not in exits}
        resized=set();unfunded=[]
        orders=drift_orders(targets,holdings,prices,nav,self.drift)
        # Buys are funded from cash plus the proceeds of today's sells, in plan order; the rest wait.
        budget=cash+sum(prices[s]*(book[s]['qty']-pending.get(s,0)) for s in exits if s in book)+sum(-d*prices[s] for s,d,*_ in orders if d<0)
        for s,delta,before,after,kind in orders:
            t=targets[s];resized.add(s)
            if delta>0:
                cost=delta*prices[s]*1.005
                if cost>budget:unfunded.append(s);continue
                budget-=cost
            if kind=='add':
                order(s,'BUY',delta,kind,t['reason'],meta=t.get('meta',{}),score=t.get('score',{}),target_weight=after)
                self._log(db,name,day,s,'ADD',0.,after,f"Added {s}: {t['reason']}",t.get('score'))
            else:
                side='BUY' if delta>0 else 'SELL';verb='Raised' if delta>0 else 'Trimmed'
                order(s,side,abs(delta),kind,t['reason'],score=t.get('score',{}),target_weight=after)
                self._log(db,name,day,s,kind.upper(),before,after,f"{verb} {s} by {abs(after-before)*100:.1f}% ({before:.1%} -> {after:.1%}): {t['reason']}",t.get('score'))
            queued+=1
        for s,t in targets.items():
            px=prices.get(s)
            if 'weight' in t:targets_w[s]=round(t['weight'],6)
            elif px:targets_w[s]=round((holdings.get(s,0) if t.get('keep') else t['shares'])*px/nav,6)
            if s in book:db.execute('UPDATE qb_positions SET score=? WHERE basket=? AND symbol=?',(j(t.get('score',{})),name,s))
        if unfunded:
            self._log(db,name,day,None,'NOTE',None,None,f"{len(unfunded)} buy(s) not queued: no free cash after sells ({', '.join(unfunded[:10])})",dict(symbols=unfunded))
        steady=[s for s in holdings if s in targets and s not in resized]
        if steady:
            self._log(db,name,day,None,'HOLD',None,None,f"{len(steady)} holding(s) kept: inside the ±{self.drift:.0%} drift band or no resize rule ({why})",dict(symbols=sorted(steady)))
        return targets_w,queued

    def _fill_pending(self,db,basket,snapshot):
        name=basket['name'];day=snapshot['day'];fills=0;turnover=fees=0.
        rows=db.execute("SELECT * FROM qb_orders WHERE basket=? AND status='pending' AND signal_day<? ORDER BY side='BUY',id",(name,day)).fetchall()
        for o in rows:
            d=json.loads(o['details']);bar=self._bar(snapshot,o['symbol'])
            pos=db.execute('SELECT * FROM qb_positions WHERE basket=? AND symbol=?',(name,o['symbol'])).fetchone()
            if o['side']=='SELL':
                qty=min(o['qty'],pos['qty'] if pos else 0)
                if qty<=0:
                    db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(day,o['id']));continue
                if not bar:continue           # exits carry over until the stock prints again
            else:
                if not bar:
                    db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(day,o['id']))
                    self._log(db,name,day,o['symbol'],'CANCEL',None,None,f"Buy {o['symbol']} cancelled: no session print on {day}");continue
                qty=min(o['qty'],self.sim.affordable(self._cash(db,name),calc.typical_price(bar),d.get('size')))
                if qty<=0:
                    db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(day,o['id']))
                    self._log(db,name,day,o['symbol'],'CANCEL',None,None,f"Buy {o['symbol']} cancelled: insufficient cash after charges");continue
            f=self.sim.fill(o['side'],qty,bar,d.get('size'),d.get('adv20'),d.get('daily_vol'))
            if not f:
                if o['side']=='BUY':db.execute("UPDATE qb_orders SET status='cancelled',fill_day=? WHERE id=?",(day,o['id']))
                continue
            realized=None
            if o['side']=='BUY':
                if pos:
                    q=pos['qty']+f['quantity']
                    db.execute('UPDATE qb_positions SET qty=?,cost=?,entry_price=? WHERE basket=? AND symbol=?',(q,pos['cost']+f['value']+f['fee'],(pos['entry_price']*pos['qty']+f['value'])/q,name,o['symbol']))
                else:
                    db.execute('INSERT INTO qb_positions(basket,symbol,qty,cost,entry_price,entry_day,high_close,meta,score) VALUES(?,?,?,?,?,?,?,?,?)',
                        (name,o['symbol'],f['quantity'],f['value']+f['fee'],f['value']/f['quantity'],day,bar['close'],j(d.get('meta',{})),j(d.get('score',{}))))
            else:
                share=pos['cost']*f['quantity']/pos['qty'];realized=round(f['cash_delta']-share,2)
                if f['quantity']==pos['qty']:db.execute('DELETE FROM qb_positions WHERE basket=? AND symbol=?',(name,o['symbol']))
                else:db.execute('UPDATE qb_positions SET qty=qty-?,cost=cost-? WHERE basket=? AND symbol=?',(f['quantity'],share,name,o['symbol']))
            db.execute('UPDATE qb_baskets SET cash=cash+? WHERE name=?',(f['cash_delta'],name))
            remaining=o['qty']-f['quantity']
            if remaining>0 and o['side']=='SELL':db.execute('UPDATE qb_orders SET qty=? WHERE id=?',(remaining,o['id']))
            else:db.execute('UPDATE qb_orders SET status=?,fill_day=? WHERE id=?',('partial' if f['partial'] else 'filled',day,o['id']))
            db.execute('INSERT INTO qb_fills(order_id,basket,day,symbol,side,qty,price,reference_price,slippage_bps,impact_bps,fee,realized_pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (o['id'],name,day,o['symbol'],o['side'],f['quantity'],f['price'],f['reference_price'],f['slippage_bps'],f['impact_bps'],f['fee'],realized))
            verb='Bought' if o['side']=='BUY' else 'Sold'
            self._log(db,name,day,o['symbol'],'FILL',None,None,
                f"{verb} {f['quantity']} {o['symbol']} @ {f['price']:.2f} (typical {f['reference_price']:.2f}, {f['slippage_bps']:g} bps slippage + {f['impact_bps']:g} bps impact, charges Rs {f['fee']:.2f})"
                +(f"; partial: {o['qty']-f['quantity']} capped by volume participation" if f['partial'] else '')
                +(f"; realized P&L Rs {realized:,.2f}" if realized is not None else ''),dict(order_id=o['id'],kind=o['kind']))
            fills+=1;turnover+=f['value'];fees+=f['fee']
        return fills,turnover,fees

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _bar(snapshot,symbol):
        bars=snapshot['bars'].get(symbol)
        return bars[-1] if bars and bars[-1]['day']==snapshot['day'] else None

    @staticmethod
    def _prices(snapshot,book):
        """Latest completed close per holding (last known close if the stock did not print today)."""
        out={}
        for s,p in book.items():
            bars=snapshot['bars'].get(s)
            out[s]=bars[-1]['close'] if bars else p['entry_price']
        return out

    @staticmethod
    def _book(db,name):
        return {r['symbol']:dict(qty=r['qty'],cost=r['cost'],entry_price=r['entry_price'],entry_day=r['entry_day'],high_close=r['high_close'],
            meta=json.loads(r['meta']),score=json.loads(r['score']),score_json=r['score']) for r in db.execute('SELECT * FROM qb_positions WHERE basket=?',(name,))}

    @staticmethod
    def _cash(db,name):return db.execute('SELECT cash FROM qb_baskets WHERE name=?',(name,)).fetchone()[0]

    @staticmethod
    def _log(db,name,day,symbol,action,before,after,reason,details=None):
        if action=='NOTE':
            last=db.execute("SELECT reason FROM qb_rebalance_log WHERE basket=? AND action='NOTE' ORDER BY id DESC LIMIT 1",(name,)).fetchone()
            if last and last[0]==reason:return      # a standing condition is logged once, not every session
        db.execute('INSERT INTO qb_rebalance_log(basket,day,symbol,action,weight_before,weight_after,reason,details) VALUES(?,?,?,?,?,?,?,?)',
            (name,day,symbol,action,None if before is None else round(before,6),None if after is None else round(after,6),reason,j(details or {})))

    # ------------------------------------------------------------ telemetry

    def payload(self,name,log_limit=100):
        """Standardized JSON for one basket: holdings, performance vs benchmark, rebalance audit log."""
        with connect(self.path) as db:
            b=db.execute('SELECT * FROM qb_baskets WHERE name=?',(name,)).fetchone()
            if not b:raise ValueError(f'No basket named {name!r}.')
            navs=db.execute('SELECT * FROM qb_nav WHERE basket=? ORDER BY day',(name,)).fetchall()
            as_of=navs[-1]['day'] if navs else None
            weights={r['symbol']:r for r in db.execute('SELECT * FROM qb_weights WHERE basket=? AND day=?',(name,as_of))}
            holdings=[]
            for p in db.execute('SELECT * FROM qb_positions WHERE basket=? ORDER BY symbol',(name,)):
                w=weights.get(p['symbol']);px=w['close'] if w else p['entry_price'];value=p['qty']*px
                holdings.append(dict(ticker=p['symbol'],shares=p['qty'],entry_day=p['entry_day'],entry_price=round(p['entry_price'],2),
                    current_price=round(px,2),weight_pct=round(100*w['weight'],2) if w else None,
                    target_weight_pct=round(100*w['target_weight'],2) if w and w['target_weight'] is not None else None,
                    cost_basis=round(p['cost'],2),market_value=round(value,2),unrealized_pnl=round(value-p['cost'],2),
                    unrealized_pnl_pct=round(100*(value/p['cost']-1),2),score=json.loads(p['score'])))
            nav=[r['nav'] for r in navs];bench=[r['benchmark'] for r in navs]
            aligned=[(n,x) for n,x in zip(nav,bench) if x];perf=performance(nav,None,self.risk_free)
            if aligned:
                bperf=performance([x for _,x in aligned],None,self.risk_free)
                perf['benchmark_return_pct']=bperf['cumulative_return_pct']
                perf['excess_return_pct']=round(perf['cumulative_return_pct']-bperf['cumulative_return_pct'],3) if perf['cumulative_return_pct'] is not None and bperf['cumulative_return_pct'] is not None else None
                perf['benchmark_max_drawdown_pct']=bperf['max_drawdown_pct']
            realized=db.execute('SELECT COALESCE(SUM(realized_pnl),0),COALESCE(SUM(fee),0),COUNT(*) FROM qb_fills WHERE basket=?',(name,)).fetchone()
            log=[dict(day=r['day'],action=r['action'],ticker=r['symbol'],weight_before_pct=None if r['weight_before'] is None else round(100*r['weight_before'],2),
                weight_after_pct=None if r['weight_after'] is None else round(100*r['weight_after'],2),reason=r['reason'])
                for r in db.execute('SELECT * FROM qb_rebalance_log WHERE basket=? ORDER BY id DESC LIMIT ?',(name,log_limit))]
            pending=[dict(signal_day=r['signal_day'],ticker=r['symbol'],side=r['side'],shares=r['qty'],kind=r['kind'],reason=r['reason'])
                for r in db.execute("SELECT * FROM qb_orders WHERE basket=? AND status='pending' ORDER BY id",(name,))]
        return dict(basket=name,strategy=b['strategy'],config=config(b['strategy'],json.loads(b['config'])),as_of=as_of,
            capital=b['capital'],nav=nav[-1] if nav else b['capital'],cash=round(b['cash'],2),regime=json.loads(b['state']).get('regime'),
            retired=bool(json.loads(b['state']).get('retired')),retired_day=json.loads(b['state']).get('retired_day'),
            last_rebalance=b['last_rebalance'],holdings=holdings,
            performance=dict(**perf,realized_pnl=round(realized[0],2),total_charges=round(realized[1],2),fills=realized[2],risk_free_rate=self.risk_free),
            pending_orders=pending,rebalance_log=log,paper_only=True)
