"""Portfolio drawdown breaker: stop new buys after a deep fall from the equity peak; exits stay active.

A halt lifts only after equity recovers to within 60% of the limit, so it cannot flap around the threshold."""
from .storage import get_state,set_state,now
DEFAULT_LIMIT_PCT=15.0
RESUME_FRACTION=.6

def limit_pct(config):
    value=(config or {}).get('max_drawdown_pct',DEFAULT_LIMIT_PCT)
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<value<100 else DEFAULT_LIMIT_PCT

def update(db,portfolio,equity,starting_capital,limit):
    """Record the equity peak and return (halted, state). Call inside the caller's write transaction."""
    state=get_state(db,'drawdown',{});old=state.get(portfolio,{})
    peak=max(float(old.get('peak',0)),float(starting_capital),float(equity));drawdown=(1-equity/peak)*100
    halted=bool(old.get('halted'))
    if not halted and drawdown>=limit:halted=True
    elif halted and drawdown<limit*RESUME_FRACTION:halted=False
    state[portfolio]=dict(peak=round(peak,2),halted=halted,drawdown_pct=round(drawdown,2),max_drawdown_pct=round(max(float(old.get('max_drawdown_pct',0)),drawdown),2),limit_pct=limit,updated_at=now())
    set_state(db,'drawdown',state)
    return halted,state[portfolio]

def gate(db,portfolio,equity,starting_capital,config):
    """(halted, info) for new buys: the drawdown breaker, or a manual exits-only switch that keeps stops and exits running."""
    halted,info=update(db,portfolio,equity,starting_capital,limit_pct(config)) if equity else (False,None)
    if (config or {}).get('exits_only'):
        return True,dict(info or {},exits_only=True,exits_only_reason=config.get('exits_only_reason') or 'set by the owner')
    return halted,info

def message(info):
    if info.get('exits_only'):
        return f"Exits only: {info['exits_only_reason']} New buys are off; stops and exits keep running."
    return (f"Drawdown limit reached: equity is {info['drawdown_pct']:.1f}% below its ₹{info['peak']:,.0f} peak (limit {info['limit_pct']:g}%). "
            f"New buys pause until the fall is under {info['limit_pct']*RESUME_FRACTION:.1f}%. Exits remain active.")
