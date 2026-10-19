from .working_universe import symbol_filter
"""Original Claude V2, opt-in for the local paper Trader. No broker order APIs.

Indicators use completed candles only. The optional live-breakout entry uses
fresh prices above the latest 20 completed highs; default entries use a completed
close breakout. Observed depth provides fills, never retrospective prices.
"""
import collections
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import requests
from .history_rules import MIN_SESSIONS,long_window,effective_rows
from .storage import connect, get_state, set_state
from .execution import charges
from .feed import load_token
from .paper import trade
from .quote_health import fresh,stale_message,refresh_held_quotes
from .buy_universe import allowed_symbols,require_buy_universe

IST = timezone(timedelta(hours=5, minutes=30))
TRADE_RISK = .01   # fraction of equity risked per trade (fixed costs dominate below ~₹5k positions)
HEAT_CAP = .06     # fraction of equity at risk across all holdings
STRATEGY = 'claude-original-v2'
RULE = dict(name='Original Claude V2 · paper Trader', strategy=STRATEGY,
    max_positions=5, position_pct=10, max_buys_per_day=3, daily_loss_pct=None,
    entry='Completed close above prior 20-session high; prior 50-day average above prior 200-day; non-overlapping prior 10/30 volume ≥ 1.2; Nifty 500 close above 200-day average. Rank by 63-session return. Next session only, first available fresh depth 09:15–09:20 IST; missed openings expire.',
    exit='Initial stop 2 × Wilder ATR(14) below fill; highest completed close minus 3 × ATR trailing stop, ratchet only from the following session; completed close below prior 50-day average exits next session. No fixed profit target.',
    risk='1% of current equity at risk including estimated round-trip fees and 0.25% per-side slippage reserve; 6% portfolio risk cap including buy/sell fees and the same slippage reserve for existing holdings and proposed buys. Existing holdings retain at least their previous protective stop during migration. No daily 2% circuit in original Claude.',
    execution='Local depth-limited simulation only. Actual opening quotes, never a retrospective opening-price fill.')


