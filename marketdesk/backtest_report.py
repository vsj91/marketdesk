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

# A paper record is judged after this many closed trades; fewer is mostly luck. The days needed follow from
# the basket's own backtest trade rate, never less than a month.
JUDGE_TRADES=30;MIN_DAYS=30
# Baskets whose rules changed after they started trading; their paper record restarts on that day.
RULES_SINCE={'smart_beta':'2026-10-21','risk_parity':'2026-10-21','regime':'2026-10-21'}   # 25% disaster stop added

def paper_length(test):
    """(trades per year, calendar days to JUDGE_TRADES closed trades) at the backtest pace; (None, None) when untested."""
    window,trades=test.get('window'),test.get('trades')
    if not window or not trades:return None,None
    years=(date.fromisoformat(window[1])-date.fromisoformat(window[0])).days/365.25
    rate=trades/years if years>0 else None
    return (round(rate,1),max(MIN_DAYS,round(JUDGE_TRADES/rate*365.25))) if rate else (None,None)

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
            first,count,closed=db.execute("SELECT MIN(created_at),COUNT(*),SUM(side='SELL') FROM trades WHERE portfolio=?",(name,)).fetchone()
            start=datetime.fromisoformat(first).astimezone(IST).date() if first else None
            if start and name in RULES_SINCE:start=max(start,date.fromisoformat(RULES_SINCE[name]))
            days=(clock.astimezone(IST).date()-start).days if start else 0
            base=next((c for d,c in reversed(bench) if start and d<start.isoformat()),None)
            test=tests.get(name) or {}
            cagr=(test.get('full') or {}).get('cagr')
            rate,needed=paper_length(test)
            paper=dict(since=start.isoformat() if start else None,days=days,trades=count,closed_trades=closed or 0,equity=a.get('equity'),initial_cash=a['initial_cash'],
                judge_trades=JUDGE_TRADES,backtest_trades_per_year=rate,days_needed=needed,
                judge_on=(start+timedelta(days=needed)).isoformat() if start and needed else None,
                return_pct=round(100*a['pnl']/a['initial_cash'],2) if a.get('pnl') is not None and start else None,
                nifty500_pct=_pct(base,bench[-1][1]) if base and bench else None,
                backtest_pace_pct=round(100*((1+cagr/100)**(days/365.25)-1),2) if cagr is not None and days else None)
            c=cfg.get(name) or {}
            state='retired' if name in RETIRED else 'off' if not c.get('enabled') else 'exits_only' if c.get('exits_only') else 'on'
            rows.append(dict(name=name,state=state,retired_reason=RETIRED.get(name),backtest=test or None,paper=paper))
    return dict(generated=saved.get('generated'),method=saved.get('method',[]),benchmarks=saved.get('benchmarks',{}),
        baskets=rows,candidates=saved.get('candidates',[]),paper_only=True,
        missing=None if saved else 'No backtest summary yet. Run .venv/bin/python scripts/research_multi_year.py summary.')
