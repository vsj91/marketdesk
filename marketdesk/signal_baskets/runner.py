"""Background scheduler for the signal baskets: data collection and signal generation. Never places orders;
the 15-second auto-trading loop (signal_baskets.engine) executes what this module decides.

Every 15 seconds `tick` runs whichever jobs are due, each idempotent and checkpointed in `state`:
  news       poll NSE announcements (5 min, 07:00-18:00 IST weekdays; 30 min otherwise), store, dedupe,
             verify identity, read material PDFs, analyse, screen for both news baskets
  confirm    promote News Momentum watchlist items once their news session's daily candle is complete
  catalyst   re-score News Catalyst candidates once per new completed session
  ema        on each completed 5-minute boundary (+15 s publication grace) while NSE is NORMAL_OPEN:
             fetch candles for a liquidity-ranked universe, evaluate the newest completed candle, arm entries
  live       Vinay Intraday: on the same boundaries book volume on the live-feed 5-minute candles (live_bars)
             with one batch quote snapshot, then scan every stock in the Nifty 500
  retention  daily pruning of cached candles, routine news and fine-grained equity snapshots
Unique keys on news (source id) and signals (event key) make a retry or overlapping tick a no-op."""
import json,math,sys,threading,time,traceback
from datetime import date,datetime,timedelta,timezone
from urllib.parse import quote as urlquote
import requests
from ..storage import connect,get_state,set_state,now
from ..feed import load_token
from ..buy_universe import allowed_symbols
from ..history_rules import is_financial
from . import news as newslib,news_rules,ema_rules,vinay_rules,live_bars
from .engine import params,set_status,transition,daily_bars,benchmark,intraday_bars,session,local_hm,IST
from .names import NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION,VINAY_INTRADAY,STRATEGY_VERSIONS,NEW_INTRADAY_BASKETS,ORB_VOLUME_BREAKOUT,LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION
from .schema import init as init_schema

def clean(x):
    """JSON-safe copy: non-finite numbers become None (SQLite json_extract and the API reject Infinity/NaN)."""
    if isinstance(x,float) and not math.isfinite(x):return None
    if isinstance(x,dict):return {k:clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)):return [clean(v) for v in x]
    return x

STATUS_KEY='signal_runner_status'
MATERIAL_FETCH=('order_win','acquisition','capacity_expansion','debt_reduction','rating_upgrade','rating_downgrade','regulatory_approval','buyback')
MAX_ATTACHMENTS=8

def add_weekdays(day,n):
    d=date.fromisoformat(day)
    while n>0:
        d+=timedelta(days=1)
        if d.weekday()<5:n-=1
    return d.isoformat()

def news_session(published):
    """Trading date whose completed candle first reflects the news: same day before 15:30 IST, else the next weekday."""
    t=datetime.fromisoformat(published).astimezone(IST);d=t.date()
    if t.weekday()>=5 or t.strftime('%H:%M')>='15:30':
        d+=timedelta(days=1)
        while d.weekday()>=5:d+=timedelta(days=1)
    return d.isoformat()

class UpstoxCandles:
    """Read-only Upstox V3 candle reader (historical and current-day intraday), rate limited to 4 requests/second."""
    BASE='https://api.upstox.com/v3/historical-candle/'
    def __init__(self,min_interval=.25):self.min_interval=min_interval;self.last=0;self.lock=threading.Lock()
    def _get(self,url):
        token=load_token()
        if not token:raise ValueError('Upstox token missing')
        with self.lock:
            delay=self.min_interval-(time.monotonic()-self.last)
            if delay>0:time.sleep(delay)
            self.last=time.monotonic()
        r=requests.get(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(5,15))
        if r.status_code!=200:raise ValueError(f'Upstox candles HTTP {r.status_code}')
        j=r.json()
        if j.get('status')!='success' or not isinstance(j.get('data',{}).get('candles'),list):raise ValueError('Upstox candles: unexpected payload')
        return j['data']['candles']
    def intraday(self,key,minutes):return self._get(self.BASE+'intraday/'+urlquote(key,safe='')+f'/minutes/{minutes}')
    def history(self,key,minutes,start,end):return self._get(self.BASE+urlquote(key,safe='')+f'/minutes/{minutes}/{end}/{start}')

class UpstoxQuotes:
    """Read-only Upstox V3 batch quote snapshot: volume traded today, VWAP and previous close per stock."""
    URL='https://api.upstox.com/v3/market-quote/quotes'
    def snapshot(self,keys):
        """{symbol: instrument_key} (at most 250) -> {symbol: dict(volume, vwap, prev_close, last_price)}; identity checked."""
        token=load_token()
        if not token:raise ValueError('Upstox token missing')
        r=requests.get(self.URL,params={'instrument_key':','.join(keys.values())},headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(5,15))
        if r.status_code!=200:raise ValueError(f'Upstox quotes HTTP {r.status_code}')
        j=r.json()
        if j.get('status')!='success' or not isinstance(j.get('data'),dict):raise ValueError('Upstox quotes: unexpected payload')
        out={}
        for symbol,key in keys.items():
            q=j['data'].get('NSE_EQ:'+symbol) or {}
            if q.get('instrument_token')!=key:continue
            vol,vw,pc,lp=(q.get(k) for k in ('volume','average_price','prev_close_price','last_price'))
            if not all(isinstance(x,(int,float)) and math.isfinite(x) and x>=0 for x in (vol,vw,pc,lp)):continue
            depth=q.get('depth') or {};bids=depth.get('buy') or [];asks=depth.get('sell') or []
            bid=sum(float(x.get('quantity') or 0) for x in bids[:5]);ask=sum(float(x.get('quantity') or 0) for x in asks[:5])
            out[symbol]=dict(volume=float(vol),vwap=vw or None,prev_close=pc or None,last_price=lp,
                depth_ratio=bid/ask if ask>0 else None)
        return out

