"""Complete explicitly requested paper exits without relaxing fill safeguards."""
import threading,uuid
from datetime import datetime,timezone
from .storage import connect,get_state,set_state,now
from .buy_universe import allowed_symbols
from .paper import trade
from .quote_health import refresh_held_quotes
KEY='nifty500_requested_exits'


def exit_guard(db,order,quote,account,position):
    pending=get_state(db,KEY,[])
    if order['side']!='SELL' or not any(r['portfolio']==order['portfolio'] and r['symbol']==order['symbol'] for r in pending):
        raise ValueError('No user-requested exit for this holding')
    allowed=allowed_symbols(db,order['portfolio'],datetime.now(timezone.utc))
    if allowed is None or order['symbol'] in allowed:raise ValueError('Holding is in the current buy universe')
    return 'User-requested exit: restrict both paper baskets to Nifty 500'


def process(path):
    with connect(path) as db:pending=get_state(db,KEY,[])
    if not pending:return
    for portfolio in sorted({r['portfolio'] for r in pending}):refresh_held_quotes(path,portfolio)
    # Retry only the specifically authorized holdings; never add future exclusions.
    for request in pending:
        p,s=request['portfolio'],request['symbol']
        with connect(path) as db:
            position=db.execute('SELECT quantity FROM positions WHERE portfolio=? AND symbol=?',(p,s)).fetchone()
            remaining=position[0] if position else 0
        outcome={'portfolio':p,'symbol':s,'remaining':remaining,'checked_at':now()}
        if remaining:
            try:
                filled=trade(path,dict(portfolio=p,symbol=s,side='SELL',quantity=min(remaining,request['max_quantity']),
                                      request_id='nifty500-exit-'+str(uuid.uuid4())),auto_guard=exit_guard)
                outcome.update(status='partial' if filled['quantity']<remaining else 'closed',trade_id=filled['id'])
            except ValueError as error:outcome.update(status='pending',reason=str(error))
            except Exception as error:outcome.update(status='pending',reason=type(error).__name__+'; fill safeguards retained')
        else:outcome['status']='closed'
        with connect(path) as db:
            row=db.execute('SELECT quantity FROM positions WHERE portfolio=? AND symbol=?',(p,s)).fetchone()
            outcome['remaining']=row[0] if row else 0
            current=get_state(db,KEY,[])
            if not outcome['remaining']:set_state(db,KEY,[r for r in current if (r['portfolio'],r['symbol'])!=(p,s)])
            reports=get_state(db,KEY+'_status',{});reports[p+':'+s]=outcome;set_state(db,KEY+'_status',reports)


class RequestedExits:
    def __init__(self,path):self.path=path;self.stop_event=threading.Event()
    def start(self):threading.Thread(target=self.run,daemon=True).start()
    def run(self):
        while not self.stop_event.is_set():
            try:process(self.path)
            except Exception:pass # An unavailable data service must not stop the normal engine.
            self.stop_event.wait(30)
    def stop(self):self.stop_event.set()
