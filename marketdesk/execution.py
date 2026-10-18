"""Read-only quotes and a local, depth-limited IOC simulation. No broker orders."""
import hashlib,json,threading,time
from datetime import datetime,timezone,timedelta
from decimal import Decimal,ROUND_HALF_UP
import requests
from .storage import connect,number
from .feed import load_token
VERSION='depth-ioc-v1'
_lock=threading.Lock();_last_request=0

def money(x):return float(Decimal(str(x)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))

def charges(value,side,dp=False):
    # Delivery-style estimates for both swing portfolios; not a contract note.
    # Published Upstox schedule checked 2026-10-12. Exchange rate effective March 2026.
    v=Decimal(str(value))
    raw={'brokerage':Decimal(20),'stt':v*Decimal('.001'),'exchange':v*Decimal('.0000307'),'sebi':v*Decimal('.000001'),'ipft':v*Decimal('.000000001'),'stamp':v*Decimal('.00015') if side=='BUY' else Decimal(0),'dp':Decimal(20) if dp else Decimal(0)}
    raw['gst']=(raw['brokerage']+raw['exchange']+raw['ipft']+raw['dp'])*Decimal('.18')
    result={k:money(v) for k,v in raw.items()};result['total']=money(sum(Decimal(str(v)) for v in result.values()))
    return result

def automatic_buy_cost_check(config,value,buy_fee):
    """Check actual filled value, never enlarge orders or constrain exits.

    Exit fees assume a delivery sale at the entry value, including DP charges.
    This estimates fee drag only; it is not a profit or executable-exit forecast.
    """
    raw=config.get('max_round_trip_fee_pct')
    if raw is None:return None
    limit=number(raw)
    if limit is None or limit<=0:raise ValueError('Invalid automatic buy fee limit; new buys wait.')
    exit_fee=charges(value,'SELL',True)['total']
    total=money(buy_fee+exit_fee)
    if value<=0 or Decimal(str(total))*100>Decimal(str(value))*Decimal(str(limit)):
        pct=total/value*100 if value>0 else float('inf')
        raise ValueError(f'Buy too small for fee floor: estimated round-trip fees ₹{total:.2f} ({pct:.2f}%) exceed {limit:g}% of actual fill value ₹{value:.2f}.')
    return dict(max_round_trip_fee_pct=limit,buy_fee=buy_fee,estimated_sell_fee=exit_fee,
        estimated_round_trip_fee=total,filled_value=value,estimated_round_trip_fee_pct=total/value*100,
        basis='Same-price delivery sale including DP charges; spread, slippage and partial-exit costs excluded')


def normalize(raw,key,symbol,received):
    if raw.get('instrument_token')!=key or raw.get('symbol')!=symbol:raise ValueError('Depth instrument identity mismatch.')
    try:
        stamp=datetime.fromisoformat(raw['timestamp']);ltt=int(raw['last_trade_time'])
        if stamp.tzinfo is None:raise ValueError()
        depth={}
        for side in ('buy','sell'):
            levels={}
            for row in raw.get('depth',{}).get(side,[]):
                p,q=number(row.get('price')),number(row.get('quantity'))
                if p==0 and q==0:continue
                if p is None or q is None or p<=0 or q<0 or not q.is_integer():raise ValueError()
                if q:levels[p]=max(levels.get(p,0),int(q))
            depth[side]=[{'price':p,'quantity':q} for p,q in sorted(levels.items(),reverse=side=='buy')]
        if not depth['buy'] or not depth['sell']:raise ValueError()
        bid,ask=depth['buy'][0]['price'],depth['sell'][0]['price']
        if bid>=ask or (ask-bid)/bid>.02:raise ValueError()
        last=number(raw.get('last_price'))
        if last is None or last<=0 or abs((bid+ask)/2/last-1)>.05:raise ValueError()
        lower,upper=number(raw.get('lower_circuit_limit')),number(raw.get('upper_circuit_limit'))
        if lower is None or upper is None or not 0<lower<upper:raise ValueError()
        if any(not lower<=r['price']<=upper for rows in depth.values() for r in rows):raise ValueError()
    except (KeyError,TypeError,ValueError,AttributeError):raise ValueError('Depth missing, crossed, too wide, or invalid; no paper fill.') from None
    digest=hashlib.sha256(json.dumps([key,depth,ltt,raw.get('volume')],sort_keys=True).encode()).hexdigest()
    return {'key':digest,'symbol':symbol,'timestamp':stamp.isoformat(),'received_at':received.isoformat(),'trade_time':ltt,'depth':depth,'source':'upstox_full_quote_v3','lower':lower,'upper':upper}

def fetch_book(path,symbol):
    global _last_request
    with connect(path) as db:row=db.execute('SELECT instrument_key FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    if not row:raise ValueError('Unknown instrument.')
    token=load_token()
    if not token:raise ValueError('Depth requires an Upstox data token.')
    try:
        with _lock:
            delay=1-(time.monotonic()-_last_request)
            if delay>0:time.sleep(delay)
            _last_request=time.monotonic()
            response=requests.get('https://api.upstox.com/v3/market-quote/quotes',params={'instrument_key':row[0]},headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(3,5))
        if response.status_code!=200:raise ValueError('Depth service unavailable (HTTP '+str(response.status_code)+'); no paper fill.')
        payload=response.json()
        if payload.get('status')!='success':raise ValueError('Depth service rejected the request; no paper fill.')
        raw=payload.get('data',{}).get('NSE_EQ:'+symbol,{})
        return normalize(raw,row[0],symbol,datetime.now(timezone.utc))
    except requests.RequestException:raise ValueError('Depth request failed; no paper fill.') from None

def match(db,book,side,requested,clock):
    for field in ('timestamp','received_at'):
        if not 0<=(clock-datetime.fromisoformat(book[field])).total_seconds()<=5:raise ValueError('Depth older than 5 seconds; no paper fill.')
    if not 0<=clock.timestamp()*1000-book['trade_time']<=30000:raise ValueError('Depth last trade older than 30 seconds; no paper fill.')
    levels=book['depth']['sell' if side=='BUY' else 'buy'];best=levels[0]['price'];fills=[];remaining=requested
    for level in levels:
        price=level['price']
        if abs(price/best-1)>.005:break
        used=db.execute('SELECT quantity FROM depth_used WHERE book_key=? AND side=? AND price=?',(book['key'],side,price)).fetchone()
        available=max(0,int(level['quantity']*.1)-(used[0] if used else 0))
        quantity=min(remaining,available)
        if quantity:
            fills.append({'price':price,'quantity':quantity});remaining-=quantity
        if not remaining:break
    if not fills:raise ValueError('No unused depth within the 10% participation and 0.5% price limits.')
    quantity=requested-remaining;value=money(sum(Decimal(str(x['price']))*x['quantity'] for x in fills))
    return {'quantity':quantity,'price':value/quantity,'value':value,'levels':fills,'best_price':best,'unfilled_quantity':remaining,'status':'PARTIAL_CANCELLED' if remaining else 'FILLED','model':VERSION}