def valid_candles(rows,minutes,clock):
    """Validated, completed candles inside the 09:15-15:30 IST session. In-progress candles are dropped."""
    out=[]
    for c in rows:
        try:
            ts=datetime.fromisoformat(c[0]);o,h,l,cl,v=(float(x) for x in c[1:6])
        except (TypeError,ValueError,IndexError):continue
        if ts.tzinfo is None or not all(map(math.isfinite,(o,h,l,cl,v))) or min(o,h,l,cl)<=0 or v<0 or h<max(o,cl,l) or l>min(o,cl):continue
        local=ts.astimezone(IST)
        if not '09:15'<=local.strftime('%H:%M')<'15:30' or ts+timedelta(minutes=minutes)>clock:continue
        out.append((local.isoformat(),o,h,l,cl,v))
    return out

def market_cap(db,symbol,price):
    """AMFI six-month average market cap scaled by price against the average close of that window (as quant.data)."""
    amfi=get_state(db,'amfi_classification',{})
    inst=db.execute('SELECT isin FROM instruments WHERE symbol=?',(symbol,)).fetchone()
    rec=(amfi.get('records') or {}).get(inst['isin'] if inst else None) or {}
    cap=rec.get('average_market_cap_crore');period=amfi.get('period')
    if not (cap and period and price):return None
    start=(date.fromisoformat(period)-timedelta(days=182)).isoformat()
    row=db.execute('SELECT AVG(close),COUNT(*) FROM candles WHERE symbol=? AND substr(ts,1,10) BETWEEN ? AND ?',(symbol,start,period)).fetchone()
    return cap*price/row[0] if row[1]>=60 and row[0] else None

def outside_universe(db,portfolio,symbol,clock,status,reasons):
    """Reject a candidate the basket's buy universe can never buy (and the live feed does not price), instead of listing it."""
    try:allowed=allowed_symbols(db,portfolio,clock)
    except ValueError:return status,reasons          # membership not verified today: the buy guard still decides
    if allowed is None or symbol in allowed or status=='REJECTED':return status,reasons
    return 'REJECTED',['Outside the Nifty 500 buy universe']+list(reasons)

def catalyst_inputs(db,symbol,today,price,catalyst,p):
    """(profile, score, daily context, catalyst) for a symbol at `today` using only completed sessions."""
    from ..quant.data import _operating_cash
    f=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(symbol,)).fetchone()
    details=json.loads(f['details']) if f else {}
    sector=(db.execute('SELECT sector FROM instruments WHERE symbol=?',(symbol,)).fetchone() or [None])[0] or details.get('sector') or ''
    bars=daily_bars(db,symbol,today);ctx=news_rules.daily_context(bars,benchmark(db,today))
    price=price or (ctx['close'] if ctx else None)
    cash_flow=db.execute("SELECT payload FROM payloads WHERE symbol=? AND endpoint='cash_flow'",(symbol,)).fetchone()
    ocf=_operating_cash(cash_flow['payload'],((details.get('latest') or {}).get('period'))) if cash_flow else None
    profile=news_rules.fundamental_profile(details,is_financial(sector,symbol),price,market_cap(db,symbol,price),ocf)
    if catalyst is None:
        since=(date.fromisoformat(today)-timedelta(days=p['catalyst_lookback_days'])).isoformat()
        row=db.execute('''SELECT a.details FROM sb_news n JOIN sb_news_analysis a ON a.news_id=n.id WHERE n.symbol=? AND n.published_at>=? AND n.verification='verified'
            AND n.duplicate_of IS NULL AND json_extract(a.details,'$.catalyst_eligible')=1 AND json_extract(a.details,'$.direction')>0 ORDER BY a.impact_score DESC LIMIT 1''',(symbol,since)).fetchone()
        catalyst=json.loads(row['details']) if row else None
    result=news_rules.opportunity_score(catalyst,profile,ctx,p)
    return profile,result,ctx,catalyst

