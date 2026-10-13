import json,os,re,secrets,threading,time,mimetypes,hashlib
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs,unquote
from .storage import ROOT,DEFAULT_DB,connect,init,now,get_state
from .analytics import listing
from .paper import init_paper,trade,portfolios
from .feed import Feed,load_token
from .refresh import Refresh
from .autotrading import AutoTrading
from .quality import audit
from .power import SleepGuard

class App:
    def __init__(self,path,read_only=False):
        self.path=path
        if not read_only:init(path);init_paper(path);audit(path)
        self.feed=None if read_only else Feed(path);self.refresh=None if read_only else Refresh(path)
        self.auto=None if read_only else AutoTrading(path,self.refresh)
        self.power=None if read_only else SleepGuard()
        self.csrf=secrets.token_urlsafe(32);self.cache=None;self.cached_at=0;self.cache_lock=threading.Lock()
    def stocks(self):
        with self.cache_lock:
            if self.cache is None or time.monotonic()-self.cached_at>5:
                self.cache=listing(self.path);self.cached_at=time.monotonic()
            return self.cache
    def health(self):
        with connect(self.path) as db:
            counts={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('instruments','candles','fundamentals','quotes','issues')}
            stream=get_state(db,'stream',{});refresh=get_state(db,'refresh',{});quality=get_state(db,'quality_summary',{});quality['repair_queue']=get_state(db,'quality_repair_queue',{})
            report=get_state(db,'last_import',{});repair=get_state(db,'fundamental_repair',{})
            incomplete=db.execute("SELECT COUNT(*) FROM fundamentals WHERE COALESCE(json_extract(details,'$.status'),'MISSING')!='COMPLETE'").fetchone()[0]
            missing={}
            for r in db.execute('SELECT details FROM fundamentals'):
                for key in json.loads(r[0]).get('missing_fields',[]):missing[key]=missing.get(key,0)+1
            missing_records=counts['instruments']-counts['fundamentals']
            if missing_records:
                from .provider import FUNDAMENTAL_REQUIRED_FIELDS
                for field in (*FUNDAMENTAL_REQUIRED_FIELDS,'sector'):missing[field]=missing.get(field,0)+missing_records
            full={endpoint:db.execute("SELECT COUNT(*) FROM payloads WHERE endpoint=? AND json_array_length(json_extract(payload,'$.data.full_statement'))>0",(endpoint,)).fetchone()[0] for endpoint in ('income_statement','balance_sheet','cash_flow')}
            failures=db.execute("SELECT COUNT(*) FROM payloads WHERE http_status!=200 OR error IS NOT NULL OR COALESCE(json_extract(payload,'$.status'),'')!='success'").fetchone()[0]
            latest=db.execute('SELECT MAX(ts) FROM candles').fetchone()[0]
            short=db.execute('SELECT COUNT(*) FROM (SELECT symbol FROM candles GROUP BY symbol HAVING COUNT(*)<200)').fetchone()[0]
        return {'fundamental_repair':repair,'sleep_guard':self.power.snapshot() if self.power else None,'quality':quality,'counts':counts,'stream':stream,'refresh':{k:v for k,v in refresh.items() if k!='completed_symbols'},'import':report,'token_saved':bool(load_token()),'complete_fundamentals':counts['fundamentals']-incomplete,'partial_fundamentals':incomplete+missing_records,'missing_fields':missing,'detailed_statements':full,'failed_endpoints':failures,'latest_candle':latest,'short_histories':short,'mode':'local_paper_only','now':now(),'csrf':self.csrf}

