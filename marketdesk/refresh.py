from .working_universe import symbol_filter,enabled,research_symbols
import threading,time,csv,json,shutil
from datetime import datetime,timedelta,timezone
from pathlib import Path
from .storage import connect,set_state,get_state,now,ROOT
from .feed import load_token
from .importer import import_exports
from . import provider
from .fallbacks import Fallbacks,apply_fallback
from .quality import audit

class Refresh:
    def __init__(self,path):
        self.path=path;self.lock=threading.Lock();self.fallbacks=Fallbacks()
        with connect(path) as db:
            previous=get_state(db,'refresh',{})
            if previous.get('status')=='running':
                previous['status']='interrupted';set_state(db,'refresh',previous)
                repair=get_state(db,'fundamental_repair',{})
                if repair.get('status')=='running':repair['status']='interrupted';set_state(db,'fundamental_repair',repair)
                queue=get_state(db,'quality_repair_queue',{})
                if queue.get('status')=='started':queue['status']='queued';set_state(db,'quality_repair_queue',queue)
    def start(self,symbols,mode='both'):
        if mode=='fundamental_repair':return self.start_fundamental_repair()
        if mode not in ('both','candles','fundamentals'):raise ValueError('Invalid refresh mode.')
        if not load_token() and mode=='candles':raise ValueError('An Upstox access token is required for candles.')
        if not self.lock.acquire(blocking=False):raise ValueError('A data refresh is already running.')
        with connect(self.path) as db:
            all_rows=[dict(r) for r in db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db,include_holdings=mode in ('candles','both'))+' ORDER BY symbol')]
        rows=[r for r in all_rows if not symbols or r['symbol'] in symbols]
        with connect(self.path) as db:
            failed={r[0] for r in db.execute("SELECT DISTINCT symbol FROM payloads WHERE http_status!=200 OR error IS NOT NULL")}
        rows.sort(key=lambda r:(r['symbol'] not in failed,r['symbol']))
        completed=[]
        with connect(self.path) as db:previous=get_state(db,'refresh',{})
        if not symbols and previous.get('scope')=='all' and previous.get('mode')==mode and previous.get('status') in ('failed','interrupted'):
            completed=[s for s in previous.get('completed_symbols',[]) if s in {r['symbol'] for r in all_rows}]
            rows=[r for r in rows if r['symbol'] not in completed]
        if not rows:self.lock.release();raise ValueError('No matching symbols.')
        self.state={'completed_symbols':completed,'scope':'all' if not symbols else 'selected','status':'running','total':len(rows)+len(completed),'done':len(completed),'errors':0,'started_at':now(),'mode':mode};self.save()
        threading.Thread(target=self.run,args=(rows,mode),daemon=True).start()
    def start_fundamental_repair(self):
        from .fundamental_repair import targets,run_repair
        if not self.lock.acquire(blocking=False):raise ValueError('A data refresh is already running.')
        try:
            with connect(self.path) as db:previous=get_state(db,'refresh',{})
            resume=previous.get('mode')=='fundamental_repair' and previous.get('status') in ('interrupted','failed')
            started=previous['started_at'] if resume else now()
            saved=ROOT/'data/fundamental-repair'/started.replace(':','-')/'before.json'
            rows=json.loads(saved.read_text()) if resume and saved.exists() else targets(self.path)
            with connect(self.path) as db:
                active={r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))}
            rows=[r for r in rows if r['symbol'] in active]
            # A changed scope starts a fresh checkpoint rather than carrying old totals.
            if resume and previous.get('working_universe')!=('nifty500' if enabled(db) else 'all'):
                resume=False;started=now();rows=targets(self.path)
            if not rows:raise ValueError('All annual inputs are already complete.')
            with connect(self.path) as db:
                if not resume:set_state(db,'refresh_before_fundamental_repair',previous)
                complete=db.execute("SELECT COUNT(*) FROM fundamentals WHERE "+symbol_filter(db)+" AND json_extract(details,'$.status')='COMPLETE'").fetchone()[0]
            self.state={'scope':'incomplete','status':'running','total':len(rows),'done':0,'errors':0,'started_at':started,
                        'mode':'fundamental_repair','stage':'NSE / BSE / company annual reports / Yahoo','complete_before':previous.get('complete_before',complete) if resume else complete}
            self.save()
            threading.Thread(target=run_repair,args=(self,rows),daemon=True).start()
        except Exception:
            self.lock.release();raise

    def save(self):
        with connect(self.path) as db:
            self.state['working_universe']='nifty500' if enabled(db) else 'all'
            set_state(db,'refresh',self.state)
    def run(self,rows,mode):
        root=ROOT/'data'/'imports'
        archive=ROOT/'data'/'refresh-history'/self.state['started_at'].replace(':','-')
        provider.UPSTOX_TOKEN=load_token();provider.headers['Authorization']='Bearer '+provider.UPSTOX_TOKEN
        provider.UPSTOX_AUTH_ERROR=None if provider.UPSTOX_TOKEN else 'Token missing';provider.FUNDAMENTAL_DIR=root/'RAW_FUNDAMENTALS'
        provider.RAW_JSON_DIR=root/'RAW_CANDLES'/'json';provider.RAW_CSV_DIR=root/'RAW_CANDLES'/'csv'
        try:
            for row in rows:
                symbol=row['symbol']
                with connect(self.path) as db:
                    research=research_symbols(db)
                    active={r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db,include_holdings=mode in ('candles','both')))}
                if symbol not in active:continue
                row_mode='candles' if research is not None and symbol not in research else mode
                self.state['current']=symbol;self.save()
                # Preserve the previous raw response before a network retry replaces it.
                old=root/'RAW_FUNDAMENTALS'/symbol
                if old.exists():shutil.copytree(old,archive/symbol,dirs_exist_ok=True)
                failed=False
                if not provider.UPSTOX_AUTH_ERROR and row_mode in ('both','fundamentals'):
                    result=provider.fetch_and_save_fundamental_jsons(symbol,row['instrument_key'],row)
                    failed=result['status'] in ('FAILED','AUTH_FAILED','INVALID_ISIN')
                if not provider.UPSTOX_AUTH_ERROR and row_mode in ('both','candles'):
                    with connect(self.path) as db:
                        latest=db.execute('SELECT MAX(ts) FROM candles WHERE symbol=?',(symbol,)).fetchone()[0]
                    today=datetime.now(timezone(timedelta(hours=5,minutes=30))).date()
                    # Daily endpoint only includes completed prior days. Current-session data comes from the stream.
                    end=today-timedelta(days=1)
                    start=datetime.fromisoformat(latest).date()-timedelta(days=7) if latest else end-timedelta(days=550)
                    with connect(self.path) as db:
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='quality'").fetchone():
                            quality=db.execute('SELECT report FROM quality WHERE symbol=?',(symbol,)).fetchone()
                            if quality and json.loads(quality[0]).get('price_reasons'):start=end-timedelta(days=550)
                    result=provider.fetch_historical_candles(row['instrument_key'],start,end)
                    if result.get('status')=='success':
                        # Merge incremental candles with existing exported history before atomically saving.
                        old={};file=provider.RAW_CSV_DIR/(provider.safe_symbol_file_name(symbol)+'.csv')
                        if file.exists():
                            with file.open(newline='') as f:
                                for x in list(csv.reader(f))[1:]:
                                    if x:old[x[0]]=x
                        for x in result['candles']:old[x[0]]=x
                        result['candles']=[old[k] for k in sorted(old,reverse=True)]
                        provider.save_raw_candle_exports(symbol,row['instrument_key'],row.get('sector'),result,'SUCCESS')
                    else:failed=True
                if provider.UPSTOX_AUTH_ERROR:
                    failed=True
                    self.state['upstox_warning']='Upstox access unavailable; NSE/Yahoo fundamentals continue. Renew the token to refresh candles.'
                import_exports(root,self.path,[symbol])
                if row_mode in ('both','fundamentals'):
                    with connect(self.path) as db:
                        entry=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
                    details=json.loads(entry[0]) if entry else provider.extract_fundamental_details({})
                    details.setdefault('status','PARTIAL' if details.get('annual') else 'FAILED')
                    if details.get('missing_fields'):
                        self.state['stage']='NSE / Yahoo fallback';self.save()
                        supplemental=self.fallbacks.fetch(symbol,row['isin'])
                        details=apply_fallback(details,supplemental)
                        with connect(self.path) as db:
                            db.execute('INSERT INTO fundamentals VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET details=excluded.details,observed_at=excluded.observed_at',(symbol,json.dumps(details,allow_nan=False),now()))
                self.state['stage']='Upstox'
                self.state['done']+=1;self.state['errors']+=int(failed);self.state['completed_symbols'].append(symbol);self.save()
            self.state.update(status='complete',finished_at=now())
        except Exception as e:self.state.update(status='failed',error=str(e)[:250],finished_at=now())
        finally:
            self.save()
            with connect(self.path) as db:
                queue=get_state(db,'quality_repair_queue',{})
                if mode=='candles' and set(queue.get('symbols',[]))=={r['symbol'] for r in rows}:
                    queue.update(status='attempted' if self.state['status']=='complete' else 'needs_retry',finished_at=now(),errors=self.state['errors']);set_state(db,'quality_repair_queue',queue)
            self.lock.release()
            audit(self.path)
