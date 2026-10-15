"""Read-only Upstox 1-minute history, cached separately from the running paper database."""
import collections, gzip, hashlib, json, math, os, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
import requests
from .autotrading import IST
from .feed import load_token
from .quality import inspect_history
from .replay import load_snapshot, signal_from


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,allow_nan=False));os.replace(tmp,path)


def prepare(path,start,end,directory):
    first=datetime.fromisoformat(start).date();last=datetime.fromisoformat(end).date()
    if first>last or (last-first).days>30:raise ValueError('Request an inclusive range of at most 31 days')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    snapshot=load_snapshot(path,end)
    with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
        keys=dict(db.execute('SELECT symbol,instrument_key FROM instruments'))
    available=[d for d in snapshot['sessions'] if d<datetime.now(IST).date().isoformat() and d<=end]
    days=[d for d in available if start<=d];frames={};universe=set()
    if not days:raise ValueError('No completed local sessions in the requested range')
    for day in days:
        clock=datetime.fromisoformat(day+'T09:20:00').replace(tzinfo=IST)
        previous=[d for d in available if d<day];signals={};eligible=set();daily={}
        for symbol in snapshot['symbols']:
            history=snapshot['histories'].get(symbol,[])
            prior=[r for r in history if r['ts'][:10]<day][-201:]
            today=[r for r in history if r['ts'][:10]==day]
            if len(today)==1:daily[symbol]=today[0]
            # Also retain valid indicators for exits after a stock ceases to qualify for entry.
            if symbol in snapshot['bad'] or inspect_history(prior,previous,clock):continue
            sig=signal_from(prior,{})
            sig['average_volume20']=sum(r['volume'] for r in prior[-20:])/20
            signals[symbol]=sig
            if (sig['dma50']>sig['dma200'] and sig['volume_ratio']>=1.2 and sig['turnover20']>=10_000_000
                and sig['close']*1.15>=20 and sig['high20']<sig['close']*1.15):
                eligible.add(symbol);universe.add(symbol)
        frames[day]={'signals':signals,'eligible':sorted(eligible),'daily':daily}
    # Inclusion uses prior-day inputs only, never the symbols selected by the daily replay.
    plan={'start':start,'end':end,'days':days,'frames':frames,'keys':{s:keys[s] for s in sorted(universe)},
          'snapshot_captured_at':snapshot['captured_at'],'input_sha256':snapshot['input_sha256']}
    atomic_json(directory/'plan.json',plan)
    print('Prepared '+str(len(days))+' sessions; '+str(len(universe))+' potential Trader instruments',flush=True)
    return plan


def validate_bars(payload,start,end):
    if not isinstance(payload,dict) or payload.get('status')!='success' or not isinstance(payload.get('data'),dict) or not isinstance(payload['data'].get('candles'),list):
        raise ValueError('No successful candle array')
    rows={}
    for item in payload['data']['candles']:
        if not isinstance(item,list) or len(item)<6:raise ValueError('Malformed candle')
        stamp=datetime.fromisoformat(item[0])
        if stamp.tzinfo is None:raise ValueError('Candle timezone missing')
        stamp=stamp.astimezone(IST);day=stamp.date().isoformat()
        if not start<=day<=end or stamp.second or stamp.microsecond:raise ValueError('Candle outside requested minute range')
        if not (9,15)<=(stamp.hour,stamp.minute)<(15,30):raise ValueError('Candle outside normal equity session')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in item[1:6]):raise ValueError('Invalid candle number')
        op,hi,lo,cl,vol=item[1:6]
        if min(op,hi,lo,cl)<=0 or vol<0 or int(vol)!=vol or hi<max(op,lo,cl) or lo>min(op,hi,cl):raise ValueError('Invalid OHLCV')
        row=[stamp.isoformat(),op,hi,lo,cl,int(vol)]
        if row[0] in rows and rows[row[0]]!=row:raise ValueError('Conflicting minute duplicate')
        rows[row[0]]=row
    return sorted(rows.values())


