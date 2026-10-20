"""Investor display ratios from the selected latest annual year. Read-only: never an entry or exit input.
Missing stays None (shown as —); a loss or a non-positive denominator is 'n.m.'."""
import json
from .storage import number
NM='n.m.'

def shares_crore(payload,latest):
    """Shares in crore = profit after tax / basic EPS, from one Upstox income statement whose
    period, basis and units match the selected latest year. Upstox carries no share count."""
    try:data=json.loads(payload)['data']
    except (TypeError,ValueError,KeyError):return None
    if not isinstance(data,dict) or data.get('type')!=latest.get('statement_type') or data.get('units_in')!='crore' or latest.get('units_in')!='crore':return None
    rows={r.get('particular'):r.get('history') or [] for r in data.get('full_statement') or [] if isinstance(r,dict)}
    def value(name):return next((number(h.get('value')) for h in rows.get(name,[]) if isinstance(h,dict) and h.get('period')==latest.get('period')),None)
    pat,eps=value('Profit After Tax'),value('EPS - Basic')
    if pat is None or eps is None or eps==0 or pat/eps<=0:return None
    return pat/eps

def _ratio(top,bottom,scale=1):
    if top is None or bottom is None:return None
    return NM if bottom<=0 or top<0 else top/bottom*scale

def metrics(details,price,shares,financial):
    latest,summary=details.get('latest') or {},details.get('summary') or {};period=latest.get('period')
    same=bool(period) and summary.get('statement_type')==latest.get('statement_type') and summary.get('units')==latest.get('units_in')
    def annual(key):return number(summary.get(key)) if same and summary.get(key+'_period')==period else None
    profit,revenue=annual('net_profit'),annual('revenue')
    equity,pbt,finance,borrow=(number(latest.get(k)) for k in ('equity','profit_before_tax','finance_costs','total_borrowings'))
    price=number(price)
    cap=price*shares if price is not None and price>0 and shares is not None else None
    out={'period':period,'price':price,'market_cap':cap,'pe':_ratio(cap,profit),'pb':_ratio(cap,equity),
         'roe':_ratio(profit,equity,100),'net_margin':_ratio(profit,revenue,100)}
    if not financial:
        ebit=None if pbt is None or finance is None else pbt+finance
        capital=None if equity is None or borrow is None else equity+borrow
        out['roce']=_ratio(ebit,capital,100)
    return out
