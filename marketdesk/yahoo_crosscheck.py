"""Fill a missing annual field from Yahoo only when Yahoo provably reports the same statement.

Yahoo does not label consolidated/standalone, so its rows are never trusted alone. For the same annual
period, at least two anchor figures (profit before tax, equity, cash) must match the verified row within
0.1% (rounding level) and none may contradict it. Only lease and borrowing totals are filled, and only from explicit numeric
components; absent rows are never read as zero. Everything else stays blocked."""
from .provider import FUNDAMENTAL_REQUIRED_FIELDS

TOLERANCE=.001
ANCHORS=('profit_before_tax','equity','cash_and_cash_equivalents')
CRORE=1e7

def _num(value):
    return float(value) if isinstance(value,(int,float)) and not isinstance(value,bool) and value==value and abs(value)!=float('inf') else None

def _anchors(latest,row):
    matched,contradicted=[],[]
    for field in ANCHORS:
        verified,yahoo=_num(latest.get(field)),_num(row.get(field))
        if verified is None or yahoo is None or verified==0:continue
        (matched if abs(yahoo/CRORE-verified)<=abs(verified)*TOLERANCE else contradicted).append(field)
    return matched,contradicted

def apply_yahoo_crosscheck(details,yahoo):
    latest=details.get('latest') or {}
    if yahoo.get('status')!='available' or latest.get('units_in')!='crore' or not latest.get('period') or not latest.get('statement_type'):return details
    row=next((r for r in yahoo.get('annual',[]) if r.get('period')==latest['period'] and r.get('currency')=='INR'),None)
    if not row:return details
    matched,contradicted=_anchors(latest,row)
    if len(matched)<2 or contradicted:return details
    fills={}
    if _num(latest.get('total_lease_liabilities')) is None:
        cur,non=_num(row.get('current_lease_liabilities')),_num(row.get('non_current_lease_liabilities'))
        if cur is not None and non is not None and cur>=0 and non>=0 and cur+non>0:fills['total_lease_liabilities']=(cur+non)/CRORE
    if _num(latest.get('total_borrowings')) is None:
        cur,non=_num(row.get('current_borrowings')),_num(row.get('non_current_borrowings'))
        if cur is not None and non is not None and cur>=0 and non>=0:fills['total_borrowings']=(cur+non)/CRORE
    if _num(latest.get('cash_and_cash_equivalents')) is None and _num(row.get('cash_and_cash_equivalents')) is not None and row['cash_and_cash_equivalents']>=0:
        fills['cash_and_cash_equivalents']=row['cash_and_cash_equivalents']/CRORE
    if not fills:return details
    latest=dict(latest);sources=dict(latest.get('sources') or {})
    for field,value in fills.items():
        latest[field]=value
        sources[field]={'source':'yahoo_finance_cross_checked','source_url':f"https://finance.yahoo.com/quote/{yahoo.get('symbol','')}/financials/",
                        'period_end':row.get('period_end'),'anchors_matched':matched,'tolerance':TOLERANCE,'observed_at':yahoo.get('observed_at')}
    latest['sources']=sources
    latest['missing_fields']=[k for k in FUNDAMENTAL_REQUIRED_FIELDS if latest.get(k) is None]
    details=dict(details);details['latest']=latest
    details['missing_fields']=latest['missing_fields']+([] if details.get('sector') else ['sector'])
    details['status']='COMPLETE' if not details['missing_fields'] else 'PARTIAL'
    details['yahoo_crosscheck']={'observed_at':yahoo.get('observed_at'),'period':latest['period'],'anchors_matched':matched,'filled':sorted(fills)}
    return details
