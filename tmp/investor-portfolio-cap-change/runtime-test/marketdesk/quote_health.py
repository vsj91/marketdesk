"""Held-price diagnostics and read-only recovery of missed live quotes.

A new HTTP observation never makes an old exchange trade fresh.
"""
from datetime import datetime, timezone
import requests
from .feed import load_token
from .storage import connect, get_state, set_state, store_quote, number


def fresh(quote, clock):
    if not quote:return False
    try:
        return (0 <= clock.timestamp()*1000-quote['trade_time'] <= 30000 and
            0 <= (clock-datetime.fromisoformat(quote['received_at'])).total_seconds() <= 30)
    except (ValueError, TypeError, KeyError):return False


def stale_holdings(db, portfolio, clock):
    rows=db.execute('''SELECT p.symbol,i.instrument_key,q.price,q.trade_time,q.received_at
        FROM positions p LEFT JOIN instruments i ON i.symbol=p.symbol
        LEFT JOIN quotes q ON q.symbol=p.symbol
        WHERE p.portfolio=? AND p.quantity>0 ORDER BY p.symbol''',(portfolio,)).fetchall()
    clock=clock or datetime.now(timezone.utc)
    result=[]
    for row in rows:
        q=dict(row)
        if fresh(q,clock):continue
        if q['trade_time'] is None:reason='no quote'
        else:
            age=clock.timestamp()-q['trade_time']/1000
            if age<0:reason='future trade timestamp'
            elif age>30:reason=f'last trade {int(age)}s old'
            else:reason='feed observation missing, stale or future dated'
        result.append(dict(symbol=q['symbol'],instrument_key=q['instrument_key'],reason=reason))
    return result


def stale_message(db, portfolio, clock):
    stocks=stale_holdings(db,portfolio,clock)
    details=', '.join(f"{s['symbol']} ({s['reason']})" for s in stocks)
    return ('Held stock has no fresh price: '+(details or 'quote changed during check')+
        '; new buys wait. Exits continue for holdings with fresh quotes.')


def refresh_held_quotes(path, portfolio, clock=None):
    """One bounded batch per live scan; merge only verified recent trades.

    Do not overwrite a newer websocket quote, relax freshness, or change orders.
    Explicit clocks are for deterministic tests only.
    """
    with connect(path) as db:
        stream=get_state(db,'stream',{})
        if stream.get('status')!='connected' or stream.get('market_status',{}).get('NSE_EQ')!='NORMAL_OPEN':return
        stale=stale_holdings(db,portfolio,clock)
    if not stale:return
    keys={s['symbol']:s['instrument_key'] for s in stale if s['instrument_key']}
    checked=clock or datetime.now(timezone.utc)
    report=dict(checked_at=checked.isoformat(),requested=list(keys),updated=[],unresolved={},status='checked')
    accepted=[]
    try:
        token=load_token()
        if not token:raise ValueError('Data token unavailable')
        if not keys:raise ValueError('Holding instrument identity unavailable')
        response=requests.get('https://api.upstox.com/v3/market-quote/quotes',
            params={'instrument_key':','.join(keys.values())},
            headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(2,3))
        if response.status_code!=200:raise ValueError('Quote service HTTP '+str(response.status_code))
        payload=response.json()
        if not isinstance(payload,dict) or payload.get('status')!='success' or not isinstance(payload.get('data'),dict):
            raise ValueError('Quote service response unavailable')
        received=clock or datetime.now(timezone.utc)
        for symbol,key in keys.items():
            try:
                raw=payload['data'].get('NSE_EQ:'+symbol,{})
                if raw.get('symbol')!=symbol or raw.get('instrument_token')!=key:raise ValueError('Quote identity missing or mismatched')
                stamp=datetime.fromisoformat(raw['timestamp'])
                if stamp.tzinfo is None or not 0<=(received-stamp).total_seconds()<=5:raise ValueError('Provider snapshot is not fresh')
                ltt=number(raw.get('last_trade_time'));price=number(raw.get('last_price'));cp=number(raw.get('prev_close_price'))
                if ltt is None or ltt<=0 or not ltt.is_integer() or price is None or price<=0 or cp is None or cp<=0:
                    raise ValueError('Invalid quote values')
                if not 0<=received.timestamp()*1000-ltt<=30000:raise ValueError('Provider last trade is not within 30 seconds')
                accepted.append((symbol,dict(ltp=price,ltt=int(ltt),cp=cp)))
            except (ValueError,KeyError,TypeError,AttributeError) as error:
                report['unresolved'][symbol]=str(error) if isinstance(error,ValueError) else 'Malformed quote'
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            stream=get_state(db,'stream',{})
            committed=clock or datetime.now(timezone.utc)
            if stream.get('status')=='connected' and stream.get('market_status',{}).get('NSE_EQ')=='NORMAL_OPEN':
                for symbol,quote in accepted:
                    if not 0<=committed.timestamp()*1000-quote['ltt']<=30000:continue
                    identity=db.execute('SELECT instrument_key FROM instruments WHERE symbol=?',(symbol,)).fetchone()
                    if not identity or identity[0]!=keys[symbol]:continue
                    if store_quote(db,symbol,quote,received.isoformat(),source='upstox_rest_quote_v3'):
                        report['updated'].append(symbol)
    except requests.RequestException:
        report.update(status='unavailable',error='Quote request failed; freshness checks retained')
    except (ValueError,TypeError,KeyError):
        report.update(status='unavailable',error='Quote service unavailable or invalid; freshness checks retained')
    with connect(path) as db:
        report['remaining']=stale_holdings(db,portfolio,clock)
        set_state(db,'held_quote_refresh_'+portfolio,report)
    return report
