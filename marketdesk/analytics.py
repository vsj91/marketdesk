from .working_universe import symbol_filter
import json
from datetime import datetime, timezone
from .storage import connect, get_state, number
from .history_rules import is_financial
from .valuation import shares_crore,metrics
_SHARES={}

def technical(rows):
    closes=[r['close'] for r in rows];volumes=[r['volume'] for r in rows]
    if not closes:return {'count':0}
    def ret(n):return (closes[-1]/closes[-1-n]-1)*100 if len(closes)>n else None
    def avg(n):return sum(closes[-n:])/n if len(closes)>=n else None
    dma50,dma200=avg(50),avg(200)
    prior=sum(volumes[-40:-10])/30 if len(volumes)>=40 else None
    vr=(sum(volumes[-10:])/10)/prior if prior and prior>0 else None
    score=sum([20 if dma50 and closes[-1]>dma50 else 0,20 if dma200 and closes[-1]>dma200 else 0,20 if dma50 and dma200 and dma50>dma200 else 0,20 if (ret(21) or 0)>0 else 0,20 if vr and vr>=1.5 else 0])
    return {'count':len(rows),'close':closes[-1],'as_of':rows[-1]['ts'],'return_1d':ret(1),'return_1m':ret(21),'return_1y':ret(252),'dma50':dma50,'dma200':dma200,'volume_ratio':vr,'trend_score':score,'history_status':'200+ observations' if len(rows)>=200 else 'Short history'}

def listing(db_path):
    with connect(db_path) as db:
        instruments=[dict(r) for r in db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db)+' ORDER BY symbol')]
        fundamentals={r['symbol']:(json.loads(r['details']),r['observed_at']) for r in db.execute('SELECT * FROM fundamentals WHERE '+symbol_filter(db))}
        quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes WHERE '+symbol_filter(db))}
        quality={r['symbol']:json.loads(r['report']) for r in db.execute('SELECT * FROM quality WHERE '+symbol_filter(db))}
        watched={r[0] for r in db.execute('SELECT symbol FROM watchlist WHERE '+symbol_filter(db))}
        bad={r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE "+symbol_filter(db)+" AND kind='candle'")}
        # One indexed ordered read avoids one database query per symbol.
        candles={}
        for r in db.execute('SELECT symbol,ts,close,volume FROM candles WHERE '+symbol_filter(db)+' ORDER BY symbol,ts'):candles.setdefault(r['symbol'],[]).append(r)
        stream=get_state(db,'stream',{})
        # Shares are re-derived only when a symbol's income statement payload changes.
        for r in db.execute("SELECT symbol,observed_at FROM payloads WHERE endpoint='income_statement' AND "+symbol_filter(db)).fetchall():
            latest=fundamentals.get(r['symbol'],({},None))[0].get('latest') or {}
            key=(r['observed_at'],latest.get('period'),latest.get('statement_type'),latest.get('units_in'))
            if _SHARES.get(r['symbol'],(None,))[0]!=key:
                raw=db.execute("SELECT payload FROM payloads WHERE symbol=? AND endpoint='income_statement'",(r['symbol'],)).fetchone()[0]
                _SHARES[r['symbol']]=(key,shares_crore(raw,latest))
    result=[];clock=datetime.now(timezone.utc).timestamp()*1000
    for item in instruments:
        sym=item['symbol'];t=technical(candles.get(sym,[]));f,observed=fundamentals.get(sym,({},None));q=quotes.get(sym)
        age=(clock-q['trade_time'])/1000 if q else None
        market=stream.get('market_status',{}).get('NSE_EQ')
        received_age=(datetime.now(timezone.utc)-datetime.fromisoformat(q['received_at'])).total_seconds() if q else None
        fresh=bool(q and age is not None and 0<=age<=30 and received_age is not None and 0<=received_age<=30 and stream.get('status')=='connected' and market=='NORMAL_OPEN')
        latest=f.get('latest') or {};summary=f.get('summary') or {}
        if sym in bad:
            # Removing invalid source observations must not create deceptively valid lookbacks.
            for field in ('return_1d','return_1m','return_1y','dma50','dma200','volume_ratio','trend_score'):t[field]=None
            t['history_status']='Source validation issues'
        item.update(quality=quality.get(sym,{}),technical=t,quote=q,quote_status='LIVE' if fresh else 'LAST TRADE' if q else 'EOD',quote_age_seconds=age,
            fundamentals_status=f.get('status','MISSING'),fundamentals_period=latest.get('period'),fundamentals_observed=observed,
            missing_fields=f.get('missing_fields',list(__import__('marketdesk.provider',fromlist=['FUNDAMENTAL_REQUIRED_FIELDS']).FUNDAMENTAL_REQUIRED_FIELDS)),
            summary=summary,watched=sym in watched,candle_issues=sym in bad,
            valuation=metrics(f,q['price'] if q else t.get('close'),_SHARES.get(sym,(None,None))[1],quality.get(sym,{}).get('financial_sector',is_financial(item['sector']))))
        result.append(item)
    return result
