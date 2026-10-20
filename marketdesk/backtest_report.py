"""Backtest results beside live paper results for every basket (read-only).

Backtests come from data/backtests/basket-backtests.json, written by scripts/research_multi_year.py summary.
Paper results come from the live ledger on every request: equity at current marks, return on starting cash,
and the Nifty 500 move over the same days. "Backtest pace" compounds the backtest's annual return over the
same number of days, so a short paper record can be read against it without annualizing a few weeks."""
import json
from datetime import date,datetime,timedelta,timezone
from .storage import ROOT,connect,get_state
from .paper import portfolios,PORTFOLIOS
from .basket_rules import RETIRED

SUMMARY=ROOT/'data/backtests/basket-backtests.json'
IST=timezone(timedelta(hours=5,minutes=30))

def _pct(a,b):return round(100*(b/a-1),2) if a and b else None

def report(path,clock=None):
    clock=clock or datetime.now(timezone.utc)
    saved=json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    tests=saved.get('baskets',{});accounts={a['name']:a for a in portfolios(path,clock)}
    with connect(path) as db:
        cfg=get_state(db,'auto_config',{})
        has=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_benchmark'").fetchone()
        bench=db.execute('SELECT day,close FROM claude_benchmark ORDER BY day').fetchall() if has else []
        rows=[]
        for name in PORTFOLIOS:
            a=accounts.get(name)
            if not a:continue
            first,count=db.execute('SELECT MIN(created_at),COUNT(*) FROM trades WHERE portfolio=?',(name,)).fetchone()
            start=datetime.fromisoformat(first).astimezone(IST).date() if first else None
            days=(clock.astimezone(IST).date()-start).days if start else 0
            base=next((c for d,c in reversed(bench) if start and d<start.isoformat()),None)
            test=tests.get(name) or {}
            cagr=(test.get('full') or {}).get('cagr')
            paper=dict(since=start.isoformat() if start else None,days=days,trades=count,equity=a.get('equity'),initial_cash=a['initial_cash'],
                return_pct=round(100*a['pnl']/a['initial_cash'],2) if a.get('pnl') is not None and start else None,
                nifty500_pct=_pct(base,bench[-1][1]) if base and bench else None,
                backtest_pace_pct=round(100*((1+cagr/100)**(days/365.25)-1),2) if cagr is not None and days else None)
            c=cfg.get(name) or {}
            state='retired' if name in RETIRED else 'off' if not c.get('enabled') else 'exits_only' if c.get('exits_only') else 'on'
            rows.append(dict(name=name,state=state,retired_reason=RETIRED.get(name),backtest=test or None,paper=paper))
    return dict(generated=saved.get('generated'),method=saved.get('method',[]),benchmarks=saved.get('benchmarks',{}),
        baskets=rows,candidates=saved.get('candidates',[]),paper_only=True,
        missing=None if saved else 'No backtest summary yet. Run .venv/bin/python scripts/research_multi_year.py summary.')
