"""Restart-safe Nifty 500 maintenance, independent of whether baskets are enabled."""
import threading,json
from datetime import datetime,timedelta,timezone
from .storage import connect,get_state,set_state,now
from .working_universe import enabled,symbol_filter
from .quality import audit
IST=timezone(timedelta(hours=5,minutes=30))


class Maintenance:
    def __init__(self,path,refresh):self.path=path;self.refresh=refresh;self.stop_event=threading.Event()
    def tick(self,clock=None):
        clock=clock or datetime.now(timezone.utc);day=clock.astimezone(IST).date().isoformat()
        with connect(self.path) as db:
            if not enabled(db):return
            job=get_state(db,'refresh',{});state=get_state(db,'nifty500_maintenance',{})
            # Track manual jobs too, so a completed repair is not immediately repeated.
            if job.get('working_universe')=='nifty500' and job.get('started_at'):
                mode=job.get('mode')
                if mode in ('fundamentals','fundamental_repair') or (mode=='candles' and job.get('scope')=='all'):
                    state.setdefault('last_started',{})[mode]=job['started_at']
                    set_state(db,'nifty500_maintenance',state)
            if job.get('status')=='running':return
        if job.get('status') in ('interrupted','failed') and job.get('working_universe')=='nifty500':
            age=(clock-datetime.fromisoformat(job.get('finished_at') or job['started_at'])).total_seconds()
            if job['status']=='interrupted' or age>3600:
                self.refresh.start([],job['mode']);return
        audit(self.path,clock)
        last=state.get('last_started',{})
        def due(mode):
            try:return datetime.fromisoformat(last[mode]).astimezone(IST).date().isoformat()!=day
            except (ValueError,KeyError,TypeError):return True
        if clock.astimezone(IST).hour<6:return
        if due('candles'):
            self.refresh.start([],'candles');return
        if due('fundamentals'):
            with connect(self.path) as db:
                rows=db.execute('SELECT i.symbol,f.observed_at,f.details FROM instruments i LEFT JOIN fundamentals f USING(symbol) WHERE '+symbol_filter(db,'i.symbol')).fetchall()
            stale=[]
            for s,stamp,details in rows:
                try:old=not 0<=(clock-datetime.fromisoformat(stamp)).total_seconds()<7*86400
                except (ValueError,TypeError):old=True
                if old or not (json.loads(details or '{}').get('latest') or {}).get('period'):stale.append(s)
            if stale:self.refresh.start(stale,'fundamentals');return
        if due('fundamental_repair'):
            from .fundamental_repair import targets
            if targets(self.path):self.refresh.start([],'fundamental_repair')
    def start(self):threading.Thread(target=self.run,daemon=True).start()
    def run(self):
        while not self.stop_event.is_set():
            try:self.tick()
            except Exception as error:
                with connect(self.path) as db:
                    state=get_state(db,'nifty500_maintenance',{});state['last_error']={'at':now(),'reason':str(error)[:180]};set_state(db,'nifty500_maintenance',state)
            self.stop_event.wait(60)
    def stop(self):self.stop_event.set()
