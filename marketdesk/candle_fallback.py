"""Alternate daily history when Upstox is unavailable. Never an execution quote.

Yahoo Finance via yfinance is optional best-effort research data. Require NSE/INR
metadata, valid completed OHLCV and comparison with saved primary closes. Never
substitute adjusted prices silently, overwrite primary history, or invent candles.
Unconfirmed history is retained with a buy-blocking source-review issue.
"""
from datetime import datetime,timedelta,timezone
import json
from .storage import connect,ROOT,now,set_state
from .importer import candle_row
IST=timezone(timedelta(hours=5,minutes=30))


def accept(path,symbol,isin,metadata,rows):
    if metadata.get('symbol')!=symbol+'.NS' or metadata.get('currency')!='INR' or metadata.get('exchangeName') not in ('NSI','NSE'):
        raise ValueError('Alternate history identity/currency/exchange mismatch')
    parsed=[candle_row(row) for row in rows]
    if not parsed or len({r[0][:10] for r in parsed})!=len(parsed):raise ValueError('Empty or duplicate alternate candles')
    with connect(path) as db:
        identity=db.execute('SELECT isin FROM instruments WHERE symbol=?',(symbol,)).fetchone()
        if not identity or identity[0]!=isin:raise ValueError('Instrument identity changed during alternate fetch')
        old={r[0][:10]:r[1] for r in db.execute('SELECT ts,close FROM candles WHERE symbol=?',(symbol,))}
        primary={r[0][:10]:r[1] for r in db.execute("SELECT ts,close FROM candles WHERE symbol=? AND source='upstox_daily_export'",(symbol,))}
        overlap=[r for r in parsed if r[0][:10] in primary]
        if any(abs(r[4]/primary[r[0][:10]]-1)>.01 for r in overlap):raise ValueError('Alternate closes disagree with primary history; review required')
        confirmed=len(overlap)>=5
        inserted=0
        for row in parsed:
            if row[0][:10] in old:continue
            db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',(symbol,*row,'yahoo_daily_unadjusted',now()));inserted+=1
        if not confirmed:
            db.execute('INSERT OR IGNORE INTO issues VALUES(?,?,?)',(symbol,'candle','Alternate daily history lacks five matching primary sessions; source review required'))
        set_state(db,'candle_fallback_'+symbol,dict(source='yahoo_finance',checked_at=now(),inserted=inserted,overlap_sessions=len(overlap),status='cross_checked' if confirmed else 'review_required'))
    return inserted


def fetch(path,symbol,isin,start,end):
    import yfinance as yf
    yf.set_tz_cache_location(str(ROOT/'data/yahoo-cache'))
    ticker=yf.Ticker(symbol+'.NS')
    frame=ticker.history(start=str(start),end=str(end+timedelta(days=1)),interval='1d',auto_adjust=False,back_adjust=False,repair=False,actions=False,timeout=10,raise_errors=True)
    metadata=ticker.get_history_metadata();rows=[];rejected=[]
    today=datetime.now(IST).date()
    for stamp,r in frame.iterrows():
        day=stamp.date()
        if not start<=day<=end or day>=today:continue
        row=[day.isoformat()+'T00:00:00+05:30',float(r['Open']),float(r['High']),float(r['Low']),float(r['Close']),float(r['Volume'])]
        try:candle_row(row)
        except ValueError as error:
            rejected.append({'session':day.isoformat(),'reason':str(error)});continue
        rows.append(row)
    # Keep the source response available for later reconciliation.
    folder=ROOT/'data/candle-alternatives';folder.mkdir(parents=True,exist_ok=True)
    record=dict(symbol=symbol,isin=isin,source='yahoo_finance',observed_at=now(),metadata={k:metadata.get(k) for k in ('symbol','currency','exchangeName','instrumentType','exchangeTimezoneName')},candles=rows,rejected=rejected)
    target=folder/(symbol.replace('/','_')+'.json');temp=target.with_suffix('.json.tmp');temp.write_text(json.dumps(record,allow_nan=False,default=str));temp.replace(target)
    return accept(path,symbol,isin,metadata,rows)
