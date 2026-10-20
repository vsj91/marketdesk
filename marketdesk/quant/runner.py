"""Background runner: keeps the quant paper baskets current inside the local app.

Every few minutes it checks for completed sessions the ledger has not processed yet and runs
them in order (catching up if the app was closed). A session counts as completed only once
it is before today's IST date and no candle refresh is running, so a half-refreshed day is
never traded. On first start the five default baskets are created on the latest completed
session: the track record is forward-only, never backfilled. Paper only, own ledger file."""
import sys,threading,time,traceback
from datetime import datetime,timedelta,timezone
from pathlib import Path
from ..storage import connect,get_state,now
from .engine import BasketEngine
from .data import load_market,sessions,snapshot
from .names import LIVE_BASKETS,TITLES,LIVE_CAPITAL

IST=timezone(timedelta(hours=5,minutes=30))
DEFAULT_BASKETS={'smart_beta':'multifactor','risk_parity':'risk_parity','stat_arb':'stat_arb','trend':'trend','regime':'regime'}
# Retired baskets keep their ledger history but never trade again (and are not created on a fresh ledger).
RETIRED={'trend':'Duplicates the Trader basket (breakout entries with ATR trailing stops).'}
CAPITAL=LIVE_CAPITAL
INTERVAL=300

def ledger_path(db_path):return Path(db_path).parent/'quant_baskets.sqlite3'

class QuantRunner:
    def __init__(self,path,ledger=None,capital=CAPITAL):
        self.path=path;self.ledger=Path(ledger) if ledger else ledger_path(path);self.capital=capital
        self.engine=BasketEngine(self.ledger,live_db=path);self.stop_event=threading.Event();self.thread=None;self.lock=threading.Lock()
        from ..claude_trader import ClaudeTrader
        self.index=ClaudeTrader(path)
        self.status=dict(state='starting',message='Waiting for the first check.',checked_at=None,last_session=None)

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def run(self):
        while not self.stop_event.is_set():
            self.tick();self.stop_event.wait(INTERVAL)

    def tick(self,clock=None):
        """Process every completed, unprocessed session. Never raises; failures are reported in status."""
        if not self.lock.acquire(blocking=False):return self.status
        try:
            clock=clock or datetime.now(timezone.utc);today=clock.astimezone(IST).date().isoformat()
            with connect(self.path) as db:job=get_state(db,'refresh',{})
            # An interrupted refresh is resumed by maintenance; until then the day is only half refreshed.
            if job.get('status') in ('running','interrupted') and job.get('mode') in ('candles','both'):
                return self._set('waiting','Candle refresh running; the daily run waits for it to finish.')
            self.index.refresh_benchmark(clock)      # the trader only fetches the index close during sessions
            market=load_market(self.path);done=[d for d in sessions(market) if d<today]
            if not done:return self._set('data_wait','No completed sessions in the database yet.')
            if not self.engine.names():
                for name,strategy in DEFAULT_BASKETS.items():
                    if name not in RETIRED:self.engine.create(name,strategy,self.capital,day=done[-1])
            last=self._last_run()
            for name,reason in RETIRED.items():
                if name in self.engine.names():self.engine.retire(name,last or done[-1],reason)
            # Live baskets take cash and holdings from the paper portfolio of the same name and fill during market hours.
            for name in LIVE_BASKETS:
                if name in self.engine.names():self.engine.go_live(name)
            todo=[d for d in done if (last is None or d>last) and d>=self._start()]
            for day in todo:self.engine.run_day(snapshot(market,day))
            if last and not todo:self.engine.remark(snapshot(market,last))
            return self._set('ok',f'Up to date through {done[-1]}.' if not todo else f'Processed {len(todo)} session(s) through {todo[-1]}.',last_session=todo[-1] if todo else last)
        except Exception as error:
            traceback.print_exc(file=sys.stderr)
            return self._set('error',f'Daily run failed: {error.__class__.__name__}. Ledger unchanged for the failed session; retrying in {INTERVAL//60} minutes.')
        finally:self.lock.release()

    def _set(self,state,message,**extra):
        self.status={**self.status,'state':state,'message':message,'checked_at':now(),**extra};return self.status

    def _last_run(self):
        with connect(self.ledger) as db:return db.execute('SELECT MAX(day) FROM qb_runs').fetchone()[0]
    def _start(self):
        with connect(self.ledger) as db:return db.execute('SELECT MIN(created_day) FROM qb_baskets').fetchone()[0] or ''

    def snapshot(self,log_limit=40):
        baskets=[]
        for name in self.engine.names():
            p=self.engine.payload(name,log_limit);p['title']=TITLES.get(name,name);baskets.append(p)
        return dict(status=self.status,baskets=baskets,ledger=str(self.ledger.name),paper_only=True)
