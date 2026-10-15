import json,uuid
from datetime import datetime,timezone,timedelta
from .storage import connect,now,get_state
from .execution import fetch_book,match,charges,money
from .quality import init_quality,require_buy_quality

def init_paper(path):
    init_quality(path)
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS portfolios(name TEXT PRIMARY KEY,initial_cash REAL NOT NULL,cash REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS positions(portfolio TEXT NOT NULL REFERENCES portfolios(name),symbol TEXT NOT NULL REFERENCES instruments(symbol),quantity INTEGER NOT NULL CHECK(quantity>=0),cost REAL NOT NULL,PRIMARY KEY(portfolio,symbol));
        CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,portfolio TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,price REAL NOT NULL,fee REAL NOT NULL,quote_time INTEGER NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS depth_used(book_key TEXT NOT NULL,side TEXT NOT NULL,price REAL NOT NULL,quantity INTEGER NOT NULL,PRIMARY KEY(book_key,side,price));
        """)
        columns={r[1] for r in db.execute('PRAGMA table_info(trades)')}
        for name,definition in [('source',"TEXT NOT NULL DEFAULT 'manual'"),('reason',"TEXT NOT NULL DEFAULT ''"),('requested_quantity','INTEGER'),('execution',"TEXT NOT NULL DEFAULT '{}'"),('fill_model',"TEXT NOT NULL DEFAULT 'legacy_ltp'")]:
            if name not in columns:db.execute('ALTER TABLE trades ADD COLUMN '+name+' '+definition)
        db.execute('UPDATE trades SET requested_quantity=quantity WHERE requested_quantity IS NULL')
        for name in ('trader','investor'):db.execute('INSERT OR IGNORE INTO portfolios VALUES(?,?,?)',(name,1000000,1000000))

def duplicate(db,payload):
    row=db.execute('SELECT * FROM trades WHERE request_id=?',(payload['request_id'],)).fetchone()
    if row:
        if any(row[k]!=payload[v] for k,v in [('portfolio','portfolio'),('symbol','symbol'),('side','side'),('requested_quantity','quantity')]):raise ValueError('Order identifier already used for a different order.')
        return dict(row)

def preflight(db,payload,clock,auto_guard):
    portfolio,symbol=payload['portfolio'],payload['symbol']
    if auto_guard is None and get_state(db,'auto_config',{}).get(portfolio,{}).get('enabled'):raise ValueError('Pause automatic trading for this portfolio before placing a manual order.')
    stream=get_state(db,'stream',{})
    if stream.get('status')!='connected' or stream.get('market_status',{}).get('NSE_EQ')!='NORMAL_OPEN':raise ValueError('Paper fills need a connected feed and an open NSE equity session.')
    quote=db.execute('SELECT * FROM quotes WHERE symbol=?',(symbol,)).fetchone()
    if not quote or not 0<=clock.timestamp()*1000-quote['trade_time']<=30000:raise ValueError('Paper order blocked: no live trade price within 30 seconds.')
    if not 0<=(clock-datetime.fromisoformat(quote['received_at'])).total_seconds()<=30:raise ValueError('Paper order blocked: feed observation is stale.')
    latest=db.execute('SELECT close FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1',(symbol,)).fetchone()
    if latest and quote['previous_close'] and abs(quote['previous_close']/latest['close']-1)>.25:
        raise ValueError('Previous close differs from stored history by over 25%; corporate-action/position review required before any fill.')
    if payload['side']=='BUY':require_buy_quality(db,symbol,portfolio,clock)
    account=db.execute('SELECT * FROM portfolios WHERE name=?',(portfolio,)).fetchone()
    position=db.execute('SELECT * FROM positions WHERE portfolio=? AND symbol=?',(portfolio,symbol)).fetchone()
    if payload['side']=='SELL' and payload['quantity']>(position['quantity'] if position else 0):raise ValueError('Cannot sell more shares than this paper portfolio holds.')
    return quote,account,position

def trade(path,payload,clock=None,auto_guard=None):
    realtime=clock is None;clock=clock or datetime.now(timezone.utc)
    portfolio=payload.get('portfolio');symbol=payload.get('symbol');side=payload.get('side');requested=payload.get('quantity');request_id=payload.get('request_id')
    if portfolio not in ('trader','investor') or side not in ('BUY','SELL'):raise ValueError('Choose a valid paper portfolio and side.')
    if not isinstance(symbol,str) or not symbol:raise ValueError('Choose a stock.')
    if isinstance(requested,bool) or not isinstance(requested,int) or not 1<=requested<=1000000:raise ValueError('Quantity must be a positive whole number, at most 1,000,000.')
    if not isinstance(request_id,str) or not 16<=len(request_id)<=80:raise ValueError('Missing valid order identifier.')
    with connect(path) as db:
        existing=duplicate(db,payload)
        if existing:return existing
        preflight(db,payload,clock,auto_guard)
    # Read-only network call outside the ledger transaction. Everything is rechecked below.
    book=fetch_book(path,symbol)
    if realtime:clock=datetime.now(timezone.utc)
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        existing=duplicate(db,payload)
        if existing:return existing
        quote,account,position=preflight(db,payload,clock,auto_guard)
        if book['symbol']!=symbol:raise ValueError('Depth identity mismatch.')
        fill=match(db,book,side,requested,clock)
        qty,price,value=fill['quantity'],fill['price'],fill['value']
        if abs(price/quote['price']-1)>.05:raise ValueError('Depth and live price disagree by more than 5%.')
        day=clock.astimezone(timezone(timedelta(hours=5,minutes=30))).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
        dp=side=='SELL' and not db.execute("SELECT 1 FROM trades WHERE portfolio=? AND symbol=? AND side='SELL' AND created_at>=?",(portfolio,symbol,day)).fetchone()
        costs=charges(value,side,dp);fee=costs['total']
        effective=dict(quote);effective.update(price=price,fill_quantity=qty,fill_fee=fee,execution_time=clock.isoformat())
        reason=auto_guard(db,payload,effective,account,position) if auto_guard else ''
        held=position['quantity'] if position else 0;cost=position['cost'] if position else 0
        if side=='BUY':
            # Reject an unaffordable request, even if a small partial fill would fit.
            requested_value=money(max(x['price'] for x in fill['levels'])*requested)
            if requested_value+charges(requested_value,side)['total']>account['cash']:raise ValueError('Insufficient paper cash, including estimated costs.')
            total=value+fee;cash=money(account['cash']-total);new_qty=held+qty;new_cost=money(cost+total)
        else:
            cash=money(account['cash']+value-fee);new_qty=held-qty;new_cost=money(cost*new_qty/held)
        db.execute('UPDATE portfolios SET cash=? WHERE name=?',(cash,portfolio))
        db.execute('INSERT INTO positions VALUES(?,?,?,?) ON CONFLICT(portfolio,symbol) DO UPDATE SET quantity=excluded.quantity,cost=excluded.cost',(portfolio,symbol,new_qty,new_cost))
        for level in fill['levels']:
            db.execute('INSERT INTO depth_used VALUES(?,?,?,?) ON CONFLICT(book_key,side,price) DO UPDATE SET quantity=quantity+excluded.quantity',(book['key'],side,level['price'],level['quantity']))
        fill.update(charges=costs,book=book,requested_quantity=requested,slippage_vs_ltp_bps=round((price/quote['price']-1)*(1 if side=='BUY' else -1)*10000,2),charge_basis='Delivery-style estimate; same-day netting and settlement not modeled')
        row=(str(uuid.uuid4()),request_id,portfolio,symbol,side,qty,price,fee,quote['trade_time'],clock.isoformat(),'automatic' if auto_guard else 'manual',reason,requested,json.dumps(fill),fill['model'])
        db.execute('INSERT INTO trades(id,request_id,portfolio,symbol,side,quantity,price,fee,quote_time,created_at,source,reason,requested_quantity,execution,fill_model) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',row)
        if auto_guard:
            from .autotrading import event
            event(db,portfolio,'fill',side+' '+str(qty)+' / '+str(requested)+' shares ('+fill['status']+'): '+reason,symbol,row[0],clock)
        return dict(db.execute('SELECT * FROM trades WHERE id=?',(row[0],)).fetchone())

def portfolios(path):
    with connect(path) as db:
        db.execute('BEGIN') # One consistent read snapshot for holdings and trade attribution.
        accounts=[dict(r) for r in db.execute('SELECT * FROM portfolios')]
        for a in accounts:
            positions=[]
            for row in db.execute('SELECT * FROM positions WHERE portfolio=? AND quantity>0',(a['name'],)):
                p=dict(row);q=db.execute('SELECT price,trade_time,received_at FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
                c=db.execute('SELECT close,ts FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 1',(p['symbol'],)).fetchone()
                p['mark']=q['price'] if q else c['close'] if c else None
                p['mark_time']=datetime.fromtimestamp(q['trade_time']/1000,timezone.utc).isoformat() if q else c['ts'] if c else None
                p['mark_source']='last_trade' if q else 'daily_close' if c else 'missing'
                p['value']=p['quantity']*p['mark'] if p['mark'] is not None else None
                p['unrealized_pnl']=p['value']-p['cost'] if p['value'] is not None else None
                positions.append(p)
            a['positions']=positions;a['equity']=a['cash']+sum(p['value'] for p in positions) if all(p['value'] is not None for p in positions) else None
            a['pnl']=a['equity']-a['initial_cash'] if a['equity'] is not None else None
            a['trades']=[dict(r) for r in db.execute('SELECT * FROM trades WHERE portfolio=? ORDER BY created_at DESC LIMIT 200',(a['name'],))]
            if a['name']=='trader':
                from .strategy_performance import trader_performance
                a['strategy_performance']=trader_performance(db,a)
        return accounts