def upsert_signal(db,name,key,symbol,status,clock,valid_from=None,expires_at=None,news_id=None,score=None,explanation='',rejection=None,details=None,**levels):
    """Insert a decision once per (basket, event key); later calls update an open decision and log the status change."""
    row=db.execute('SELECT id,status FROM sb_signals WHERE portfolio=? AND signal_key=?',(name,key)).fetchone()
    isin=(db.execute('SELECT isin FROM instruments WHERE symbol=?',(symbol,)).fetchone() or [None])[0]
    payload=json.dumps(clean(details or {}),default=str)
    if row is None:
        cur=db.execute('''INSERT INTO sb_signals(portfolio,strategy_version,signal_key,symbol,isin,created_at,session,side,status,entry_trigger,limit_price,initial_stop,initial_target,
            risk_reward,quantity,score,explanation,valid_from,expires_at,rejection_reason,news_id,details,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (name,STRATEGY_VERSIONS[name],key,symbol,isin,clock.isoformat(),session(clock),levels.get('side','long'),status,levels.get('entry_trigger'),levels.get('limit_price'),
             levels.get('initial_stop'),levels.get('initial_target'),levels.get('risk_reward'),levels.get('quantity'),score,explanation,valid_from,expires_at,rejection,news_id,payload,clock.isoformat()))
        transition(db,name,symbol,cur.lastrowid,None,status,explanation or (rejection or ''),clock=clock,dedup=f'sig:{cur.lastrowid}:{status}')
        return cur.lastrowid
    if row['status'] in ('ENTRY_FILLED','EXPIRED','CANCELLED'):return row['id']
    db.execute('UPDATE sb_signals SET score=?,explanation=?,rejection_reason=?,details=?,valid_from=COALESCE(?,valid_from),expires_at=COALESCE(?,expires_at),updated_at=? WHERE id=?',
        (score,explanation,rejection,payload,valid_from,expires_at,clock.isoformat(),row['id']))
    set_status(db,row['id'],status,explanation or (rejection or ''),clock)
    return row['id']

class SignalRunner:
    def __init__(self,path,provider=None,candles=None,quotes=None):
        self.path=path;init_schema(path);self.provider=provider or newslib.NseAnnouncements();self.candles=candles or UpstoxCandles();self.quotes=quotes or UpstoxQuotes()
        self.lock=threading.Lock();self.stop_event=threading.Event();self.thread=None;self.universe=(None,[]);self.warm={}
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
    def stop(self):self.stop_event.set()
    def run(self):
        while not self.stop_event.is_set():
            self.tick();self.stop_event.wait(15)

    def status(self,**parts):
        with connect(self.path) as db:
            s=get_state(db,STATUS_KEY,{})
            for k,v in parts.items():s[k]={**s.get(k,{}),**v,'updated_at':now()}
            set_state(db,STATUS_KEY,s)

    def tick(self,clock=None):
        """Run due jobs. Never raises: a failing job records its error and the others still run."""
        if not self.lock.acquire(blocking=False):return
        try:
            clock=clock or datetime.now(timezone.utc)
            for job in (self.poll_news,self.confirm_momentum,self.refresh_catalysts,self.ema_cycle,self.live_cycle,self.retention):
                try:job(clock)
                except Exception as error:
                    traceback.print_exc(file=sys.stderr)
                    # Recording the error needs the database too; if that is what failed, the next tick retries.
                    try:self.status(**{job.__name__:dict(ok=False,error=f'{error.__class__.__name__}: {error}'[:300])})
                    except Exception as record_error:print('Signal basket status save failed ('+type(record_error).__name__+').',file=sys.stderr,flush=True)
        finally:self.lock.release()

    # ---- news -------------------------------------------------------------------------------------
    def poll_news(self,clock,force=False):
        local=clock.astimezone(IST);busy=local.weekday()<5 and '07:00'<=local.strftime('%H:%M')<'18:00'
        with connect(self.path) as db:
            checkpoint=get_state(db,'signal_news_checkpoint',{});cfgs=get_state(db,'auto_config',{})
        if not force and not any(cfgs.get(n,{}).get('enabled') for n in (NEWS_MOMENTUM,NEWS_CATALYST)):return
        last=checkpoint.get('last_attempt')
        if not force and last and (clock-datetime.fromisoformat(last)).total_seconds()<(300 if busy else 1800):return
        start=local.date()-timedelta(days=1 if checkpoint.get('last_success_day')!=local.date().isoformat() else 0)
        checkpoint['last_attempt']=clock.isoformat()
        try:rows=self.provider.fetch(start,local.date())
        except ValueError as error:
            checkpoint.update(ok=False,error=str(error))
            with connect(self.path) as db:set_state(db,'signal_news_checkpoint',checkpoint)
            self.status(poll_news=dict(ok=False,error=str(error),last_attempt=clock.isoformat()));return
        added=self.store(rows,clock)
        checkpoint.update(ok=True,error=None,last_success=clock.isoformat(),last_success_day=local.date().isoformat(),last_rows=len(rows),last_added=len(added))
        with connect(self.path) as db:set_state(db,'signal_news_checkpoint',checkpoint)
        analysed=self.analyse_new(added,clock)
        self.status(poll_news=dict(ok=True,error=None,last_success=clock.isoformat(),rows=len(rows),added=len(added),analysed=analysed))

    def store(self,rows,clock):
        """Insert unseen announcements; returns their ids. Identity is verified against the instrument master."""
        added=[]
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            inst={r['symbol']:r['isin'] for r in db.execute('SELECT symbol,isin FROM instruments')}
            for raw in rows:
                item=newslib.normalize(raw,clock)
                if not item:continue
                if db.execute('SELECT 1 FROM sb_news WHERE source=? AND source_id=?',(item['source'],item['source_id'])).fetchone():continue
                s=item['symbol']
                verification='verified' if s in inst and item['isin'] and inst[s]==item['isin'] else 'isin_mismatch' if s in inst and item['isin'] else 'missing_isin' if s in inst else 'outside_universe'
                since=(datetime.fromisoformat(item['published_at'])-timedelta(days=3)).isoformat()
                dup=db.execute('SELECT id FROM sb_news WHERE dedup_key=? AND published_at>=? AND duplicate_of IS NULL ORDER BY id LIMIT 1',(item['dedup_key'],since)).fetchone()
                cur=db.execute('''INSERT INTO sb_news(source,source_id,symbol,isin,company,subject,headline,url,industry,published_at,disseminated_at,retrieved_at,event_at,dedup_key,duplicate_of,verification)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(item['source'],item['source_id'],s,item['isin'],item['company'],item['subject'],item['headline'],item['url'],item['industry'],
                    item['published_at'],item['disseminated_at'],item['retrieved_at'],item['event_at'],item['dedup_key'],dup[0] if dup else None,verification))
                if verification=='verified' and not dup:added.append(cur.lastrowid)
        return added

    def analyse_new(self,ids,clock):
        count=0;fetched=0
        for news_id in ids:
            with connect(self.path) as db:item=dict(db.execute('SELECT * FROM sb_news WHERE id=?',(news_id,)).fetchone())
            category,_,_=newslib.classify(item['subject'],item['headline'])
            if category in ('routine','other'):
                with connect(self.path) as db:self.save_analysis(db,item,clock,None,category);continue
            inr,_=newslib.amounts(item['headline'])
            if category in MATERIAL_FETCH and not inr and fetched<MAX_ATTACHMENTS:
                text=self.provider.attachment_text(item['url']);fetched+=1
                if text:
                    item['attachment_text']=text
                    with connect(self.path) as db:db.execute('UPDATE sb_news SET attachment_text=? WHERE id=?',(text,news_id))
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE');self.save_analysis(db,item,clock);count+=1
        return count

    def save_analysis(self,db,item,clock,analysis=None,category=None):
        if category:
            analysis=dict(version=newslib.ANALYSIS_VERSION,category=category,label='Neutral',direction=0,impact_score=0.,confidence=0.,routine=True,
                verification=item['verification'],momentum_eligible=False,catalyst_eligible=False,governance=False,components={},missing=[],notes=['Routine filing: not analysed further'])
        else:
            # Completed sessions strictly before the news: news_session is the publication day before 15:30 IST, else the next weekday.
            published=item['published_at'];bars=daily_bars(db,item['symbol'],news_session(published))
            f=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(item['symbol'],)).fetchone()
            summary=(json.loads(f['details']).get('summary') or {}) if f else {}
            prior=[dict(json.loads(r['details']),published_at=r['published_at']) for r in db.execute('''SELECT a.details,n.published_at FROM sb_news n JOIN sb_news_analysis a ON a.news_id=n.id
                WHERE n.symbol=? AND n.published_at<? AND n.published_at>=? AND n.id!=?''',(item['symbol'],published,(datetime.fromisoformat(published)-timedelta(days=30)).isoformat(),item['id']))]
            q=db.execute('SELECT price,trade_time FROM quotes WHERE symbol=?',(item['symbol'],)).fetchone()
            price=q['price'] if q and q['trade_time']/1000>=datetime.fromisoformat(published).timestamp() else None
            revenue=summary.get('revenue') if isinstance(summary.get('revenue'),(int,float)) else None
            analysis=newslib.analyse(item,revenue=revenue,revenue_period=summary.get('revenue_period'),prior=prior,bars=bars,price=price,now=clock,verification=item['verification'])
        db.execute('INSERT OR REPLACE INTO sb_news_analysis VALUES(?,?,?,?,?,?,?,?)',(item['id'],analysis['version'],clock.isoformat(),analysis['category'],analysis['label'],
            analysis['impact_score'],analysis['confidence'],json.dumps(clean(analysis),default=str)))
        if not category:self.decide(db,item,analysis,clock)

    def decide(self,db,item,a,clock):
        """Initial decisions for both news baskets. Only analysed, non-routine filings get a decision row."""
        cfgs=get_state(db,'auto_config',{});sess=news_session(item['published_at'])
        headline=f"{item['subject']}: {item['headline'][:160]}"
        if cfgs.get(NEWS_MOMENTUM,{}).get('enabled') and (a['direction']!=0 or a['momentum_eligible']):
            p=params(NEWS_MOMENTUM,cfgs.get(NEWS_MOMENTUM));status,reasons=outside_universe(db,NEWS_MOMENTUM,item['symbol'],clock,*news_rules.momentum_screen(a,p))
            upsert_signal(db,NEWS_MOMENTUM,f"news:{item['id']}",item['symbol'],status,clock,news_id=item['id'],score=a['impact_score'],
                explanation=f"{a['label']} {a['category'].replace('_',' ')} (impact {a['impact_score']:.0f}, confidence {a['confidence']:.2f}). {headline}",
                rejection='; '.join(reasons) if status=='REJECTED' else None,details=dict(analysis=a,news_session=sess,reasons=reasons))
        if cfgs.get(NEWS_CATALYST,{}).get('enabled') and a['catalyst_eligible'] and a['direction']>0:
            self.catalyst_decision(db,item['symbol'],dict(a,news_id=item['id'],headline=headline),add_weekdays(sess,1),clock)

    def catalyst_decision(self,db,symbol,catalyst,valid_from,clock):
        cfgs=get_state(db,'auto_config',{});p=params(NEWS_CATALYST,cfgs.get(NEWS_CATALYST));today=session(clock)
        profile,result,ctx,catalyst=catalyst_inputs(db,symbol,today,None,catalyst,p)
        status,reasons=outside_universe(db,NEWS_CATALYST,symbol,clock,*news_rules.catalyst_decision(catalyst,profile,result,ctx,p))
        upsert_signal(db,NEWS_CATALYST,f"catalyst:{catalyst['news_id']}",symbol,status,clock,valid_from=valid_from,
            expires_at=add_weekdays(valid_from,p['signal_expiry_sessions']) if status=='BUY_CANDIDATE' else None,news_id=catalyst['news_id'],score=result['score'],
            explanation=f"Opportunity score {result['score']:.0f} (coverage {result['coverage']*100:.0f}%); catalyst: {catalyst.get('headline','')}",
            rejection='; '.join(reasons) if status=='REJECTED' else None,details=dict(score=result,profile={k:v for k,v in profile.items() if k!='financial'},catalyst=catalyst,reasons=reasons,
                context={k:ctx[k] for k in ('close','dma200','turnover20_cr','vol60')} if ctx else None))

    # ---- confirmation and refresh ----------------------------------------------------------------
    def confirm_momentum(self,clock):
        today=session(clock)
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config',{}).get(NEWS_MOMENTUM,{});p=params(NEWS_MOMENTUM,cfg)
            rows=[dict(r) for r in db.execute("SELECT * FROM sb_signals WHERE portfolio=? AND status='WATCHLIST'",(NEWS_MOMENTUM,))]
        for s in rows:
            d=json.loads(s['details']);sess=d['news_session']
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                done=db.execute('SELECT MIN(substr(ts,1,10)) FROM candles WHERE symbol=? AND substr(ts,1,10)>=? AND substr(ts,1,10)<?',(s['symbol'],sess,today)).fetchone()[0]
                if not done:
                    if (date.fromisoformat(today)-date.fromisoformat(sess)).days>5:set_status(db,s['id'],'EXPIRED','No completed news session candle within 5 days',clock)
                    continue
                bars=daily_bars(db,s['symbol'],(date.fromisoformat(done)+timedelta(days=1)).isoformat())   # through the news session
                bench=[r[0] for r in db.execute('SELECT close FROM claude_benchmark WHERE day<=? ORDER BY day',(done,))]
                ctx=news_rules.daily_context(bars,bench)
                sector=(db.execute('SELECT sector FROM instruments WHERE symbol=?',(s['symbol'],)).fetchone() or [None])[0]
                held=db.execute('SELECT COUNT(*) FROM positions p JOIN instruments i USING(symbol) WHERE p.portfolio=? AND p.quantity>0 AND COALESCE(i.sector,\'\')=COALESCE(?,\'\')',(NEWS_MOMENTUM,sector)).fetchone()[0]
                status,reasons,plan=news_rules.momentum_confirm(d['analysis'],ctx,bars,p,held)
                d.update(confirmation_session=done,plan=plan,confirmation_reasons=reasons,context={k:ctx[k] for k in ('close','ema20','ema50','atr','rel_volume','relative_strength','regime_ok')} if ctx else None)
                valid=(date.fromisoformat(done)+timedelta(days=1)).isoformat()
                db.execute('UPDATE sb_signals SET details=?,valid_from=?,expires_at=?,initial_stop=?,initial_target=?,risk_reward=?,entry_trigger=? WHERE id=?',
                    (json.dumps(d,default=str),valid,add_weekdays(done,p['signal_expiry_sessions']),plan and plan['stop'],plan and plan['target'],plan and plan['risk_reward'],plan and plan['reference'],s['id']))
                set_status(db,s['id'],status,('Confirmed on '+done+': '+(f"reaction {plan['reaction_pct']:+.1f}%, volume {plan['rel_volume']:.1f}x, RR {plan['risk_reward']:.2f}" if plan else '')) if status=='BUY_CANDIDATE' else 'Not confirmed: '+'; '.join(reasons),clock,
                    rejection_reason='; '.join(reasons) if status=='REJECTED' else None)

    def refresh_catalysts(self,clock):
        today=session(clock)
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config',{}).get(NEWS_CATALYST,{})
            if not cfg.get('enabled'):return
            latest=db.execute('SELECT MAX(substr(ts,1,10)) FROM candles WHERE substr(ts,1,10)<?',(today,)).fetchone()[0]
            if not latest or get_state(db,'signal_catalyst_refreshed')==latest:return
            p=params(NEWS_CATALYST,cfg);since=(date.fromisoformat(today)-timedelta(days=p['catalyst_lookback_days'])).isoformat()
            rows=db.execute('''SELECT s.symbol,s.news_id,s.valid_from,a.details,n.subject,n.headline FROM sb_signals s JOIN sb_news n ON n.id=s.news_id JOIN sb_news_analysis a ON a.news_id=n.id
                WHERE s.portfolio=? AND s.status IN ('WATCHLIST','BUY_CANDIDATE','REJECTED') AND n.published_at>=?''',(NEWS_CATALYST,since)).fetchall()
        for r in rows:
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                a=json.loads(r['details']);a.update(news_id=r['news_id'],headline=f"{r['subject']}: {r['headline'][:160]}")
                self.catalyst_decision(db,r['symbol'],a,max(r['valid_from'] or today,today),clock)
        with connect(self.path) as db:set_state(db,'signal_catalyst_refreshed',latest)
        self.status(refresh_catalysts=dict(ok=True,error=None,session=latest,reviewed=len(rows)))

    # ---- EMA data and signals --------------------------------------------------------------------
    def ema_universe(self,db,clock,p,name=EMA_PRICE_ACTION):
        day=session(clock)
        if self.universe[0]==(day,p['universe_size'],name):return self.universe[1]
        allowed=allowed_symbols(db,name,clock)
        bad={r[0] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0")}
        ranked=[]
        for r in db.execute('''SELECT symbol,AVG(close*volume) t,MAX(ts) last,(SELECT close FROM candles c2 WHERE c2.symbol=c.symbol ORDER BY ts DESC LIMIT 1) px
            FROM (SELECT * FROM candles WHERE substr(ts,1,10)<? AND substr(ts,1,10)>=?) c GROUP BY symbol''',(day,(date.fromisoformat(day)-timedelta(days=35)).isoformat())):
            if (allowed is None or r['symbol'] in allowed) and r['symbol'] not in bad and r['px'] and r['px']>=p['min_price'] and r['t']>=p['min_turnover_cr']*1e7:ranked.append((r['t'],r['symbol']))
        out=[s for _,s in sorted(ranked,reverse=True)[:p['universe_size']]]
        self.universe=((day,p['universe_size'],name),out);return out

    def ema_cycle(self,clock):
        """EMA basket: download candles for its liquidity-ranked universe on each completed boundary, then signals."""
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config',{}).get(EMA_PRICE_ACTION,{});stream=get_state(db,'stream',{})
            checkpoint=get_state(db,'signal_ema_checkpoint',{})
        if not cfg.get('enabled'):return
        if stream.get('status')!='connected' or stream.get('market_status',{}).get('NSE_EQ')!='NORMAL_OPEN':return
        p=params(EMA_PRICE_ACTION,cfg);tf=p['timeframe_minutes'];local=clock.astimezone(IST)
        boundary=local.replace(minute=local.minute-local.minute%tf,second=0,microsecond=0)
        if (local-boundary).total_seconds()<15 or checkpoint.get('boundary',' ')>=boundary.isoformat():return
        with connect(self.path) as db:
            universe=self.ema_universe(db,clock,p)
            extra={r[0] for r in db.execute("SELECT symbol FROM sb_positions WHERE portfolio=? UNION SELECT symbol FROM sb_signals WHERE portfolio=? AND status='ENTRY_PENDING'",(EMA_PRICE_ACTION,EMA_PRICE_ACTION))}
            keys=dict(db.execute('SELECT symbol,instrument_key FROM instruments'))
        symbols=list(dict.fromkeys([*universe,*sorted(extra)]))
        today=session(clock);errors=[];stored=0
        for s in symbols:
            if s not in keys:continue
            try:
                rows=[]
                if self.warm.get((s,tf))!=today:
                    start=(local.date()-timedelta(days=10)).isoformat();end=(local.date()-timedelta(days=1)).isoformat()
                    rows+=valid_candles(self.candles.history(keys[s],tf,start,end),tf,clock);self.warm[(s,tf)]=today
                rows+=valid_candles(self.candles.intraday(keys[s],tf),tf,clock)
            except (ValueError,requests.RequestException) as error:errors.append(f'{s}: {error}');continue
            with connect(self.path) as db:
                db.executemany('INSERT OR REPLACE INTO sb_intraday VALUES(?,?,?,?,?,?,?,?)',[(s,tf,*r) for r in rows]);stored+=len(rows)
        evaluated=self.ema_signals(symbols,clock,p)
        with connect(self.path) as db:set_state(db,'signal_ema_checkpoint',dict(boundary=boundary.isoformat(),symbols=len(symbols),errors=len(errors),
            last_success=clock.isoformat() if len(errors)<len(symbols)/2 else checkpoint.get('last_success')))
        self.status(ema_cycle=dict(ok=len(errors)<len(symbols)/2,error='; '.join(errors[:3]) or None,boundary=boundary.isoformat(),universe=len(symbols),candles_stored=stored,crossovers=evaluated))

    def live_universe(self,db,clock,p):
        """Every buyable stock for Vinay Intraday (the verified Nifty 500 when that universe is on), priced >= min_price."""
        allowed=allowed_symbols(db,VINAY_INTRADAY,clock)
        bad={r[0] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0")}
        rows=db.execute('SELECT i.symbol,i.instrument_key,q.price FROM instruments i JOIN quotes q USING(symbol)').fetchall()
        return {r['symbol']:r['instrument_key'] for r in rows if (allowed is None or r['symbol'] in allowed) and r['symbol'] not in bad and r['price']>=p['min_price']}

    def live_cycle(self,clock):
        """Vinay Intraday: on each completed 5-minute boundary book volume on the live-feed candles with one batch quote
        snapshot (up to 250 stocks a request), then scan every stock in the universe."""
        with connect(self.path) as db:
            allcfg=get_state(db,'auto_config',{});cfg=allcfg.get(VINAY_INTRADAY,{})
            active_new=[n for n in NEW_INTRADAY_BASKETS if allcfg.get(n,{}).get('enabled')]
            stream=get_state(db,'stream',{})
            checkpoint=get_state(db,'signal_live_checkpoint',{})
        if not cfg.get('enabled') and not active_new:return
        if stream.get('status')!='connected' or stream.get('market_status',{}).get('NSE_EQ')!='NORMAL_OPEN':return
        p=params(VINAY_INTRADAY,cfg);tf=5;local=clock.astimezone(IST)
        boundary=local.replace(minute=local.minute-local.minute%tf,second=0,microsecond=0)
        if (local-boundary).total_seconds()<15 or checkpoint.get('boundary',' ')>=boundary.isoformat():return
        with connect(self.path) as db:
            keys=self.live_universe(db,clock,p)
            for r in db.execute("SELECT p.symbol,i.instrument_key FROM sb_positions p JOIN instruments i USING(symbol) WHERE p.portfolio=?",(VINAY_INTRADAY,)):keys[r[0]]=r[1]
        snapshot={};errors=[];names=sorted(keys)
        for i in range(0,len(names),250):
            try:snapshot.update(self.quotes.snapshot({s:keys[s] for s in names[i:i+250]}))
            except (ValueError,requests.RequestException) as error:errors.append(str(error)[:120])
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE');booked=live_bars.finalize(db,snapshot,boundary,tf)
        prev={s:q['prev_close'] for s,q in snapshot.items() if q.get('prev_close')}
        leaders=self.vinay_signals(sorted(snapshot),clock,p,prev) if cfg.get('enabled') else dict(scanned=0,candidates=0,near_misses={})
        new_signals={}
        for name in active_new:
            basket_cfg=allcfg.get(name,{})
            new_signals[name]=self.experimental_signals(name,sorted(snapshot),clock,params(name,basket_cfg),snapshot)
        ok=bool(snapshot) and not errors
        with connect(self.path) as db:set_state(db,'signal_live_checkpoint',dict(boundary=boundary.isoformat(),symbols=len(snapshot),errors=len(errors),
            last_success=clock.isoformat() if ok else checkpoint.get('last_success')))
        self.status(vinay_scan=dict(leaders,ok=ok,error='; '.join(errors[:2]) or None,boundary=boundary.isoformat(),universe=len(keys),candles_booked=booked),**{
            f'{name}_scan':dict(result,ok=ok,error='; '.join(errors[:2]) or None,boundary=boundary.isoformat(),universe=len(keys),candles_booked=booked)
            for name,result in new_signals.items()})

    def experimental_signals(self,name,symbols,clock,p,snapshot):
        """Evaluate isolated experimental baskets on completed live-feed bars."""
        from . import intraday_rules
        from .names import LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION
        today=session(clock);hm=local_hm(clock);found=scanned=0;misses={}
        for symbol in symbols:
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE');bars=live_bars.bars(db,symbol,clock,p['timeframe_minutes'])
                if not bars or bars[-1]['ts'][:10]!=today:continue
                daily_ema=None
                if name==ORDER_FLOW_REVERSAL:
                    from .indicators import ema_series
                    prior=daily_bars(db,symbol,today,limit=120);series=ema_series([b['close'] for b in prior],50)
                    daily_ema=series[-1] if series else None
                ratio=(snapshot.get(symbol) or {}).get('depth_ratio') if name==ORDER_FLOW_REVERSAL else None
                ev=intraday_rules.evaluate(name,bars,p,depth_ratio=ratio,daily_ema50=daily_ema)
                if not ev:continue
                scanned+=1
                if ev['status']!='BUY_CANDIDATE':
                    for reason in ev['reasons']:
                        key='depth unavailable/weak' if 'depth' in reason.lower() else 'setup not confirmed'
                        misses[key]=misses.get(key,0)+1
                    continue
                if not p['entry_start']<=hm<p['entry_cutoff']:continue
                if db.execute("SELECT 1 FROM sb_signals WHERE portfolio=? AND symbol=? AND session=? AND status IN ('BUY_CANDIDATE','ENTRY_FILLED')",(name,symbol,today)).fetchone():continue
                key=f"{symbol}:{ev['trigger_ts']}:{p['timeframe_minutes']}";end=datetime.fromisoformat(ev['trigger_ts'])+timedelta(minutes=p['timeframe_minutes']*2)
                why=f"{name.replace('_',' ').title()} setup; RVOL {ev.get('rvol') or 0:.1f}x; stop {ev['stop_pct']:.2f}%; target {p['target_r']:g}R"
                if name==ORB_VOLUME_BREAKOUT:why+=f"; opening-range high ₹{ev['opening_range_high']:.2f}; above VWAP"
                elif name==LIQUIDITY_SWEEP:why+=f"; swept/reclaimed ₹{ev['level']:.2f}"
                elif name==ORDER_FLOW_REVERSAL:why+=f"; top-five bid/ask {ev['depth_ratio']:.1f}:1; above daily EMA50"
                elif name==VOLATILITY_COMPRESSION:why+=f"; BB width {ev['bb_width_pct']:.2f}% compressed and expanded above upper band"
                upsert_signal(db,name,key,symbol,'BUY_CANDIDATE',clock,valid_from=today,expires_at=end.astimezone(timezone.utc).isoformat(),score=ev['score'],
                    explanation=why,details=ev,initial_stop=ev['initial_stop'],initial_target=ev['initial_target'],risk_reward=ev['risk_reward'])
                found+=1
        return dict(scanned=scanned,candidates=found,near_misses=misses)

    def vinay_signals(self,symbols,clock,p,prev_close=None):
        """Rank the universe on the newest completed live candle. Only candidates are stored (one open per stock);
        near misses are summarised in the runner status so the screen can say why nothing was bought."""
        tf=p['timeframe_minutes'];today=session(clock);hm=local_hm(clock);found=0;scanned=0;misses={};prev_close=prev_close or {}
        window=p['entry_start']<=hm<p['entry_cutoff']
        for s in symbols:
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                bars=live_bars.bars(db,s,clock,tf)
                if not bars or bars[-1]['ts'][:10]!=today:continue
                pc=prev_close.get(s)
                if pc is None:
                    q=db.execute('SELECT previous_close FROM quotes WHERE symbol=?',(s,)).fetchone();pc=q[0] if q else None
                ev=vinay_rules.evaluate(bars,p,pc)
                if not ev:continue
                scanned+=1
                if ev['status']!='BUY_CANDIDATE':
                    for r in ev['reasons']:k=vinay_rules.miss_label(r);misses[k]=misses.get(k,0)+1
                    continue
                if not window:misses['outside the entry window']=misses.get('outside the entry window',0)+1;continue
                if db.execute("SELECT 1 FROM sb_signals WHERE portfolio=? AND symbol=? AND session=? AND status IN ('BUY_CANDIDATE','ENTRY_FILLED')",(VINAY_INTRADAY,s,today)).fetchone():continue
                key=f"{s}:{ev['trigger_ts']}:{tf}"
                end=datetime.fromisoformat(ev['trigger_ts'])+timedelta(minutes=tf*(1+p['candidate_expiry_candles']))
                found+=1
                upsert_signal(db,VINAY_INTRADAY,key,s,'BUY_CANDIDATE',clock,valid_from=today,expires_at=end.astimezone(timezone.utc).isoformat(),score=ev['score'],
                    explanation=(f"Opened {ev['gap_pct']:+.2f}%, now up {ev['day_change_pct']:+.2f}%"+(f" on {ev['rvol']:.1f}x usual volume" if ev['rvol'] else '')
                        +f"; close {ev['close']:.2f} above opening range high {ev['or_high']:.2f} and VWAP {ev['vwap']:.2f} (+{ev['vwap_extension_pct']:.2f}%); stop {ev['stop_pct']:.2f}%"),details=ev)
        return dict(scanned=scanned,candidates=found,near_misses=dict(sorted(misses.items(),key=lambda x:-x[1])[:6]))

    def ema_signals(self,symbols,clock,p):
        found=0;tf=p['timeframe_minutes'];today=session(clock);hm=local_hm(clock)
        for s in symbols:
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                bars=intraday_bars(db,s,tf,clock)
                if not bars or bars[-1]['ts'][:10]!=today:continue
                ev=ema_rules.evaluate(bars,p)
                if not ev:continue
                found+=1;end=datetime.fromisoformat(ev['trigger_ts'])+timedelta(minutes=tf)
                status=ev['status']
                if status=='SIGNAL_DETECTED' and not p['entry_start']<=hm<p['entry_cutoff']:
                    status='REJECTED';ev['reasons']=[f"Outside the entry window {p['entry_start']}-{p['entry_cutoff']} IST"]
                key=f"{s}:{ev['trigger_ts']}:{tf}"
                if db.execute('SELECT 1 FROM sb_signals WHERE portfolio=? AND signal_key=?',(EMA_PRICE_ACTION,key)).fetchone():continue
                explanation=(f"EMA9 {ev['ema9']:.2f} crossed above EMA21 {ev['ema21']:.2f}; close {ev['close']:.2f} vs EMA200 {ev['ema200']:.2f}; RSI {ev['rsi14'] or 0:.1f}; ATR {ev['atr14'] or 0:.2f} ({ev['atr_pct'] or 0:.2f}%)"
                    +(f"; RR {ev['risk_reward']:.2f}" if ev.get('risk_reward') else ''))
                sid=upsert_signal(db,EMA_PRICE_ACTION,key,s,'SIGNAL_DETECTED' if status=='SIGNAL_DETECTED' else 'REJECTED',clock,valid_from=today,
                    expires_at=(end+timedelta(minutes=tf*p['signal_expiry_candles'])).astimezone(timezone.utc).isoformat(),score=ev.get('risk_reward'),explanation=explanation,
                    rejection='; '.join(ev['reasons']) or None,details=ev,entry_trigger=ev.get('entry_trigger'),limit_price=ev.get('limit_price'),initial_stop=ev.get('initial_stop'),
                    initial_target=ev.get('initial_target'),risk_reward=ev.get('risk_reward'))
                if status=='SIGNAL_DETECTED':set_status(db,sid,'ENTRY_PENDING',f"Stop-limit armed: trigger {ev['entry_trigger']:.2f}, limit {ev['limit_price']:.2f}, SL {ev['initial_stop']:.2f}, TP {ev['initial_target']:.2f}",clock)
        return found

    # ---- retention -------------------------------------------------------------------------------
    def retention(self,clock):
        today=session(clock)
        with connect(self.path) as db:
            if get_state(db,'signal_retention_day')==today:return
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sb_intraday WHERE ts<?',((date.fromisoformat(today)-timedelta(days=15)).isoformat(),))
            db.execute('DELETE FROM sb_live_bars WHERE ts<?',((date.fromisoformat(today)-timedelta(days=15)).isoformat(),))
            old=(date.fromisoformat(today)-timedelta(days=45)).isoformat()
            ids=[r[0] for r in db.execute('''SELECT n.id FROM sb_news n LEFT JOIN sb_news_analysis a ON a.news_id=n.id WHERE n.published_at<? AND
                (a.category IS NULL OR a.category IN ('routine','other')) AND n.id NOT IN (SELECT news_id FROM sb_signals WHERE news_id IS NOT NULL)''',(old,))]
            db.executemany('DELETE FROM sb_news_analysis WHERE news_id=?',[(i,) for i in ids]);db.executemany('DELETE FROM sb_news WHERE id=?',[(i,) for i in ids])
            cutoff=(date.fromisoformat(today)-timedelta(days=30)).isoformat()
            db.execute('''DELETE FROM sb_equity WHERE ts<? AND rowid NOT IN (SELECT MAX(rowid) FROM sb_equity WHERE ts<? GROUP BY portfolio,substr(ts,1,10))''',(cutoff,cutoff))
            set_state(db,'signal_retention_day',today)
        self.status(retention=dict(ok=True,error=None,day=today,news_pruned=len(ids)))
