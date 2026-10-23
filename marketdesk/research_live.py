"""Live signals for the research paper baskets: the backtest's own rule code (scripts/research_baskets.py and
scripts/research_fundamentals.py) run on the app's completed daily candles, so tested and traded rules cannot drift.

Each research basket maps to one backtest candidate. For the latest completed session it yields the buy order (top of
the candidate's score), the keep order read at monthly reviews, the holdings its exit rule sells next session, and its
stop. Inputs are completed sessions only; a missing Nifty 500 close leaves the market filters off (no buys).
Fundamentals come from data/backtests/fundamentals-history.json (point-in-time NSE results). Paper only."""
import importlib.util,sys
import numpy as np,pandas as pd
from .storage import ROOT,connect
from .working_universe import symbol_filter

# basket -> backtest candidate key, and which script defines it.
RESEARCH={'vol_momentum':('price','volmom'),'residual_momentum':('price','resmom'),'smooth_momentum':('price','fip'),
    'volume_surge':('price','volume'),'pullback':('price','pullback'),'low_max':('price','lowmax'),'reversal':('price','strev'),
    'minervini':('price','minervini'),'vcp':('price','vcp'),'conservative':('price','conservative'),'turtle':('price','turtle'),'connors':('price','connors'),
    'dual_momentum':('price','dual_mom'),'golden_cross':('price','golden_cross'),'macd':('price','macd'),'bollinger_rsi':('price','bollinger_rsi'),
    'quality_value':('fund','quality_value'),'magic_formula':('fund','magic_proxy'),'net_buyback':('fund','net_buyback'),
    'dividend_quality':('fund','dividend_quality'),'value_momentum':('fund','value_momentum')}

def script(name):
    if name in sys.modules:return sys.modules[name]
    if str(ROOT/'scripts') not in sys.path:sys.path.insert(0,str(ROOT/'scripts'))   # research_fundamentals imports research_baskets by name
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py');module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    # A failed load must not stay cached half-built, or every later call fails with a misleading AttributeError.
    try:spec.loader.exec_module(module)
    except BaseException:sys.modules.pop(name,None);raise
    return module

def panel(path,today):
    """Completed daily candles as the research panel: {'o','h','l','c','v'} frames (sessions x symbols) plus index, sectors, members."""
    with connect(path) as db:
        rows=db.execute('SELECT symbol,substr(ts,1,10),open,high,low,close,volume FROM candles WHERE '+symbol_filter(db,include_holdings=True)+' AND substr(ts,1,10)<?',(today,)).fetchall()
        bench=dict(db.execute('SELECT day,close FROM claude_benchmark')) if db.execute("SELECT 1 FROM sqlite_master WHERE name='claude_benchmark'").fetchone() else {}
        sectors=dict(db.execute('SELECT symbol,sector FROM instruments'));members=[r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))]
    df=pd.DataFrame([tuple(r) for r in rows],columns=['s','d','o','h','l','c','v'])
    P={k:df.pivot_table(index='d',columns='s',values=k,aggfunc='last').sort_index() for k in 'ohlcv'}
    cal=P['c'].index;P={k:v.reindex(index=cal,columns=P['c'].columns) for k,v in P.items()}
    P['index']=pd.Series(bench,dtype=float).reindex(cal);P['sector']=sectors;P['members']=members
    return P

def signals(path,today):
    """{basket: dict(as_of, order, keep_order, keep, monthly, max_hold, exit_now, stop, stop_frame)} for the latest completed session."""
    rb,rf=script('research_baskets'),script('research_fundamentals')
    P=panel(path,today);I=rb.indicators(P);E=I['tradable']&I['trend']
    specs=rb.candidates(P,I,E)
    if rf.SRC.exists():specs.update(rf.candidates(P,I,rf.frames(P),E))
    day=P['c'].index[-1];out={}
    def ranked(frame):
        row=frame.iloc[-1].dropna();return list(row.sort_values(ascending=False,kind='stable').index)
    for name,(_,key) in RESEARCH.items():
        spec=specs.get(key)
        if not spec:continue
        stop=spec.get('stop',.20);frame=stop if isinstance(stop,pd.DataFrame) else None
        when=spec.get('exit_when')
        out[name]=dict(as_of=day,order=ranked(spec['score']),keep_order=ranked(spec.get('exit_score',spec['score'])),keep=spec.get('keep',30),
            monthly=spec.get('monthly',True),max_hold=spec.get('max_hold'),n=10,
            exit_now=set(when.columns[when.iloc[-1].fillna(False).astype(bool).values]) if when is not None else set(),
            stop=None if frame is not None or stop>=1 else float(stop),
            stop_frame={d:{s:float(v) for s,v in row.dropna().items()} for d,row in frame.tail(400).iterrows()} if frame is not None else None)
    return out

def entry_stop(signal,symbol,entry_session):
    """Stop fraction for a position: fixed, or the per-position value read at the last completed session before entry."""
    if signal.get('stop_frame') is None:return signal.get('stop')
    days=[d for d in signal['stop_frame'] if d<entry_session]
    return signal['stop_frame'][max(days)].get(symbol) if days else None
