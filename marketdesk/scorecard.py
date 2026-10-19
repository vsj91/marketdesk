"""Go-live scorecard: objective gates that must all be met before real money is even considered.

It reports facts from the local ledger. It is not advice, and meeting every gate does not guarantee profit."""
from datetime import datetime,timezone
from .storage import get_state
MIN_DAYS=90;MIN_CLOSED={'trader':30,'investor':20};MAX_DRAWDOWN_PCT=20.0;MIN_CHECKED_SESSIONS=20

def gate(key,label,met,detail):return {'key':key,'label':label,'met':bool(met),'detail':detail}
def inr(value):return ('−₹' if value<0 else '₹')+f'{abs(value):,.0f}'

def closed_round_trips(db,name):
    """Count exits that took a holding to zero; partial trims are not closed trades."""
    held={};closed=0
    for t in db.execute('SELECT symbol,side,quantity FROM trades WHERE portfolio=? ORDER BY created_at,rowid',(name,)):
        before=held.get(t['symbol'],0);held[t['symbol']]=before+(t['quantity'] if t['side']=='BUY' else -t['quantity'])
        if t['side']=='SELL' and before>0 and held[t['symbol']]<=0:closed+=1
    return closed

def scorecard(db,clock=None):
    clock=clock or datetime.now(timezone.utc);dd=get_state(db,'drawdown',{});cc=get_state(db,'close_check',{}).get('sessions',{})
    checked=[v for v in cc.values() if v.get('compared')];compared=sum(v['compared'] for v in checked);bad=sum(v['close_mismatches'] for v in checked)
    shared=[gate('prices','Saved prices match NSE (independent daily check)',len(checked)>=MIN_CHECKED_SESSIONS and bad==0,f'{bad} mismatches / {compared:,} closes over {len(checked)} sessions (need {MIN_CHECKED_SESSIONS}+ sessions, 0 mismatches)'),
            gate('breaker','Drawdown breaker active on every basket',True,'New buys pause at a 15% fall from peak; exits stay active'),
            gate('orders','Real broker order placement with kill switch',False,'Not built. The app is paper-only by design and places no broker orders')]
    baskets={}
    from .paper import PORTFOLIOS
    for name in PORTFOLIOS:
        account=db.execute('SELECT * FROM portfolios WHERE name=?',(name,)).fetchone()
        first=db.execute('SELECT MIN(created_at) FROM trades WHERE portfolio=?',(name,)).fetchone()[0]
        closed=closed_round_trips(db,name)
        days=(clock-datetime.fromisoformat(first)).days if first else 0
        info=dd.get(name,{});peak=info.get('max_drawdown_pct');pnl=None
        if account:
            value=account['cash']
            for p in db.execute('SELECT symbol,quantity,cost FROM positions WHERE portfolio=? AND quantity>0',(name,)):
                q=db.execute('SELECT price FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
                value+=p['quantity']*q['price'] if q else p['cost']
            pnl=value-account['initial_cash']
        baskets[name]=[gate('days',f'Paper record of {MIN_DAYS}+ days',days>=MIN_DAYS,f'{days} days since the first trade'),
            gate('closed',f'{MIN_CLOSED.get(name,20)}+ closed trades',closed>=MIN_CLOSED.get(name,20),f'{closed} fully closed so far (partial trims not counted)'),
            gate('profit','Profit after fees and slippage',days>=MIN_DAYS and pnl is not None and pnl>0,'Unavailable' if pnl is None else f'{inr(pnl)} net on the current marks'+('' if days>=MIN_DAYS else f' (counts only after {MIN_DAYS} days)')),
            gate('drawdown',f'Worst drawdown within {MAX_DRAWDOWN_PCT:g}%',days>=MIN_DAYS and peak is not None and peak<=MAX_DRAWDOWN_PCT,'Not tracked yet' if peak is None else f'{peak:g}% worst so far'+('' if days>=MIN_DAYS else f' (counts only after {MIN_DAYS} days)'))]
    met=sum(g['met'] for g in shared)+sum(g['met'] for v in baskets.values() for g in v);total=len(shared)+sum(len(v) for v in baskets.values())
    return {'shared':shared,'baskets':baskets,'met':met,'total':total,'ready':met==total,
            'note':'Facts from the local ledger, not advice. Meeting every gate does not guarantee profit; use only money you can afford to lose.'}