def init(path):
    with connect(path) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS claude_positions(
            symbol TEXT PRIMARY KEY, entry REAL NOT NULL, stop REAL NOT NULL,
            highest REAL NOT NULL, entry_day TEXT NOT NULL, updated_day TEXT NOT NULL,
            ma_exit INTEGER NOT NULL DEFAULT 0, legacy INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS claude_benchmark(day TEXT PRIMARY KEY, close REAL NOT NULL);
        ''')
        if 'entry_request' not in {r[1] for r in db.execute('PRAGMA table_info(claude_positions)')}:
            db.execute('ALTER TABLE claude_positions ADD COLUMN entry_request TEXT')


def local_day(clock):
    return clock.astimezone(IST).date().isoformat()


def features(rows):
    """Matches the frozen research indicator windows, including shifted volume."""
    rows = effective_rows(rows)
    if len(rows) < MIN_SESSIONS+1:
        return None
    prior = None; atr = None; seed = []; out = []; jumps = [0]
    for n, row in enumerate(rows):
        c, h, low = row['close'], row['high'], row['low']
        if not all(math.isfinite(x) for x in (c, h, low, row['open'], row['volume'])) or not 0 < low <= min(c, row['open']) <= max(c, row['open']) <= h or row['volume'] < 0:
            return None
        tr = max(h-low, abs(h-prior), abs(low-prior)) if prior else h-low
        jumps.append(jumps[-1] + int(bool(prior) and (abs(row['open']/prior-1)>.25 or abs(c/prior-1)>.25)))
        if atr is None:
            seed.append(tr)
            if len(seed) == 14: atr = sum(seed)/14
        else: atr = (13*atr+tr)/14
        if n >= MIN_SESSIONS:
            window = long_window(n)
            prev = rows[n-window:n]
            volume = sum(r['volume'] for r in rows[n-40:n-10])/30
            quality = all(r['volume'] > 0 for r in rows[n-20:n])
            quality = quality and jumps[n]-jumps[max(1,n-window)] == 0
            out.append(dict(as_of=row['ts'][:10], close=c, atr=atr,
                dma50=sum(r['close'] for r in rows[n-50:n])/50,
                dma200=sum(r['close'] for r in prev)/window, short_history=n<200,
                high20=max(r['high'] for r in rows[n-20:n]),
                live_high20=max(r['high'] for r in rows[n-19:n+1]),
                volume_ratio=(sum(r['volume'] for r in rows[n-10:n])/10)/volume if volume else 0,
                turnover20=sum(r['close']*r['volume'] for r in rows[n-20:n])/20,
                rank=c/rows[n-63]['close']-1,
                quality=quality and abs(c/prior-1) <= .15))
        prior = c
    return out


def market_filter_enabled(config):
    # Only an explicit saved opt-out disables the original market trend rule.
    return config.get('nifty500_filter_enabled') is not False


def position_limits(config):
    result=[]
    for key in ('max_positions','max_buys_per_day'):
        value=config.get(key,RULE[key])
        result.append(value if type(value) is int and value>0 else RULE[key])
    return tuple(result)


def active_rule(config):
    rule=dict(RULE)
    rule['max_positions'],rule['max_buys_per_day']=position_limits(config)
    if not market_filter_enabled(config):
        rule.update(name='Claude V2 · Nifty 500 filter off · paper Trader',
        entry=rule['entry'].replace('Nifty 500 close above 200-day average.',
            'Nifty 500 trend filter disabled; completed-session data checks remain required.'))
    if config.get('full_session_buying') is True:
        rule['name']=rule['name'].replace(' · paper Trader',' · full-session buying · paper Trader')
        rule['entry']=rule['entry'].replace('Next session only, first available fresh depth 09:15–09:20 IST; missed openings expire.',
            'Next session only, fresh depth throughout 09:15–15:30 IST while the NSE normal session is open; unfilled signals expire after the session.')
        rule['execution']='Local depth-limited simulation only. Fresh observed quotes throughout the normal session; never retrospective fills.'
    if config.get('allow_stale_held_valuation') is True:
        rule['risk']+=' Existing Trader holdings may use their last valid current-session quote for portfolio valuation when stale. New orders and exits still require fresh prices and depth. Missing, future-dated or prior-session quotes block new buys.'
    if config.get('live_breakout') is True:
        rule['live_breakout']=True
        rule['name']=rule['name'].replace(' · paper Trader',' · live breakout · paper Trader')
        rule['entry']=rule['entry'].replace('Completed close above prior 20-session high;',
            'Fresh live price above the highest high of the latest 20 completed sessions, including yesterday; live price and actual fill must both remain above that level;')
        rule['entry']=rule['entry'].replace('Next session only,','During the next session,')
        rule['entry']+=' Trend, volume, ATR and ranking still use completed-session inputs. Existing holdings keep their stops and exit rules.'
    return rule


def eligible(signal, price, clock, benchmark, *, use_market_filter=True, live_breakout=False):
    if not signal or not benchmark or (use_market_filter and not benchmark['allowed']):
        return False
    age=(clock.astimezone(IST).date()-datetime.fromisoformat(signal['as_of']).date()).days
    level=signal.get('live_high20') if live_breakout else signal['high20']
    breakout=(level is not None and math.isfinite(level) and level>0 and
              (price if live_breakout else signal['close'])>level)
    return (1 <= age <= 7 and signal['as_of'] == benchmark['as_of'] and
        signal['quality'] and signal['close'] >= 20 and price >= 20 and
        signal['turnover20'] >= 10_000_000 and abs(price/signal['close']-1) <= .15 and
        breakout and signal['dma50'] > signal['dma200'] and
        signal['volume_ratio'] >= 1.2 and 0 < 2*signal['atr'] < price)


def portfolio_valuation(db, account, clock):
    """Trader-only opt-in: old marks value holdings, never execute a trade."""
    cfg=get_state(db,'auto_config',{}).get('trader',{})
    allow=cfg.get('allow_stale_held_valuation') is True and cfg.get('strategy')==STRATEGY
    value=account['cash'];stale_marks=[]
    for p in db.execute("SELECT * FROM positions WHERE portfolio='trader' AND quantity>0"):
        q=db.execute('SELECT * FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
        if not q:return None,[]
        if not math.isfinite(q['price']) or q['price']<=0:return None,[]
        if not fresh(q,clock):
            if not allow:return None,[]
            try:
                traded=datetime.fromtimestamp(q['trade_time']/1000,timezone.utc)
                received=datetime.fromisoformat(q['received_at'])
                if (received.tzinfo is None or traded>clock or received>clock or
                    local_day(traded)!=local_day(clock) or local_day(received)!=local_day(clock)):
                    return None,[]
            except (TypeError,ValueError,OverflowError,OSError):return None,[]
            stale_marks.append(dict(symbol=p['symbol'],price=q['price'],trade_time=q['trade_time'],received_at=q['received_at']))
        value+=p['quantity']*q['price']
    return value,stale_marks


def holding_stop_risk(position,state):
    """Remaining cost already includes allocated buy fees, including legacy buys.

    Never count those fees twice or offset another holding's risk with profit.
    Use the same 0.25%-per-side reserve on the recorded entry value.
    """
    stop_value=position['quantity']*state['stop']
    exit_fee=charges(stop_value,'SELL',True)['total']
    net_loss=max(0,position['cost']-stop_value+exit_fee)
    return net_loss+position['quantity']*state['entry']*.005


def limits(db, account, clock):
    eq,_=portfolio_valuation(db,account,clock)
    if eq is None:raise ValueError(stale_message(db,'trader',clock))
    if eq <= 0:raise ValueError('Portfolio equity is not positive; new buys wait.')
    heat=0
    for p in db.execute("SELECT * FROM positions WHERE portfolio='trader' AND quantity>0"):
        s=db.execute('SELECT * FROM claude_positions WHERE symbol=?',(p['symbol'],)).fetchone()
        if not s: raise ValueError('Existing Trader holding awaits valid ATR history; new buys wait.')
        heat += holding_stop_risk(p,s)
    return eq,heat,min(account['cash'],account['initial_cash']*.1)


def fits(q, price, atr, eq, heat, budget, mark, fee=None):
    if q < 1 or 2*atr >= price: return False
    buy=charges(round(q*price,2),'BUY')['total'] if fee is None else fee
    risk=q*2*atr+buy+charges(q*(price-2*atr),'SELL',True)['total']+q*price*.005
    after=eq-buy+q*(mark-price)
    return q*price+buy <= budget and risk <= TRADE_RISK*eq and heat+risk <= HEAT_CAP*after


def quantity(price, atr, eq, heat, budget):
    lo=0;hi=max(0,min(1_000_000,math.floor(budget/price)))
    while lo < hi:
        mid=(lo+hi+1)//2
        if fits(mid,price,atr,eq,heat,budget,price):lo=mid
        else:hi=mid-1
    return lo


class ClaudeTrader:
    def __init__(self,path):
        self.path=path;init(path);self.loaded=0;self.histories={};self.benchmark=None;self.attempted=0

    def refresh_benchmark(self,clock):
        today=local_day(clock)
        with connect(self.path) as db:
            saved=get_state(db,'claude_benchmark_fetch',{})
        if saved.get('session') == today and saved.get('okay'):return
        if self.attempted and time.monotonic()-self.attempted < 300:return
        self.attempted=time.monotonic()
        end=clock.astimezone(IST).date()-timedelta(days=1);start=end-timedelta(days=550)
        try:
            token=load_token()
            if not token:raise ValueError('No data token')
            url='https://api.upstox.com/v3/historical-candle/'+quote('NSE_INDEX|Nifty 500',safe='')+'/days/1/'+str(end)+'/'+str(start)
            r=requests.get(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(3,8))
            if r.status_code != 200:raise ValueError('Historical service unavailable')
            p=r.json()
            if p.get('status') != 'success':raise ValueError('Historical response unavailable')
            bars={}
            for row in p['data']['candles']:
                day=datetime.fromisoformat(row[0]).astimezone(IST).date().isoformat()
                o,h,low,c,v=map(float,row[1:6])
                if not str(start) <= day <= str(end) or not all(math.isfinite(x) for x in (o,h,low,c,v)) or not 0 < low <= min(o,c) <= max(o,c) <= h or v < 0:raise ValueError('Invalid index candle')
                if day in bars and bars[day] != c:raise ValueError('Conflicting index candle')
                bars[day]=c
            if len(bars)<201:raise ValueError('Index history incomplete')
            with connect(self.path) as db:
                db.executemany('INSERT OR REPLACE INTO claude_benchmark VALUES(?,?)',sorted(bars.items()))
                set_state(db,'claude_benchmark_fetch',{'session':today,'okay':True,'as_of':max(bars)})
            self.loaded=0
        except (requests.RequestException,ValueError,KeyError,TypeError,IndexError):
            with connect(self.path) as db:set_state(db,'claude_benchmark_fetch',{'session':today,'okay':False})

    def load(self,clock):
        today=local_day(clock)
        with connect(self.path) as db:
            bench=[dict(r) for r in db.execute('SELECT * FROM claude_benchmark WHERE day<? ORDER BY day',(today,))]
            fetch=get_state(db,'claude_benchmark_fetch',{})
            required=get_state(db,'auto_previous_session','')
            latest=db.execute('SELECT MAX(substr(ts,1,10)) FROM candles WHERE '+symbol_filter(db,include_holdings=True)+' AND substr(ts,1,10)<?',(today,)).fetchone()[0] or ''
            self.benchmark=None
            if len(bench)>=201 and fetch.get('okay') and fetch.get('session')==today:
                last=bench[-1];age=(datetime.fromisoformat(today)-datetime.fromisoformat(last['day'])).days
                if 1<=age<=7 and last['day']>=max(required,latest):
                    self.benchmark=dict(as_of=last['day'],allowed=last['close']>sum(r['close'] for r in bench[-200:])/200)
            bad={r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind IN ('candle','identity')")}
            bad.update(r[0] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0"))
            groups=collections.defaultdict(list)
            for row in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles WHERE '+symbol_filter(db,include_holdings=True)+' AND substr(ts,1,10)<? ORDER BY symbol,ts',(today,)):
                groups[row['symbol']].append(dict(row))
        required_days={r['day'] for r in bench[-201:]}
        self.histories={}
        for symbol,rows in groups.items():
            f=features(rows)
            if not f:continue
            valid=symbol not in bad and required_days <= {r['ts'][:10] for r in rows}
            for s in f:s['quality']=s['quality'] and valid
            self.histories[symbol]=f
        self.loaded=time.monotonic();self.loaded_day=today

    def update_positions(self,db,clock):
        today=local_day(clock)
        for p in db.execute("SELECT * FROM positions WHERE portfolio='trader' AND quantity>0").fetchall():
            rows=self.histories.get(p['symbol'],[])
            state=db.execute('SELECT * FROM claude_positions WHERE symbol=?',(p['symbol'],)).fetchone()
            last_buy=db.execute("SELECT request_id FROM trades WHERE portfolio='trader' AND symbol=? AND side='BUY' ORDER BY rowid DESC LIMIT 1",(p['symbol'],)).fetchone()
            request=last_buy[0] if last_buy else None
            if state and state['entry_request'] != request:
                # A manual buy while paused starts a new migration, not an obsolete stop.
                db.execute('DELETE FROM claude_positions WHERE symbol=?',(p['symbol'],));state=None
            if not state:
                if not rows or rows[-1]['atr']<=0:continue
                entry=p['cost']/p['quantity'];stop=max(entry*.97,entry-2*rows[-1]['atr'])
                # Preserve the former stop; do not pretend old trades were Claude entries.
                db.execute('INSERT OR REPLACE INTO claude_positions VALUES(?,?,?,?,?,?,?,?,?)',(p['symbol'],entry,stop,entry,today,rows[-1]['as_of'],int(rows[-1]['close']<rows[-1]['dma50']),1,request))
                continue
            state=dict(state)
            for s in rows:
                if s['as_of'] >= today or s['as_of'] < state['entry_day'] or s['as_of'] <= state['updated_day']:continue
                state['highest']=max(state['highest'],s['close'])
                state['stop']=max(state['stop'],state['highest']-3*s['atr'])
                state['ma_exit']=int(s['close']<s['dma50'])
                state['updated_day']=s['as_of']
            db.execute('UPDATE claude_positions SET stop=?,highest=?,updated_day=?,ma_exit=? WHERE symbol=?',(state['stop'],state['highest'],state['updated_day'],state['ma_exit'],p['symbol']))

    def exit_reason(self,db,symbol,price):
        s=db.execute('SELECT * FROM claude_positions WHERE symbol=?',(symbol,)).fetchone()
        if s:
            if price<=s['stop']:return 'Claude ATR protective stop (legacy stop floor preserved)' if s['legacy'] else 'Claude ATR protective stop'
            if s['ma_exit']:return 'Claude completed close below prior 50-day average'
        else:
            # Missing migration history must never remove an existing protective exit.
            p=db.execute("SELECT * FROM positions WHERE portfolio='trader' AND symbol=?",(symbol,)).fetchone()
            if p and p['quantity'] and price<=p['cost']/p['quantity']*.97:return 'Legacy 3% protective stop pending ATR migration'
        return None

    def guard(self,signal,clock):
        def check(db,order,quote,account,position):
            from .autotrading import day_start,entry_window_open,entry_window_label
            cfg=get_state(db,'auto_config',{}).get('trader',{})
            if not cfg.get('enabled') or cfg.get('strategy')!=STRATEGY:raise ValueError('Claude paper Trader is paused or strategy changed.')
            execution=datetime.fromisoformat(quote['execution_time']) if quote.get('execution_time') else clock
            price=quote['price'];q=quote.get('fill_quantity',order['quantity']);held=position['quantity'] if position else 0
            if order['side']=='SELL':
                reason=self.exit_reason(db,order['symbol'],price) if held else None
                if not reason or order['quantity']!=held:raise ValueError('Exit condition changed; recheck next scan.')
                if q==held:db.execute('DELETE FROM claude_positions WHERE symbol=?',(order['symbol'],))
                return reason
            if not entry_window_open(execution,cfg,opening=True):raise ValueError('Claude entries only use '+entry_window_label(cfg,opening=True)+'.')
            require_buy_universe(db,'trader',order['symbol'],execution)
            live=cfg.get('live_breakout') is True
            if local_day(execution)!=local_day(clock) or not eligible(signal,price,execution,self.benchmark,use_market_filter=market_filter_enabled(cfg),live_breakout=live):raise ValueError('Completed signal, market filter or fill gap check failed.')
            if live:
                latest=db.execute('SELECT * FROM quotes WHERE symbol=?',(order['symbol'],)).fetchone()
                if not fresh(latest,execution) or not eligible(signal,latest['price'],execution,self.benchmark,use_market_filter=market_filter_enabled(cfg),live_breakout=True):
                    raise ValueError('Live breakout no longer confirmed by a fresh trade price; buy waits.')
            start=day_start(execution)
            if held or db.execute("SELECT 1 FROM trades WHERE portfolio='trader' AND symbol=? AND created_at>=?",(order['symbol'],start)).fetchone():raise ValueError('No averaging or same-session re-entry.')
            max_positions,max_buys=position_limits(cfg)
            if db.execute("SELECT COUNT(*) FROM positions WHERE portfolio='trader' AND quantity>0").fetchone()[0]>=max_positions:raise ValueError(f'{max_positions}-position limit reached.')
            if db.execute("SELECT COUNT(*) FROM trades WHERE portfolio='trader' AND source='automatic' AND side='BUY' AND created_at>=?",(start,)).fetchone()[0]>=max_buys:raise ValueError(f'{max_buys} new positions per session limit reached.')
            eq,heat,budget=limits(db,account,execution)
            mark=db.execute('SELECT price FROM quotes WHERE symbol=?',(order['symbol'],)).fetchone()[0]
            if not fits(q,price,signal['atr'],eq,heat,budget,mark,quote.get('fill_fee')):raise ValueError('Claude risk, heat, cash or position cap exceeded at actual fill.')
            db.execute('INSERT OR REPLACE INTO claude_positions VALUES(?,?,?,?,?,?,0,0,?)',(order['symbol'],price,price-2*signal['atr'],price,local_day(execution),signal['as_of'],order['request_id']))
            label='Original Claude V2' if market_filter_enabled(cfg) else 'Claude V2 (Nifty 500 filter off)'
            _,stale_marks=portfolio_valuation(db,account,execution)
            valuation_note='; stale holding valuation: '+', '.join(f"{m['symbol']} at {m['price']}" for m in stale_marks) if stale_marks else ''
            trigger=(f" live breakout above {signal['live_high20']:.4f}; completed inputs " if live else ' completed breakout; signal ')
            return label+trigger+signal['as_of']+'; 2 ATR stop; 1% risk; paper only'+('; full-session buying' if cfg.get('full_session_buying') is True else '')+valuation_note
        return check

    def execute(self,symbol,side,qty,signal,clock,realtime):
        from .autotrading import event
        if realtime:clock=datetime.now(timezone.utc)
        try:
            trade(self.path,dict(portfolio='trader',symbol=symbol,side=side,quantity=qty,request_id='claude-'+str(uuid.uuid4())),None if realtime else clock,auto_guard=self.guard(signal,clock))
            return 1
        except ValueError as error:
            with connect(self.path) as db:event(db,'trader','blocked',str(error),symbol,clock=clock)
            return 0

    def scan(self,clock,realtime=False):
        from .autotrading import fresh,day_start,entry_window_open,entry_window_label
        self.refresh_benchmark(clock)
        if not self.loaded or time.monotonic()-self.loaded>300 or getattr(self,'loaded_day',None)!=local_day(clock):self.load(clock)
        if realtime:refresh_held_quotes(self.path,'trader')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            if realtime:clock=datetime.now(timezone.utc)
            self.update_positions(db,clock)
            cfg=get_state(db,'auto_config',{}).get('trader',{})
            use_market_filter=market_filter_enabled(cfg)
            held=[dict(r) for r in db.execute("SELECT * FROM positions WHERE portfolio='trader' AND quantity>0")]
            quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
            exits=[p for p in held if fresh(quotes.get(p['symbol']),clock) and self.exit_reason(db,p['symbol'],quotes[p['symbol']]['price'])]
        fills=sum(self.execute(p['symbol'],'SELL',p['quantity'],None,clock,realtime) for p in exits)
        def status(message,state='monitoring',**extra):return dict(state=state,message=message,fills_this_scan=fills,strategy=STRATEGY,nifty500_filter_enabled=use_market_filter,live_breakout=cfg.get('live_breakout') is True,**extra)
        if not self.benchmark:return status('Waiting for verified completed Nifty 500 history; new buys blocked. Exits remain active.','data_wait')
        if use_market_filter and not self.benchmark['allowed']:return status('Nifty 500 is below its 200-day average; no new entries. Exits remain active.','market_wait')
        label='Original Claude' if use_market_filter else 'Claude V2 (Nifty 500 filter off)'
        if cfg.get('live_breakout') is True:label+=' live breakout'
        if not entry_window_open(clock,cfg,opening=True):return status(label+' active. Monitoring exits; new signals use '+entry_window_label(cfg,opening=True)+'.')
        candidates=[]
        try:
            with connect(self.path) as db:allowed=allowed_symbols(db,'trader',clock)
        except ValueError as error:return status(str(error),'data_wait')
        for symbol,rows in self.histories.items():
            if allowed is not None and symbol not in allowed:continue
            q=quotes.get(symbol);s=rows[-1]
            if fresh(q,clock) and eligible(s,q['price'],clock,self.benchmark,use_market_filter=use_market_filter,live_breakout=cfg.get('live_breakout') is True):candidates.append((s['rank'],symbol,s))
        for _,symbol,s in sorted(candidates,key=lambda x:(-x[0],x[1])):
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                if realtime:clock=datetime.now(timezone.utc)
                max_positions,max_buys=position_limits(get_state(db,'auto_config',{}).get('trader',{}))
                if db.execute("SELECT COUNT(*) FROM positions WHERE portfolio='trader' AND quantity>0").fetchone()[0]>=max_positions:break
                if db.execute("SELECT COUNT(*) FROM trades WHERE portfolio='trader' AND side='BUY' AND source='automatic' AND created_at>=?",(day_start(clock),)).fetchone()[0]>=max_buys:break
                if db.execute("SELECT 1 FROM positions WHERE portfolio='trader' AND symbol=? AND quantity>0",(symbol,)).fetchone():continue
                if db.execute("SELECT 1 FROM trades WHERE portfolio='trader' AND symbol=? AND created_at>=?",(symbol,day_start(clock))).fetchone():continue
                account=db.execute("SELECT * FROM portfolios WHERE name='trader'").fetchone()
                try:eq,heat,budget=limits(db,account,clock)
                except ValueError as error:return status(str(error),'risk_wait')
            # Reserve 0.5% adverse depth movement for the sizing request; exact fill is rechecked atomically.
            q=quantity(quotes[symbol]['price']*1.005,s['atr'],eq,heat,budget)
            if q:fills+=self.execute(symbol,'BUY',q,s,clock,realtime)
        window='full-session buying' if cfg.get('full_session_buying') is True else 'opening signals'
        with connect(self.path) as db:
            max_positions,max_buys=position_limits(get_state(db,'auto_config',{}).get('trader',{}))
            positions=db.execute("SELECT COUNT(*) FROM positions WHERE portfolio='trader' AND quantity>0").fetchone()[0]
            buys=db.execute("SELECT COUNT(*) FROM trades WHERE portfolio='trader' AND side='BUY' AND source='automatic' AND created_at>=?",(day_start(clock),)).fetchone()[0]
        capacity=f'{positions}/{max_positions} holdings; {buys}/{max_buys} new buys today.'
        if buys>=max_buys:capacity+=' Daily buy limit reached; exits continue.'
        elif positions>=max_positions:capacity+=' Holding limit reached; exits continue.'
        return status(f'{label}: {len(candidates)} eligible signals; {window}; risk and heat limits checked. '+capacity,
            candidates=len(candidates),position_count=positions,buy_count=buys,max_positions=max_positions,max_buys_per_day=max_buys)
