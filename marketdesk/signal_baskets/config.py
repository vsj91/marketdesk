"""Validated settings changes for the signal baskets (parameters and starting capital). Paper only."""
from ..storage import connect,get_state,set_state,now,number
from .engine import validate_params,transition
from .names import SIGNAL_BASKETS,TITLES

def update(path,payload):
    """{'portfolio', 'params': {...}} merges validated parameters; {'capital': n} sets starting capital, allowed
    only before the basket's first trade so existing ledgers, P&L and history are never rewritten."""
    name=payload.get('portfolio')
    if name not in SIGNAL_BASKETS:raise ValueError('Choose one of the signal baskets.')
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        cfg=get_state(db,'auto_config',{});entry=cfg.setdefault(name,{})
        changes={}
        if 'params' in payload:
            clean=validate_params(name,payload['params'])
            entry['params']={**entry.get('params',{}),**clean};changes['params']=clean
        if 'capital' in payload:
            capital=number(payload['capital'])
            if capital is None or not 10_000<=capital<=10_000_000:raise ValueError('Starting capital must be between ₹10,000 and ₹1,00,00,000.')
            if db.execute('SELECT 1 FROM trades WHERE portfolio=? LIMIT 1',(name,)).fetchone() or db.execute('SELECT 1 FROM positions WHERE portfolio=? AND quantity>0',(name,)).fetchone():
                raise ValueError(f'{TITLES[name]} already has paper trades; its starting capital is fixed so past P&L stays comparable.')
            db.execute('UPDATE portfolios SET initial_cash=?,cash=? WHERE name=?',(capital,capital,name));changes['capital']=capital
        if not changes:raise ValueError('Nothing to change.')
        entry['updated_at']=now();set_state(db,'auto_config',cfg)
        transition(db,name,None,None,None,'CONFIG',f'Settings changed: {changes}',changes)
    return dict(saved=True,portfolio=name,changes=changes)