class HistoryDownload:
    def __init__(self,directory):
        self.directory=Path(directory);self.lock=threading.Lock();self.last=0;self.token=load_token()
        if not self.token:raise ValueError('Save a valid Upstox data token first')
    def fetch(self,symbol,key,start,end):
        identity=hashlib.sha256((key+start+end).encode()).hexdigest()[:20]
        path=self.directory/'raw'/(identity+'.json.gz');path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            with gzip.open(path,'rt') as f:record=json.load(f)
            if (record['symbol'],record['instrument_key'],record['start'],record['end'])!=(symbol,key,start,end):raise ValueError('Cache identity mismatch')
            validate_bars(record['payload'],start,end)
            return path,record
        url='https://api.upstox.com/v3/historical-candle/'+quote(key,safe='')+'/minutes/1/'+end+'/'+start
        last_error='Unknown download error'
        for attempt in range(3):
            with self.lock:
                delay=1-(time.monotonic()-self.last)
                if delay>0:time.sleep(delay)
                self.last=time.monotonic()
            try:
                response=requests.get(url,headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'},timeout=(5,30),allow_redirects=False)
                if response.status_code in (401,403):raise PermissionError('Upstox data token rejected; renew it in Connection')
                if response.status_code!=200:
                    last_error='HTTP '+str(response.status_code)
                    if response.status_code==429 or response.status_code>=500:time.sleep(2**attempt);continue
                    raise ValueError(last_error)
                payload=response.json();validate_bars(payload,start,end)
                record={'symbol':symbol,'instrument_key':key,'start':start,'end':end,'url':url,
                        'received_at':datetime.now(timezone.utc).isoformat(),'payload':payload}
                tmp=path.with_suffix('.tmp')
                with gzip.open(tmp,'wt') as f:json.dump(record,f)
                os.replace(tmp,path)
                return path,record
            except requests.RequestException as error:
                last_error=type(error).__name__;time.sleep(2**attempt)
        raise ValueError('History download failed: '+last_error)


def download(plan,directory):
    directory=Path(directory);client=HistoryDownload(directory)
    db=sqlite3.connect(directory/'minutes.sqlite3')
    db.executescript('CREATE TABLE IF NOT EXISTS bars(symbol TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume INTEGER,PRIMARY KEY(symbol,ts)); CREATE TABLE IF NOT EXISTS fetched(symbol TEXT PRIMARY KEY,metadata TEXT);')
    status={'requested':len(plan['keys']),'completed':0,'bars':0,'errors':{},'state':'downloading'}
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs={pool.submit(client.fetch,s,key,plan['start'],plan['end']):s for s,key in plan['keys'].items()}
            for job in as_completed(jobs):
                symbol=jobs[job]
                try:
                    path,record=job.result();rows=validate_bars(record['payload'],plan['start'],plan['end'])
                    metadata={k:v for k,v in record.items() if k!='payload'}
                    metadata.update(raw_file=str(path),sha256=hashlib.sha256(path.read_bytes()).hexdigest(),bars=len(rows))
                    with db:
                        db.execute('DELETE FROM bars WHERE symbol=?',(symbol,))
                        db.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?)',[(symbol,*row) for row in rows])
                        db.execute('INSERT OR REPLACE INTO fetched VALUES(?,?)',(symbol,json.dumps(metadata)))
                    status['completed']+=1;status['bars']+=len(rows)
                except PermissionError:
                    for future in jobs:future.cancel()
                    raise
                except Exception as error:status['errors'][symbol]=type(error).__name__+': '+str(error)[:160]
                atomic_json(directory/'download-status.json',status)
                if (status['completed']+len(status['errors']))%25==0:print(json.dumps(status|{'errors':len(status['errors'])}),flush=True)
        db.execute('CREATE INDEX IF NOT EXISTS bars_ts ON bars(ts)');db.commit()
        status['state']='complete' if not status['errors'] else 'partial';atomic_json(directory/'download-status.json',status)
        return status
    finally:db.close()