class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,app):self.app=app;super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,format,*args):pass
    def send(self,data,status=200,content='application/json; charset=utf-8'):
        body=json.dumps(data,allow_nan=False).encode() if content.startswith('application/json') else data
        self.send_response(status);self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        try:self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):pass
    def allowed(self):
        host=self.headers.get('Host','');port=self.server.server_port
        return host in (f'127.0.0.1:{port}',f'localhost:{port}')
    def do_GET(self):
        if not self.allowed():return self.send({'error':'Local host required'},403)
        app=self.server.app;url=urlsplit(self.path);q=parse_qs(url.query)
        try:
            if url.path=='/api/health':return self.send(app.health())
            if url.path=='/api/fundamental-repair':
                file=ROOT/'data/fundamental-repair/latest.json'
                return self.send(json.loads(file.read_text()) if file.exists() else {'status':'not_started','results':[]})
            if url.path=='/api/stocks':return self.send({'stocks':app.stocks()})
            if url.path=='/api/paper':return self.send({'portfolios':portfolios(app.path),'automation':app.auto.snapshot()})
            if url.path=='/api/stock':
                symbol=q.get('symbol',[''])[0]
                with connect(app.path) as db:
                    i=db.execute('SELECT * FROM instruments WHERE symbol=?',(symbol,)).fetchone()
                    if not i:return self.send({'error':'Unknown stock'},404)
                    f=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
                    payloads=[dict(r) for r in db.execute('SELECT endpoint,http_status,error,observed_at,source_url,payload FROM payloads WHERE symbol=?',(symbol,))]
                    for p in payloads:p['payload']=json.loads(p['payload'])
                    return self.send({'instrument':dict(i),'candles':[dict(r) for r in db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts',(symbol,))],
                        'quality':dict(db.execute('SELECT * FROM quality WHERE symbol=?',(symbol,)).fetchone() or {}),'fundamentals':json.loads(f['details']) if f else {},'payloads':payloads,'issues':[dict(r) for r in db.execute('SELECT * FROM issues WHERE symbol=?',(symbol,))]})
            name=unquote(url.path).lstrip('/') or 'index.html';root=(ROOT/'web').resolve();file=(root/name).resolve()
            if root not in file.parents or not file.is_file():return self.send({'error':'Not found'},404)
            return self.send(file.read_bytes(),content=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        except Exception:return self.send({'error':'Local request failed. Check server and database.'},500)
    def do_POST(self):
        app=self.server.app
        if not self.allowed():return self.send({'error':'Local host required'},403)
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://localhost:{self.server.server_port}',f'http://127.0.0.1:{self.server.server_port}'):
            return self.send({'error':'Local origin required'},403)
        if not secrets.compare_digest(self.headers.get('X-Local-CSRF',''),app.csrf):return self.send({'error':'Reload the local app and retry.'},403)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>20000:raise ValueError('Request too large')
            payload=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(payload,dict):raise ValueError('Expected a JSON object')
            path=urlsplit(self.path).path
            if path=='/api/token':
                token=payload.get('token','').strip()
                if not re.fullmatch(r'[A-Za-z0-9_.~-]{50,8192}',token):raise ValueError('Paste a valid access token.')
                fd=os.open(ROOT/'.env',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as f:f.write('UPSTOX_TOKEN='+token+'\n')
                os.chmod(ROOT/'.env',0o600)
                return self.send({'saved':True})
            if path=='/api/feed/start':app.feed.start();return self.send({'status':'connecting'})
            if path=='/api/feed/stop':app.feed.stop();return self.send({'status':'disconnected'})
            if path=='/api/refresh':
                symbols=payload.get('symbols',[])
                if not isinstance(symbols,list) or len(symbols)>5000 or any(not isinstance(x,str) for x in symbols):raise ValueError('Invalid symbol list')
                app.refresh.start(symbols,payload.get('mode','both'));return self.send({'status':'running'},202)
            if path=='/api/paper/automation':return self.send(app.auto.configure(payload.get('portfolio'),payload.get('enabled')))
            if path=='/api/paper/order':return self.send(trade(app.path,payload))
            if path=='/api/watch':
                with connect(app.path) as db:
                    if payload.get('watched'):db.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(payload.get('symbol'),))
                    else:db.execute('DELETE FROM watchlist WHERE symbol=?',(payload.get('symbol'),))
                app.cache=None;return self.send({'saved':True})
            return self.send({'error':'Not found'},404)
        except ValueError as e:return self.send({'error':str(e)},400)
        except Exception:return self.send({'error':'Local action failed. No broker order was placed.'},500)

def serve(path=DEFAULT_DB,port=8765,live=False):
    # Bind first: a duplicate launch must not reset the existing feed/job state.
    server=Server(('127.0.0.1',port),None)
    try:app=App(path)
    except Exception:server.server_close();raise
    server.app=app
    print(f'MarketDesk local paper app: http://127.0.0.1:{port}',flush=True)
    if live:
        try:app.feed.start()
        except Exception as e:print('Live feed unavailable; configure the token in the app.',flush=True)
    app.power.start()
    app.auto.start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.power.stop();app.auto.stop();app.feed.stop();server.server_close()
