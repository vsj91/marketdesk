"""Reconcile large raw-price moves against NSE without adjusting stored candles.

An ordinary market move requires matching exchange OHLCV on both adjacent
sessions, NSE's previous close, current ISIN identity, and an empty corporate
action window. Demergers/splits and unavailable evidence stay buy-blocking.
"""
import hashlib,json,math
from datetime import datetime,timedelta,timezone
from .storage import connect,get_state,set_state,now
from .working_universe import symbol_filter
from .buy_universe import SOURCE as IDENTITY,KEY as MEMBERSHIP_KEY,membership,IST

HISTORY='https://www.nseindia.com/api/historicalOR/generateSecurityWiseHistoricalData'
ACTIONS='https://www.nseindia.com/api/corporates-corporateActions'
FIELDS={'open':'CH_OPENING_PRICE','high':'CH_TRADE_HIGH_PRICE','low':'CH_TRADE_LOW_PRICE',
        'close':'CH_CLOSING_PRICE','volume':'CH_TOT_TRADED_QTY'}

def jumps(rows):
    return [(a,b) for a,b in zip(rows,rows[1:]) if a['close']>0 and
            (abs(b['open']/a['close']-1)>.25 or abs(b['close']/a['close']-1)>.25)]

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

def verify(proof,symbol,isin,pair,clock):
    """Revalidate every observation against current candles at audit/fill time."""
    stamp=datetime.fromisoformat(proof['observed_at'])
    if not 0<=(clock-stamp).total_seconds()<86400:raise ValueError('Exchange reconciliation expired')
    if proof.get('symbol')!=symbol or proof.get('isin')!=isin:raise ValueError('Reconciliation identity mismatch')
    if proof.get('sources')!={'history':HISTORY,'actions':ACTIONS,'identity':IDENTITY}:raise ValueError('Unrecognized reconciliation sources')
    data=proof['evidence']
    if digest(data)!=proof['sha256']:raise ValueError('Exchange evidence changed')
    if data['identity'].get('symbol')!=symbol or data['identity'].get('isin')!=isin:raise ValueError('NSE ISIN mismatch')
    identity=data['identity']
    if not 0<=(clock-datetime.fromisoformat(identity['observed_at'])).total_seconds()<86400 or len(identity.get('membership_sha256',''))!=64:raise ValueError('Official membership identity is stale or unverified')
    a,b=pair;days=[a['ts'][:10],b['ts'][:10]]
    if proof['sessions']!=days:raise ValueError('Reconciliation session mismatch')
    if not isinstance(data['actions'],list):raise ValueError('Invalid corporate action response')
    # Do not turn a verified demerger into a safe unadjusted signal history.
    if data['actions']:raise ValueError('Corporate action requires adjusted-history reconciliation')
    records=data['history']['data'];by_date={}
    if not isinstance(records,list):raise ValueError('Invalid exchange history')
    for row in records:
        if row.get('CH_SYMBOL')!=symbol or row.get('CH_SERIES')!='EQ':raise ValueError('Exchange history identity/series mismatch')
        day=datetime.strptime(row['mTIMESTAMP'],'%d-%b-%Y').date().isoformat()
        if day in by_date:raise ValueError('Duplicate exchange session')
        by_date[day]=row
    for local,day in zip(pair,days):
        official=by_date[day]
        for field,source in FIELDS.items():
            value=official[source]
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Non-numeric exchange candle')
            if abs(local[field]-value)>(0 if field=='volume' else .0001):raise ValueError('NSE and stored OHLCV disagree')
    if abs(by_date[days[1]]['CH_PREVIOUS_CLS_PRICE']-a['close'])>.0001:raise ValueError('Exchange previous close differs; adjustment needs review')
    return days[1]

def confirmed(db,symbol,rows,clock):
    identity=db.execute('SELECT isin FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    if not identity:return set()
    saved=get_state(db,'price_reconciliation_'+symbol,{}).get('proofs',{})
    valid=set()
    for pair in jumps(rows):
        proof=saved.get(pair[1]['ts'][:10])
        if not proof:continue
        try:valid.add(verify(proof,symbol,identity[0],pair,clock))
        except (ValueError,KeyError,TypeError,OverflowError):pass
    return valid

def reconcile(path,symbols=None):
    """Bounded daily public-source attempts, retaining failures for the next run."""
    from .financial_sources import request
    from .fallbacks import Fallbacks
    clock=datetime.now(timezone.utc);client=Fallbacks();results={}
    with connect(path) as db:
        instruments=[dict(r) for r in db.execute('SELECT symbol,isin FROM instruments WHERE '+symbol_filter(db))]
    for instrument in instruments:
        symbol=instrument['symbol'];isin=instrument['isin']
        if symbols is not None and symbol not in symbols:continue
        with connect(path) as db:
            rows=list(reversed([dict(r) for r in db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 201',(symbol,))]))
            saved=get_state(db,'price_reconciliation_'+symbol,{})
        pairs=jumps(rows)
        if not pairs:continue
        fingerprint=digest([list(pair) for pair in pairs])
        try:
            retry=3600 if any(a.get('status')=='unavailable' for a in saved.get('attempts',[])) else 86400
            checked=datetime.fromisoformat(saved['checked_at'])
            if saved.get('version')==1 and saved.get('fingerprint')==fingerprint and checked.astimezone(IST).date()==clock.astimezone(IST).date() and 0<=(clock-checked).total_seconds()<retry:continue
        except (KeyError,TypeError,ValueError):pass
        result={'version':1,'checked_at':now(),'fingerprint':fingerprint,'proofs':{},'attempts':[]}
        def fetch(url,params):
            response=request(client.session,url,params=params)
            try:return response.json()
            finally:response.close()
        try:
            # Reuse the independently verified official constituent identities;
            # NSE's quote API is not needed for this daily-history comparison.
            with connect(path) as db:
                members=membership(db,datetime.now(timezone.utc));snapshot=get_state(db,MEMBERSHIP_KEY,{})
            identity={'symbol':symbol,'isin':members.get(symbol),'observed_at':snapshot['fetched_at'],'membership_sha256':snapshot['sha256']}
            for pair in pairs[:5]:
                start,end=[datetime.fromisoformat(r['ts']).strftime('%d-%m-%Y') for r in pair]
                try:
                    actions=fetch(ACTIONS,{'index':'equities','symbol':symbol,'from_date':start,'to_date':end})
                    history=fetch(HISTORY,{'symbol':symbol,'series':'EQ','type':'priceVolumeDeliverable','from':start,'to':end})
                    evidence={'identity':identity,'history':history,'actions':actions}
                    proof={'symbol':symbol,'isin':isin,'observed_at':now(),'sessions':[r['ts'][:10] for r in pair],
                           'sources':{'history':HISTORY,'actions':ACTIONS,'identity':IDENTITY},'evidence':evidence,'sha256':digest(evidence)}
                    day=verify(proof,symbol,isin,pair,datetime.now(timezone.utc));result['proofs'][day]=proof
                    result['attempts'].append({'session':day,'status':'verified_market_move'})
                except Exception as error:
                    unavailable=not isinstance(error,(ValueError,KeyError,TypeError)) or any(t in str(error) for t in ('HTTP','cooldown'))
                    result['attempts'].append({'session':pair[1]['ts'][:10],'status':'unavailable' if unavailable else 'unresolved','reason':str(error)[:200]})
        except Exception as error:result['attempts'].append({'status':'unavailable','reason':str(error)[:200]})
        with connect(path) as db:set_state(db,'price_reconciliation_'+symbol,result)
        results[symbol]=result['attempts']
    return results
