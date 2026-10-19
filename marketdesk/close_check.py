"""Independent daily check of saved candles against NSE's public bhavcopy (one file covers every stock).

Read-only: it never edits candles. Mismatches are surfaced on the Data health screen for review."""
import csv,io
from datetime import datetime,timedelta,timezone
import requests
from .storage import connect,get_state,set_state,now,number
from .working_universe import symbol_filter
IST=timezone(timedelta(hours=5,minutes=30))
URL='https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{}.csv'
CLOSE_TOLERANCE=.005
VOLUME_TOLERANCE=.05

def parse_bhav(text):
    """symbol -> {open,high,low,close,volume} for equity series; EQ wins over other series."""
    rows={}
    for raw in csv.DictReader(io.StringIO(text)):
        row={(k or '').strip():(v or '').strip() for k,v in raw.items()}
        series=row.get('SERIES')
        if series not in ('EQ','BE','BZ','SM','ST'):continue
        values={'open':number(row.get('OPEN_PRICE')),'high':number(row.get('HIGH_PRICE')),'low':number(row.get('LOW_PRICE')),'close':number(row.get('CLOSE_PRICE')),'volume':number(row.get('TTL_TRD_QNTY'))}
        if any(v is None for v in values.values()):continue
        symbol=row.get('SYMBOL')
        if symbol and (symbol not in rows or series=='EQ'):rows[symbol]=values
    return rows

def fetch_bhav(day,get=requests.get):
    stamp=datetime.fromisoformat(day).strftime('%d%m%Y')
    response=get(URL.format(stamp),headers={'User-Agent':'Mozilla/5.0'},timeout=30)
    if response.status_code==404:return None
    response.raise_for_status()
    return parse_bhav(response.text)

def compare(ours,theirs):
    """ours/theirs: symbol -> OHLCV dict. Returns counts and the largest close mismatches."""
    compared=0;close_bad=[];volume_bad=0;missing=[]
    for symbol,o in ours.items():
        t=theirs.get(symbol)
        if not t:missing.append(symbol);continue
        compared+=1
        if t['close']>0 and abs(o['close']/t['close']-1)>CLOSE_TOLERANCE:close_bad.append({'symbol':symbol,'ours':o['close'],'nse':t['close'],'pct':round(100*(o['close']/t['close']-1),2)})
        if t['volume']>0 and abs(o['volume']/t['volume']-1)>VOLUME_TOLERANCE:volume_bad+=1
    close_bad.sort(key=lambda x:-abs(x['pct']))
    return {'compared':compared,'close_mismatches':len(close_bad),'volume_mismatches':volume_bad,'not_in_bhavcopy':len(missing),'worst':close_bad[:15]}

def run(path,sessions=20,fetch=fetch_bhav,clock=None):
    clock=clock or datetime.now(timezone.utc)
    with connect(path) as db:
        days=get_state(db,'quality_sessions',[])[-sessions:]
        symbols=[r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))]
        previous=get_state(db,'close_check',{}).get('sessions',{})
    result={}
    for day in days:
        if day in previous and previous[day].get('compared'):result[day]=previous[day];continue
        try:theirs=fetch(day)
        except Exception as error:result[day]={'error':type(error).__name__};continue
        if theirs is None:result[day]={'error':'no bhavcopy (holiday or not yet published)'};continue
        with connect(path) as db:
            ours={}
            for s in symbols:
                r=db.execute("SELECT open,high,low,close,volume FROM candles WHERE symbol=? AND substr(ts,1,10)=?",(s,day)).fetchone()
                if r:ours[s]=dict(r)
        result[day]=compare(ours,theirs)
    summary={'checked_at':now(),'sessions':result,'source':'NSE sec_bhavdata_full','tolerance_pct':CLOSE_TOLERANCE*100}
    with connect(path) as db:set_state(db,'close_check',summary)
    return summary
