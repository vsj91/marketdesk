"""5-minute candles for every streamed stock, built from the live feed instead of per-stock candle downloads.

Prices come from the Upstox LTPC websocket the app already runs for the whole Nifty 500: every accepted tick
updates its stock's candle (open, high, low, close) in sb_live_bars. LTPC carries no volume, so at each completed
5-minute boundary the runner takes one batch quote snapshot (Upstox market-quote, up to 250 stocks a request)
and books each stock's volume-traded-today increase as the volume of the candle that just closed. A candle is
only returned once its volume is booked, so readers see completed candles only.

Limits: a candle's high and low are the highest and lowest ticks the feed delivered (the feed can skip ticks in
fast markets); the volume snapshot is taken about 15 seconds after the boundary, so a little of the next
candle's volume lands in the closed one; a stock with no trade in a 5-minute window gets a flat candle at its
last price. Candles exist only from when the app was running during the session."""
from datetime import datetime,timedelta,timezone

IST=timezone(timedelta(hours=5,minutes=30))
TF=5

def bucket(ltt_ms,tf=TF):
    """Start of the tf-minute candle (IST ISO text) containing a trade time in epoch milliseconds; None outside 09:15-15:30."""
    t=datetime.fromtimestamp(ltt_ms/1000,IST)
    if not '09:15'<=t.strftime('%H:%M')<'15:30':return None
    return t.replace(minute=t.minute-t.minute%tf,second=0,microsecond=0).isoformat()

def record(db,symbol,ltpc,tf=TF):
    """Fold one accepted live tick into its candle. Called by the feed in the same transaction as the quote."""
    try:price=float(ltpc.get('ltp'));ts=bucket(int(ltpc.get('ltt')),tf)
    except (TypeError,ValueError):return
    if ts is None or not price>0:return
    db.execute('''INSERT INTO sb_live_bars(symbol,interval,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,NULL)
        ON CONFLICT(symbol,interval,ts) DO UPDATE SET high=MAX(high,excluded.high),low=MIN(low,excluded.low),close=excluded.close''',
        (symbol,tf,ts,price,price,price,price))

def finalize(db,snapshot,boundary,tf=TF):
    """Book volume for candles that ended at or before `boundary` (IST datetime of the just-completed boundary).

    `snapshot` maps symbol -> dict(volume=volume traded today, last_price=...). The increase over the previous
    snapshot of the same session becomes the volume of the newest completed candle; earlier unbooked candles of
    the session get 0. A stock that traded earlier today but not in the closed window gets a flat candle."""
    day=boundary.date().isoformat();last=(boundary-timedelta(minutes=tf)).isoformat();booked=0
    for symbol,q in snapshot.items():
        vtt=q.get('volume')
        if vtt is None or vtt<0:continue
        prev=db.execute('SELECT day,volume FROM sb_live_volume WHERE symbol=?',(symbol,)).fetchone()
        delta=vtt-prev['volume'] if prev and prev['day']==day and vtt>=prev['volume'] else vtt
        row=db.execute('SELECT ts FROM sb_live_bars WHERE symbol=? AND interval=? AND ts=?',(symbol,tf,last)).fetchone()
        if not row:
            before=db.execute('SELECT close FROM sb_live_bars WHERE symbol=? AND interval=? AND substr(ts,1,10)=? AND ts<? ORDER BY ts DESC LIMIT 1',(symbol,tf,day,last)).fetchone()
            if before:db.execute('INSERT INTO sb_live_bars(symbol,interval,ts,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,NULL)',(symbol,tf,last,*(before['close'],)*4))
        db.execute('UPDATE sb_live_bars SET volume=0 WHERE symbol=? AND interval=? AND substr(ts,1,10)=? AND ts<? AND volume IS NULL',(symbol,tf,day,last))
        booked+=db.execute('UPDATE sb_live_bars SET volume=? WHERE symbol=? AND interval=? AND ts=? AND volume IS NULL',(delta,symbol,tf,last)).rowcount
        db.execute('INSERT INTO sb_live_volume VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET day=excluded.day,volume=excluded.volume',(symbol,day,vtt))
    return booked

def bars(db,symbol,clock,tf=TF,limit=400):
    """Completed live candles (volume booked), oldest first."""
    cutoff=(clock-timedelta(minutes=tf)).astimezone(IST).isoformat()
    rows=db.execute('SELECT ts,open,high,low,close,volume FROM sb_live_bars WHERE symbol=? AND interval=? AND ts<=? AND volume IS NOT NULL ORDER BY ts DESC LIMIT ?',(symbol,tf,cutoff,limit)).fetchall()
    return [dict(r) for r in reversed(rows)]
