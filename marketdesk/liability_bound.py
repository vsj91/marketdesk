"""Conservative upper bound for missing borrowing or lease lines. Nothing is filled in.

Borrowings and lease liabilities are distinct parts of total liabilities, so
borrowings + leases <= current + non-current liabilities. If the Investor net-debt
test passes even when every liability is treated as debt, the missing line
cannot change that result. The record stays PARTIAL; only the decision is bounded."""
import json
from .storage import number
BOUNDED_FIELDS=frozenset({'total_borrowings','total_lease_liabilities'})
MAX_NET_DEBT_TO_EQUITY=.5

def liability_bound(details,payload):
    """Total liabilities for the verified annual period, or None unless every identity agrees."""
    latest=(details or {}).get('latest') or {};data=(payload or {}).get('data') or {}
    period=latest.get('period');equity=number(latest.get('equity'))
    if not period or equity is None or equity<=0:return None
    if data.get('type')!=latest.get('statement_type') or data.get('units_in')!=latest.get('units_in'):return None
    rows={r.get('particular'):{h.get('period'):number(h.get('value')) for h in r.get('history') or []} for r in data.get('full_statement') or [] if isinstance(r,dict)}
    current,non_current,total,reported_equity=(rows.get(k,{}).get(period) for k in ('Current Liabilities','Non-Current Liabilities','Total Equity & Liabilities','Equity Capital'))
    if None in (current,non_current,total,reported_equity) or min(current,non_current)<0 or total<=0:return None
    liabilities=current+non_current
    # The statement must balance and agree with the verified equity before it can bound anything.
    if abs(total-reported_equity-liabilities)>total*.005 or abs(reported_equity/equity-1)>.01:return None
    return {'total_liabilities':round(liabilities,4),'period':period,'statement_type':data['type'],'units':data['units_in'],
            'equity_used':min(equity,reported_equity),'source':'Upstox balance sheet: current + non-current liabilities'}

def bounded_debt_pass(details):
    """True when only borrowing/lease lines are missing and the worst case still passes the net-debt test."""
    f=details or {};latest=f.get('latest') or {};bound=f.get('liability_bound')
    missing=set(f.get('missing_fields') or [])
    if f.get('status')=='COMPLETE' or not bound or not missing or not missing<=BOUNDED_FIELDS:return False
    if bound.get('period')!=latest.get('period') or bound.get('statement_type')!=latest.get('statement_type') or bound.get('units')!=latest.get('units_in'):return False
    cash=number(latest.get('cash_and_cash_equivalents'));equity=number(bound.get('equity_used'));liabilities=number(bound.get('total_liabilities'))
    if None in (cash,equity,liabilities) or cash<0 or equity<=0:return False
    return (liabilities-cash)/equity<=MAX_NET_DEBT_TO_EQUITY

def attach(db,details_by_symbol):
    """Add liability_bound to in-memory fundamentals that miss borrowing/lease lines."""
    wanted=[s for s,d in details_by_symbol.items() if d and d.get('status')!='COMPLETE' and set(d.get('missing_fields') or [])&BOUNDED_FIELDS]
    for symbol in wanted:
        row=db.execute("SELECT payload FROM payloads WHERE symbol=? AND endpoint='balance_sheet'",(symbol,)).fetchone()
        try:bound=liability_bound(details_by_symbol[symbol],json.loads(row[0])) if row else None
        except (TypeError,ValueError):bound=None
        if bound:details_by_symbol[symbol]['liability_bound']=bound
    return details_by_symbol
