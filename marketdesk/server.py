from .working_universe import symbol_filter,status as working_status,enabled,scope_report,sync_instruments
from .history_rules import MIN_SESSIONS
from .scorecard import scorecard
import json,os,re,secrets,threading,time,mimetypes,hashlib
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs,unquote
from .storage import ROOT,DEFAULT_DB,connect,init,now,get_state,set_state
from .analytics import listing
from .paper import init_paper,trade,portfolios
from .feed import Feed,load_token
from .refresh import Refresh
from .autotrading import AutoTrading
from .quality import audit
from .power import SleepGuard
from .alerts import Alerts
from .maintenance import Maintenance
from .universe_exits import RequestedExits
from .market_caps import classifications,decorate_html,ClassificationRefresh
from .quant.runner import QuantRunner
from .signal_baskets.runner import SignalRunner

class App:
    def __init__(self,path,read_only=False):
        self.path=path
        if not read_only:
            init(path);init_paper(path)
            with connect(path) as db:
                if get_state(db,'working_universe') is None:set_state(db,'working_universe','nifty500')
            sync_instruments(path);audit(path)
        self.feed=None if read_only else Feed(path);self.refresh=None if read_only else Refresh(path)
        self.auto=None if read_only else AutoTrading(path,self.refresh)
        if not read_only:
            with connect(path) as db:
                if enabled(db):
                    cfg=get_state(db,'auto_config',{})
                    from .paper import PORTFOLIOS
                    for portfolio in PORTFOLIOS:cfg.setdefault(portfolio,{})['buy_universe']='nifty500'
                    set_state(db,'auto_config',cfg)
        self.power=None if read_only else SleepGuard()
        self.cap_refresh=None if read_only else ClassificationRefresh(path)
        self.requested_exits=None if read_only else RequestedExits(path)
        self.maintenance=None if read_only else Maintenance(path,self.refresh)
        self.quant=None if read_only else QuantRunner(path)
        self.signals=None if read_only else SignalRunner(path)
        self.alerts=None if read_only else Alerts(path,self.feed)
        self.csrf=secrets.token_urlsafe(32);self.cache=None;self.cached_at=0;self.cache_lock=threading.Lock()
    def stocks(self):
        with self.cache_lock:
            if self.cache is None or time.monotonic()-self.cached_at>5:
                self.cache=listing(self.path);self.cached_at=time.monotonic()
            return self.cache
    def health(self):
        with connect(self.path) as db:
            counts={table:db.execute('SELECT COUNT(*) FROM '+table+' WHERE '+symbol_filter(db)).fetchone()[0] for table in ('instruments','candles','fundamentals','quotes','issues')}
            stream=get_state(db,'stream',{});refresh=get_state(db,'refresh',{});quality=get_state(db,'quality_summary',{});quality['repair_queue']=get_state(db,'quality_repair_queue',{})
            report=get_state(db,'last_import',{});repair=get_state(db,'fundamental_repair',{})
            incomplete=db.execute("SELECT COUNT(*) FROM fundamentals WHERE "+symbol_filter(db)+" AND COALESCE(json_extract(details,'$.status'),'MISSING')!='COMPLETE'").fetchone()[0]
            # Banks, insurers and NBFCs report borrowings/leases/finance costs differently; those gaps are not actionable.
            from .history_rules import is_financial
            from .liability_bound import attach,bounded_debt_pass
            from .bank_inputs import not_applicable
            missing={};financial_gaps={};bounded=[];financial_incomplete=0
            records={r[0]:(json.loads(r[1]),is_financial(r[2],r[0])) for r in db.execute('SELECT f.symbol,f.details,i.sector FROM fundamentals f JOIN instruments i USING(symbol) WHERE f.'+symbol_filter(db))}
            attach(db,{k:v[0] for k,v in records.items()})
            for symbol,(details,financial) in records.items():
                # Only a line the verified filing framework does not carry is excused; other lender gaps still block buys.
                excused=financial and details.get('status')!='COMPLETE' and set(details.get('missing_fields') or [])<=not_applicable(details)
                target=financial_gaps if excused else missing
                if excused:financial_incomplete+=1
                if not excused and bounded_debt_pass(details):bounded.append(symbol);continue      # same pass as the Investor check
                for key in details.get('missing_fields',[]):target[key]=target.get(key,0)+1
            missing_records=counts['instruments']-counts['fundamentals']
            if missing_records:
                from .provider import FUNDAMENTAL_REQUIRED_FIELDS
                for field in (*FUNDAMENTAL_REQUIRED_FIELDS,'sector'):missing[field]=missing.get(field,0)+missing_records
            full={endpoint:db.execute("SELECT COUNT(*) FROM payloads WHERE "+symbol_filter(db)+" AND endpoint=? AND json_array_length(json_extract(payload,'$.data.full_statement'))>0",(endpoint,)).fetchone()[0] for endpoint in ('income_statement','balance_sheet','cash_flow')}
            failures=db.execute("SELECT COUNT(*) FROM payloads WHERE "+symbol_filter(db)+" AND (http_status!=200 OR error IS NOT NULL OR COALESCE(json_extract(payload,'$.status'),'')!='success')").fetchone()[0]
            latest=db.execute('SELECT MAX(ts) FROM candles WHERE '+symbol_filter(db)).fetchone()[0]
            recent=db.execute('SELECT COUNT(*) FROM (SELECT symbol FROM candles WHERE '+symbol_filter(db)+' GROUP BY symbol HAVING COUNT(*)>='+str(MIN_SESSIONS)+' AND COUNT(*)<200)').fetchone()[0]
            short=db.execute('SELECT COUNT(*) FROM (SELECT symbol FROM candles WHERE '+symbol_filter(db)+' GROUP BY symbol HAVING COUNT(*)<'+str(MIN_SESSIONS)+')').fetchone()[0]
            universe=working_status(db)
            if enabled(db):
                active={r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))}
                quality['repair_queue']['symbols']=[s for s in quality['repair_queue'].get('symbols',[]) if s in active]
                if refresh.get('working_universe')!='nifty500':refresh={'status':'ready','mode':'Nifty 500','total':len(active),'done':0}
                if repair.get('working_universe')!='nifty500':repair={}
                report={}
            close_check=get_state(db,'close_check',{});score=scorecard(db)
        return {'universe':universe,'fundamental_repair':repair,'sleep_guard':self.power.snapshot() if self.power else None,'alerts':self.alerts.snapshot() if self.alerts else None,'quality':quality,'close_check':close_check,'scorecard':score,'counts':counts,'stream':stream,'refresh':{k:v for k,v in refresh.items() if k!='completed_symbols'},'import':report,'token_saved':bool(load_token()),'complete_fundamentals':counts['fundamentals']-incomplete,'partial_fundamentals':incomplete+missing_records,'actionable_partial_fundamentals':incomplete+missing_records-financial_incomplete-len(bounded),'financial_incomplete':financial_incomplete,'bounded_debt':sorted(bounded),'financial_not_applicable_fields':financial_gaps,'missing_fields':missing,'detailed_statements':full,'failed_endpoints':failures,'latest_candle':latest,'short_histories':short,'recent_listings':recent,'mode':'local_paper_only','now':now(),'csrf':self.csrf}

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
            if url.path=='/api/stock-classifications':return self.send(classifications(app.path))
            if url.path=='/api/source-conflicts':
                file=ROOT/'data/source-conflicts/latest.json'
                report=json.loads(file.read_text()) if file.exists() else {'status':'not_started','results':[]}
                for row in report['results']:
                    if row.get('evidence'):row['evidence']={k:v for k,v in row['evidence'].items() if k!='annual'}
                with connect(app.path) as db:report=scope_report(db,report)
                return self.send(report)
            if url.path=='/api/fundamental-repair':
                file=ROOT/'data/fundamental-repair/latest.json'
                with connect(app.path) as db:report=scope_report(db,json.loads(file.read_text()) if file.exists() else {'status':'not_started','results':[]})
                return self.send(report)
            if url.path=='/api/stocks':return self.send({'stocks':app.stocks()})
            if url.path=='/api/quant-baskets':return self.send(app.quant.snapshot())
            if url.path=='/api/signal-baskets':
                from .signal_baskets.report import snapshot as signal_snapshot
                return self.send(signal_snapshot(app.path))
            if url.path=='/api/backtests':
                from .backtest_report import report as backtest_report
                return self.send(backtest_report(app.path))
            if url.path=='/api/paper':return self.send({'portfolios':portfolios(app.path),'automation':app.auto.snapshot()})
            if url.path=='/api/stock':
                symbol=q.get('symbol',[''])[0]
                with connect(app.path) as db:
                    i=db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db,include_holdings=True)+' AND symbol=?',(symbol,)).fetchone()
                    if not i:return self.send({'error':'Unknown stock'},404)
                    f=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
                    payloads=[dict(r) for r in db.execute('SELECT endpoint,http_status,error,observed_at,source_url,payload FROM payloads WHERE symbol=?',(symbol,))]
                    for p in payloads:p['payload']=json.loads(p['payload'])
                    return self.send({'instrument':dict(i),'candles':[dict(r) for r in db.execute('SELECT ts,open,high,low,close,volume FROM candles WHERE symbol=? ORDER BY ts',(symbol,))],
                        'quality':dict(db.execute('SELECT * FROM quality WHERE symbol=?',(symbol,)).fetchone() or {}),'fundamentals':json.loads(f['details']) if f else {},'payloads':payloads,'issues':[dict(r) for r in db.execute('SELECT * FROM issues WHERE symbol=?',(symbol,))]})
            name=unquote(url.path).lstrip('/') or 'index.html';root=(ROOT/'web').resolve();file=(root/name).resolve()
            if root not in file.parents or not file.is_file():return self.send({'error':'Not found'},404)
            with connect(app.path) as db:scoped=enabled(db)
            if scoped and name in ('replay.html','intraday-replay.html','trader-review.html','trader-comparison.html'):
                body=b'<!doctype html><html><head><title>MarketDesk research archive</title><link rel="stylesheet" href="/style.css"></head><body><main><a href="/">Back to MarketDesk</a><h1>Nifty 500 research</h1><p>The previous broad-universe experiment is archived locally. It is not a result for the current Nifty 500 universe.</p><p>Replacement buy rules remain inactive while they are developed and tested.</p></main></body></html>'
            else:body=file.read_bytes()
            if file.suffix=='.html':body=decorate_html(body)
            return self.send(body,content=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
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
            if path=='/api/signal-baskets/config':
                from .signal_baskets.config import update as update_signal_config
                return self.send(update_signal_config(app.path,payload))
            if path=='/api/watch':
                with connect(app.path) as db:
                    if payload.get('watched') and not db.execute('SELECT 1 FROM instruments WHERE '+symbol_filter(db)+' AND symbol=?',(payload.get('symbol'),)).fetchone():raise ValueError('Stock is outside the research universe.')
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
    app.cap_refresh.start()
    app.requested_exits.start()
    app.maintenance.start()
    app.auto.start()
    app.quant.start()
    app.signals.start()
    app.alerts.start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:app.alerts.stop();app.signals.stop();app.quant.stop();app.maintenance.stop();app.requested_exits.stop();app.cap_refresh.stop();app.power.stop();app.auto.stop();app.feed.stop();server.server_close()
