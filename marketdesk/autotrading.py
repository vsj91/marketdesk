from .working_universe import symbol_filter
"""Deterministic experimental strategies. All execution uses the local paper ledger."""
import collections,json,math,threading,time,uuid
from datetime import datetime,timedelta,timezone
from .storage import connect,get_state,set_state,number,now
from .paper import trade
from .execution import charges,money
from .order_audit import init_attempts,blocked_category,current_identity
from .quality import audit
from . import drawdown
from .history_rules import MIN_SESSIONS,long_window,effective_rows
from .quote_health import fresh,stale_holdings,stale_message,refresh_held_quotes
from .buy_universe import allowed_symbols,require_buy_universe,universe_status,MembershipRefresh

IST=timezone(timedelta(hours=5,minutes=30))
RULES={
 'trader':{'name':'Daily breakout · swing paper strategy','max_positions':5,'position_pct':10,'max_buys_per_day':3,'stop_pct':3,'take_profit_pct':6,'daily_loss_pct':2,
 'entry':'200+ completed daily candles; price above the previous 20-session high; 50-day average above 200-day; historical 10/30-session volume ratio ≥ 1.2.',
 'exit':'Sell the whole position at a 3% stop, 6% profit target, or below the 50-day average. Positions may remain overnight.'},
 'investor':{'name':'Fundamentals and trend · paper strategy','max_positions':8,'position_pct':10,'max_buys_per_day':2,'stop_pct':10,'take_profit_pct':None,'daily_loss_pct':2,
 'entry':'Complete annual inputs, no source conflicts, positive PBT/equity and revenue/profit YoY growth, interest coverage ≥ 3 (or reported zero finance costs), net debt/equity ≤ 0.5, and price above the 200-day average. Financial periods/basis/units must agree.',
 'exit':'Sell the whole position at a 10% stop or below the 200-day average. Incomplete financial refreshes block new buys; they do not force a sale.'}}

def capacity_rules(portfolio,config):
 rules=dict(RULES[portfolio])
 for key in ('max_positions','max_buys_per_day'):
  value=config.get(key)
  if type(value) is int and value>0:rules[key]=value
 return rules

def buy_budget(portfolio,account,rules,config):
 budget=min(account['cash'],account['initial_cash']*rules['position_pct']/100)
 if portfolio=='investor' and config.get('buy_target_inr') is not None:
  target=number(config['buy_target_inr'])
  if target is None or target<=0:raise ValueError('Invalid Investor buy target; new buys wait.')
  budget=min(budget,target)
 return budget

def init_auto(path):
 with connect(path) as db:
  db.executescript('''CREATE TABLE IF NOT EXISTS auto_days(portfolio TEXT NOT NULL,session TEXT NOT NULL,opening_equity REAL NOT NULL,halted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(portfolio,session));
  CREATE TABLE IF NOT EXISTS auto_events(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,portfolio TEXT NOT NULL,kind TEXT NOT NULL,symbol TEXT,reason TEXT NOT NULL,trade_id TEXT);''')
  init_attempts(db)
  if get_state(db,'auto_config') is None:set_state(db,'auto_config',{p:{'enabled':False} for p in RULES})

def session(clock):return clock.astimezone(IST).date().isoformat()
def entry_window_open(clock,config,opening=False):
 local=clock.astimezone(IST);minute=(local.hour,local.minute)
 if config.get('full_session_buying') is True:return (9,15)<=minute<(15,30)
 return (9,15)<=minute<(9,20) if opening else (9,20)<=minute<(15,0)

def entry_window_label(config,opening=False):
 if config.get('full_session_buying') is True:return '09:15–15:30 IST during the connected NSE normal session'
 return 'next-session opening depth, 09:15–09:20 IST' if opening else '09:20–15:00 IST'

def day_start(clock):return clock.astimezone(IST).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).isoformat()
def event(db,portfolio,kind,reason,symbol=None,trade_id=None,clock=None,details=None):
 db.execute('INSERT INTO auto_events(created_at,portfolio,kind,symbol,reason,trade_id) VALUES(?,?,?,?,?,?)',((clock or datetime.now(timezone.utc)).isoformat(),portfolio,kind,symbol,reason,trade_id))
 if kind in ('blocked','fill') or details is not None:
  details=dict(details or {})
  for key,value in dict(requested_quantity=None,filled_quantity=0 if kind=='blocked' else None,risk_used_inr=0 if kind=='blocked' else None,risk_pct_equity=None,strategy_version_id=current_identity(db,portfolio,True)[0]).items():details.setdefault(key,value)
  if kind=='blocked':details.setdefault('blocked_by',blocked_category(reason))
  db.execute('INSERT INTO auto_event_evidence VALUES(last_insert_rowid(),?)',(json.dumps(details,sort_keys=True),))

def daily_buy_count(db,portfolio,clock):
 return db.execute("SELECT COUNT(*) FROM trades WHERE portfolio=? AND source='automatic' AND side='BUY' AND created_at>=?",(portfolio,day_start(clock))).fetchone()[0]

def load_signals(path):
 with connect(path) as db:
  histories=collections.defaultdict(lambda:collections.deque(maxlen=201))
  for row in db.execute('SELECT symbol,ts,open,close,high,volume FROM candles WHERE '+symbol_filter(db,include_holdings=True)+' ORDER BY symbol,ts'):histories[row['symbol']].append(dict(row))
  bad={r[0] for r in db.execute("SELECT DISTINCT symbol FROM issues WHERE kind IN ('candle','identity')")}
  bad.update(r['symbol'] for r in db.execute("SELECT symbol FROM quality WHERE json_extract(report,'$.trader_ready')=0"))
  funds={r['symbol']:json.loads(r['details']) for r in db.execute('SELECT symbol,details FROM fundamentals WHERE '+symbol_filter(db,include_holdings=True))}
 result={}
 latest_session=max((rows[-1]['ts'][:10] for rows in histories.values() if rows),default='')
 for symbol,history in histories.items():
  rows=effective_rows(history)
  if symbol in bad or len(rows)<MIN_SESSIONS or rows[-1]['ts'][:10]!=latest_session:continue
  closes=[r['close'] for r in rows];volumes=[r['volume'] for r in rows]
  previous_volume=sum(volumes[-40:-10])/30
  result[symbol]={'as_of':rows[-1]['ts'],'close':closes[-1],'dma50':sum(closes[-50:])/50,'dma200':sum(closes[-long_window(len(rows)):])/long_window(len(rows)),'short_history':len(rows)<200,'high20':max(r['high'] for r in rows[-20:]),'volume_ratio':(sum(volumes[-10:])/10)/previous_volume if previous_volume>0 else 0,'turnover20':sum(r['close']*r['volume'] for r in rows[-20:])/20,'fundamentals':funds.get(symbol,{})}
 return result

def entry_signal(portfolio,signal,price,clock):
 if not signal:return None
 try:
  age=(clock.astimezone(IST).date()-datetime.fromisoformat(signal['as_of']).astimezone(IST).date()).days
  if not 1<=age<=7 or price<20 or signal['turnover20']<10_000_000 or abs(price/signal['close']-1)>.15:return None
  if portfolio=='trader':
   if price>signal['high20'] and signal['dma50']>signal['dma200'] and signal['volume_ratio']>=1.2:
    return ('20-session price breakout, positive 50/200-day trend and historical volume confirmation',price/signal['high20']-1)
   return None
  f=signal['fundamentals'];annual=f.get('latest') or {};summary=f.get('summary') or {}
  if f.get('status')!='COMPLETE' or f.get('source_conflicts') or price<=signal['dma200']:return None
  period=datetime.strptime(annual['period'],'%b %Y').date()
  if not 0<=(clock.date()-period).days<=550:return None
  if any(summary.get(field)!=annual.get('period') for field in ('revenue_period','net_profit_period')):return None
  if summary.get('statement_type')!=annual.get('statement_type') or summary.get('units')!=annual.get('units_in') or summary.get('frequency') not in ('yearly','annual'):return None
  values=[number(annual.get(k)) for k in ('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents')]
  if any(v is None for v in values):return None
  pbt,finance,equity,borrowings,leases,cash=values
  revenue,profit=number(summary.get('revenue_growth_yoy')),number(summary.get('net_profit_growth_yoy'))
  if pbt<=0 or equity<=0 or min(finance,borrowings,leases,cash)<0 or revenue is None or profit is None or min(revenue,profit)<=0:return None
  if finance and (pbt+finance)/finance<3 or (borrowings+leases-cash)/equity>.5:return None
  # Rank among stocks that already pass every gate: growth 50%, profitability on equity 30%, trend strength 20%.
  growth=(min(revenue,100)+min(profit,100))/200
  quality=min(max(pbt/equity,0),.4)/.4
  trend=min(max(price/signal['dma200']-1,0),.5)/.5
  return ('Complete matching annual inputs, positive growth, debt/interest filters and price above 200-day average',.5*growth+.3*quality+.2*trend)
 except (ValueError,TypeError,KeyError,ZeroDivisionError):return None

def exit_signal(portfolio,signal,position,price):
 rules=RULES[portfolio];average=position['cost']/position['quantity']
 if price<=average*(1-rules['stop_pct']/100):return f"{rules['stop_pct']}% loss threshold"
 if rules['take_profit_pct'] and price>=average*(1+rules['take_profit_pct']/100):return f"{rules['take_profit_pct']}% profit target"
 if signal and price<signal['dma50' if portfolio=='trader' else 'dma200']:return 'Price below '+('50' if portfolio=='trader' else '200')+'-day average'
 return None

def investor_valuation(db,clock):
 """Opt-in current-session marks value holdings only; never authorize fills."""
 cfg=get_state(db,'auto_config',{}).get('investor',{})
 allow=cfg.get('allow_stale_held_valuation') is True
 account=db.execute("SELECT * FROM portfolios WHERE name='investor'").fetchone()
 value=account['cash'];marks=[]
 for p in db.execute("SELECT * FROM positions WHERE portfolio='investor' AND quantity>0"):
  q=db.execute('SELECT * FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
  if not q or not math.isfinite(q['price']) or q['price']<=0:return None,[]
  if not fresh(q,clock):
   if not allow:return None,[]
   try:
    traded=datetime.fromtimestamp(q['trade_time']/1000,timezone.utc)
    received=datetime.fromisoformat(q['received_at'])
    if (received.tzinfo is None or traded>clock or received>clock or
        session(traded)!=session(clock) or session(received)!=session(clock)):return None,[]
   except (TypeError,ValueError,OverflowError,OSError):return None,[]
   marks.append(dict(symbol=p['symbol'],price=q['price'],trade_time=q['trade_time'],received_at=q['received_at']))
  value+=p['quantity']*q['price']
 return value,marks

def investor_stop_risk(cost):
 """Fee-inclusive remaining cost; reserve 0.25% per side without double-counting buy fees."""
 stop_value=cost*(1-RULES['investor']['stop_pct']/100)
 return max(0,cost-stop_value+charges(stop_value,'SELL',True)['total'])+cost*.005

def investor_portfolio_risk(db,clock):
 raw=get_state(db,'auto_config',{}).get('investor',{}).get('portfolio_risk_cap_pct')
 if raw is None:return None
 cap=number(raw)
 if cap is None or cap<=0:raise ValueError('Invalid Investor portfolio risk cap; new buys wait. Exits remain active.')
 eq,_=investor_valuation(db,clock)
 if eq is None:raise ValueError(stale_message(db,'investor',clock))
 if eq<=0:raise ValueError('Investor equity is not positive; new buys wait. Exits remain active.')
 risk=sum(investor_stop_risk(p['cost']) for p in db.execute("SELECT cost FROM positions WHERE portfolio='investor' AND quantity>0"))
 return dict(equity=eq,risk=risk,risk_pct=risk/eq*100,cap_pct=cap,limit=eq*cap/100)

def investor_buy_fits(q,price,mark,risk,fee=None):
 if q<1:return False
 value=money(q*price);fee=charges(value,'BUY')['total'] if fee is None else fee
 after=risk['equity']-fee+q*(mark-price)
 return after>0 and risk['risk']+investor_stop_risk(value+fee)<=after*risk['cap_pct']/100

def investor_risk_quantity(maximum,price,mark,risk):
 lo=0;hi=maximum
 while lo<hi:
  middle=(lo+hi+1)//2
  if investor_buy_fits(middle,price,mark,risk):lo=middle
  else:hi=middle-1
 return lo

def equity(db,portfolio,clock):
 if portfolio=='investor':return investor_valuation(db,clock)[0]
 account=db.execute('SELECT * FROM portfolios WHERE name=?',(portfolio,)).fetchone();value=account['cash']
 for p in db.execute('SELECT * FROM positions WHERE portfolio=? AND quantity>0',(portfolio,)):
  q=db.execute('SELECT * FROM quotes WHERE symbol=?',(p['symbol'],)).fetchone()
  if not fresh(q,clock):return None
  value+=p['quantity']*q['price']
 return value

def daily_risk(db,portfolio,clock):
 value=equity(db,portfolio,clock)
 if value is None:return False,stale_message(db,portfolio,clock)
 key=session(clock)
 db.execute('INSERT OR IGNORE INTO auto_days VALUES(?,?,?,0)',(portfolio,key,value))
 day=db.execute('SELECT * FROM auto_days WHERE portfolio=? AND session=?',(portfolio,key)).fetchone()
 if value<=day['opening_equity']*(1-RULES[portfolio]['daily_loss_pct']/100) and not day['halted']:
  db.execute('UPDATE auto_days SET halted=1 WHERE portfolio=? AND session=?',(portfolio,key))
  event(db,portfolio,'risk_halt','Daily equity loss limit reached. New buys stop for this session; exits remain active.',clock=clock)
  return False,'Daily loss limit reached; exits remain active.'
 return (False,'Daily loss limit reached; exits remain active.') if day['halted'] else (True,'Within daily risk limit.')

class AutoTrading:
 def __init__(self,path,refresh=None):
  self.membership_refresh=MembershipRefresh(path)
  self.claude=None;self.quality_checked=0;self.refresh=refresh;self.path=path;init_auto(path);self.stop_event=threading.Event();self.thread=None;self.scan_lock=threading.Lock();self.signals={};self.loaded=0
 def start(self):
  if self.thread and self.thread.is_alive():return
  self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
 def stop(self):self.stop_event.set()
 def configure(self,portfolio,enabled):
  if portfolio not in RULES or not isinstance(enabled,bool):raise ValueError('Choose Trader or Investor and a true/false enabled value.')
  with connect(self.path) as db:
   db.execute('BEGIN IMMEDIATE');cfg=get_state(db,'auto_config');cfg[portfolio]={**cfg.get(portfolio,{}),'enabled':enabled,'updated_at':now()};set_state(db,'auto_config',cfg)
   statuses=get_state(db,'auto_status',{});statuses[portfolio]={'state':'waiting' if enabled else 'paused','message':'Enabled; awaiting the next engine check.' if enabled else 'Automatic paper trading is paused.','checked_at':now()};set_state(db,'auto_status',statuses)
   event(db,portfolio,'enabled' if enabled else 'paused','Automatic paper trading '+('enabled' if enabled else 'paused')+'. Existing holdings remain in the paper portfolio.')
  return self.snapshot()
 def snapshot(self):
  with connect(self.path) as db:
   return {'config':get_state(db,'auto_config',{}),'status':get_state(db,'auto_status',{}),'rules':self.active_rules(db),'events':event_snapshot(db),'interval_seconds':15}
 def active_rules(self,db):
  from .claude_trader import STRATEGY,active_rule
  cfg=get_state(db,'auto_config',{})
  rules={p:capacity_rules(p,cfg.get(p,{})) for p in RULES}
  if cfg.get('trader',{}).get('strategy')==STRATEGY:rules['trader']=active_rule(cfg['trader'])
  if cfg.get('investor',{}).get('allow_stale_held_valuation') is True:
   rules['investor']['risk']='A 2% fall from the first marked portfolio equity of the session stops new buys for that session; exits remain active. Existing Investor holdings may use their last valid current-session quote for valuation when stale. Missing, invalid, future-dated or prior-session quotes block new buys. All orders and exits still require fresh prices and depth.'
  cap=cfg.get('investor',{}).get('portfolio_risk_cap_pct')
  if cap is not None:
   rules['investor']['portfolio_risk_cap_pct']=cap
   base=rules['investor'].get('risk','A 2% daily equity loss stops new buys for that session; exits remain active.')
   rules['investor']['risk']=base+f' New buys must keep combined risk at the existing 10% stops within {cap}% of post-fill equity, including buy/sell fees and a 0.25%-per-side slippage reserve. Existing holdings remain until their stop or trend exit triggers.'
  for p in rules:
   if p=='investor' and cfg.get(p,{}).get('buy_target_inr') is not None:
    target=number(cfg[p]['buy_target_inr'])
    rules[p]['buy_target_inr']=target
    if target and target>0:
     rules[p]['entry']+=f' Future purchases target up to ₹{target:,.0f} per stock including buy fees; whole shares, available depth and risk allowance may reduce fills. Existing holdings are not resized.'
   if cfg.get(p,{}).get('buy_universe')=='nifty500':rules[p]['buy_universe']=universe_status(db,p)
   fee_limit=cfg.get(p,{}).get('max_round_trip_fee_pct')
   if fee_limit is not None:
    rules[p]['max_round_trip_fee_pct']=fee_limit
    base=rules[p].get('risk',f"A {rules[p]['daily_loss_pct']}% fall from the first marked portfolio equity of the session stops new buys; exits remain active.")
    rules[p]['risk']=base+f' Automatic buys require estimated buy-plus-sell fees ≤ {fee_limit}% of actual filled value, including delivery DP charges. Tiny partial buys are rejected; quantities are never increased to meet this check. Sell and stop orders are exempt.'
   if cfg.get(p,{}).get('full_session_buying') is True:
    rules[p]={**rules[p],'entry_window':entry_window_label(cfg[p])}
  return rules
 def scan_selected(self,portfolio,signals,clock,realtime,cfg):
  from .claude_trader import STRATEGY,ClaudeTrader
  if portfolio!='trader' or cfg.get('trader',{}).get('strategy')!=STRATEGY:
   return self.scan(portfolio,signals,clock,realtime)
  try:
   if self.claude is None:self.claude=ClaudeTrader(self.path)
   return self.claude.scan(clock,realtime)
  except Exception as error:
   # A Trader adapter failure must not alter Investor configuration.
   with connect(self.path) as db:
    current=get_state(db,'auto_config',{});current['trader']['enabled']=False;set_state(db,'auto_config',current)
    event(db,'trader','error','Claude paper Trader paused after '+type(error).__name__+'. Review before enabling again.')
   return {'state':'error','message':'Claude paper Trader paused after an engine error; Investor unchanged.'}
 def guard(self,portfolio,signal,clock):
  def check(db,order,quote,account,position):
   cfg=get_state(db,'auto_config',{}).get(portfolio,{})
   if not cfg.get('enabled'):raise ValueError('Automatic trading was paused.')
   execution_clock=datetime.fromisoformat(quote['execution_time']) if quote.get('execution_time') else clock
   rules=capacity_rules(portfolio,cfg);price=quote['price'];held=position['quantity'] if position else 0
   if order['side']=='SELL':
    reason=exit_signal(portfolio,signal,position,price) if held else None
    if not reason or order['quantity']!=held:raise ValueError('Exit signal or held quantity changed; re-evaluate next cycle.')
    return reason
   if not entry_window_open(execution_clock,cfg):raise ValueError('New entries run '+entry_window_label(cfg)+'.')
   require_buy_universe(db,portfolio,order['symbol'],execution_clock)
   if held:raise ValueError('Already holding this stock; no automatic averaging.')
   count=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(portfolio,)).fetchone()[0]
   if count>=rules['max_positions']:raise ValueError('Position limit reached.')
   start=day_start(execution_clock)
   buys=db.execute("SELECT COUNT(*) FROM trades WHERE portfolio=? AND source='automatic' AND side='BUY' AND created_at>=?",(portfolio,start)).fetchone()[0]
   if buys>=rules['max_buys_per_day']:raise ValueError('Daily new-position limit reached.')
   if db.execute('SELECT 1 FROM trades WHERE portfolio=? AND symbol=? AND created_at>=? LIMIT 1',(portfolio,order['symbol'],start)).fetchone():raise ValueError('Already traded this stock today; no same-session re-entry.')
   okay,reason=daily_risk(db,portfolio,execution_clock)
   if not okay:raise ValueError(reason)
   execution_signal=signal
   if portfolio=='investor' and signal:
    # A financial refresh can finish after candidate ranking or depth retrieval.
    # Reapply the same thresholds to the current record inside the fill transaction.
    current=db.execute('SELECT details FROM fundamentals WHERE symbol=?',(order['symbol'],)).fetchone()
    execution_signal={**signal,'fundamentals':json.loads(current['details']) if current else {}}
   signal_result=entry_signal(portfolio,execution_signal,price,execution_clock)
   if not signal_result:raise ValueError('Entry conditions changed; re-evaluate next cycle.')
   value=round(price*quote.get('fill_quantity',order['quantity']),2);total=value+quote.get('fill_fee',round(value*.001,2))
   budget=buy_budget(portfolio,account,rules,cfg)
   if total>budget:raise ValueError('Position budget exceeded at the latest price.')
   if portfolio=='investor':
    risk=investor_portfolio_risk(db,execution_clock)
    mark=db.execute('SELECT price FROM quotes WHERE symbol=?',(order['symbol'],)).fetchone()[0]
    if risk and not investor_buy_fits(quote.get('fill_quantity',order['quantity']),price,mark,risk,quote.get('fill_fee')):
     raise ValueError(f"Investor portfolio risk cap ({risk['cap_pct']:g}%) exceeded including fees and slippage; new buy blocked. Exits remain active.")
   marks=investor_valuation(db,execution_clock)[1] if portfolio=='investor' else []
   note='; stale holding valuation: '+', '.join(f"{m['symbol']} at {m['price']}" for m in marks) if marks else ''
   return signal_result[0]+note
  check.audit_signal=signal
  check.audit_strategy=portfolio+'-fundamental'
  return check
 def cycle(self,clock=None,signals=None):
  realtime=clock is None
  clock=clock or datetime.now(timezone.utc)
  with self.scan_lock:
   with connect(self.path) as db:cfg=get_state(db,'auto_config',{});stream=get_state(db,'stream',{})
   statuses={p:{'state':'paused','message':'Automatic paper trading is paused.'} for p in RULES}
   enabled=[p for p in RULES if cfg.get(p,{}).get('enabled')]
   opened=stream.get('status')=='connected' and stream.get('market_status',{}).get('NSE_EQ')=='NORMAL_OPEN'
   if enabled and opened:
    with connect(self.path) as db:
     observed=get_state(db,'auto_observed_session')
     if observed and observed<session(clock):set_state(db,'auto_previous_session',observed)
     set_state(db,'auto_observed_session',session(clock))
     required_session=get_state(db,'auto_previous_session')
    if signals is None:
     if time.monotonic()-self.loaded>300 or not self.signals:self.signals=load_signals(self.path);self.loaded=time.monotonic()
     signals=self.signals
    if required_session:signals={symbol:signal for symbol,signal in signals.items() if signal['as_of'][:10]>=required_session}
    for portfolio in enabled:
     scan_clock=datetime.now(timezone.utc) if realtime else clock
     statuses[portfolio]=self.scan_selected(portfolio,signals,scan_clock,realtime,cfg)
   else:
    for p in enabled:statuses[p]={'state':'waiting','message':'Waiting for an open NSE session and a connected feed.'}
   for p,status in statuses.items():
    status['checked_at']=(datetime.now(timezone.utc) if realtime else clock).isoformat()
    status['full_session_buying']=cfg.get(p,{}).get('full_session_buying') is True
   with connect(self.path) as db:
    db.execute('BEGIN IMMEDIATE')
    checked=datetime.now(timezone.utc) if realtime else clock
    for p,status in statuses.items():
     status['stale_holdings']=stale_holdings(db,p,checked)
     if p=='trader':
      from .claude_trader import STRATEGY,portfolio_valuation
      active=get_state(db,'auto_config',{}).get('trader',{})
      status['allow_stale_held_valuation']=active.get('strategy')==STRATEGY and active.get('allow_stale_held_valuation') is True
      if status['allow_stale_held_valuation']:
       account=db.execute("SELECT * FROM portfolios WHERE name='trader'").fetchone()
       _,marks=portfolio_valuation(db,account,checked)
       status['valuation_only_holdings']=marks
     elif p=='investor':
      active=get_state(db,'auto_config',{}).get(p,{})
      status['allow_stale_held_valuation']=active.get('allow_stale_held_valuation') is True
      if status['allow_stale_held_valuation']:
       _,marks=investor_valuation(db,checked)
       status['valuation_only_holdings']=marks
    set_state(db,'auto_status',statuses)
 def scan(self,portfolio,signals,clock,realtime=False):
  fills=0
  if realtime:refresh_held_quotes(self.path,portfolio)
  with connect(self.path) as db:
   db.execute('BEGIN IMMEDIATE')
   if realtime:clock=datetime.now(timezone.utc)
   okay,risk_message=daily_risk(db,portfolio,clock)
   cfg=get_state(db,'auto_config',{}).get(portfolio,{})
   dd_halted,dd_info=False,None
   marked=equity(db,portfolio,clock)
   if marked:dd_halted,dd_info=drawdown.update(db,portfolio,marked,db.execute('SELECT initial_cash FROM portfolios WHERE name=?',(portfolio,)).fetchone()[0],drawdown.limit_pct(cfg))
   held=[dict(r) for r in db.execute('SELECT * FROM positions WHERE portfolio=? AND quantity>0',(portfolio,))]
   quotes={r['symbol']:dict(r) for r in db.execute('SELECT * FROM quotes')}
  for p in held:
   q=quotes.get(p['symbol']);signal=signals.get(p['symbol'])
   if fresh(q,clock) and exit_signal(portfolio,signal,p,q['price']):
    fills+=self.execute(portfolio,p['symbol'],'SELL',p['quantity'],signal,clock,realtime)
  if dd_halted:return {'state':'risk_wait','message':drawdown.message(dd_info),'fills_this_scan':fills}
  if not entry_window_open(clock,cfg):return {'state':'monitoring','message':'Monitoring exits. New entries run '+entry_window_label(cfg)+'.','fills_this_scan':fills}
  if not okay:return {'state':'risk_wait','message':risk_message,'fills_this_scan':fills}
  if portfolio=='investor':
   try:
    with connect(self.path) as db:risk=investor_portfolio_risk(db,datetime.now(timezone.utc) if realtime else clock)
   except ValueError as error:return {'state':'risk_wait','message':str(error),'fills_this_scan':fills}
   if risk and risk['risk']>=risk['limit']:
    return {'state':'risk_wait','message':f"Investor stop risk including fees and slippage is ₹{risk['risk']:.2f} ({risk['risk_pct']:.3f}%), above or at its {risk['cap_pct']:g}% cap of ₹{risk['limit']:.2f}. New buys wait; existing holdings retain their stops and trend exits.",'fills_this_scan':fills,'portfolio_risk':risk}
  try:
   with connect(self.path) as db:allowed=allowed_symbols(db,portfolio,clock)
  except ValueError as error:return {'state':'data_wait','message':str(error),'fills_this_scan':fills}
  candidates=[]
  for symbol,signal in signals.items():
   if allowed is not None and symbol not in allowed:continue
   q=quotes.get(symbol)
   result=entry_signal(portfolio,signal,q['price'],clock) if fresh(q,clock) else None
   if result:candidates.append((result[1],symbol))
  for _,symbol in sorted(candidates,key=lambda x:(-x[0],x[1])):
   with connect(self.path) as db:
    account=db.execute('SELECT * FROM portfolios WHERE name=?',(portfolio,)).fetchone()
    buy_config=get_state(db,'auto_config',{}).get(portfolio,{})
    rules=capacity_rules(portfolio,buy_config)
    count=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(portfolio,)).fetchone()[0]
    buys=daily_buy_count(db,portfolio,clock)
    if count>=rules['max_positions'] or buys>=rules['max_buys_per_day']:break
    if db.execute('SELECT 1 FROM positions WHERE portfolio=? AND symbol=? AND quantity>0',(portfolio,symbol)).fetchone():continue
    if db.execute('SELECT 1 FROM trades WHERE portfolio=? AND symbol=? AND created_at>=?',(portfolio,symbol,day_start(clock))).fetchone():continue
   budget=buy_budget(portfolio,account,rules,buy_config)
   qty=max(0,math.floor((budget-25)/(quotes[symbol]['price']*1.025)))
   sizing=dict(position_budget=budget,quantity_before_risk=qty,binding_constraints=['position_cap' if budget<account['cash'] else 'cash'])
   if portfolio=='investor' and qty>0:
    try:
     with connect(self.path) as db:risk=investor_portfolio_risk(db,datetime.now(timezone.utc) if realtime else clock)
    except ValueError as error:return {'state':'risk_wait','message':str(error),'fills_this_scan':fills}
    if risk:qty=investor_risk_quantity(qty,quotes[symbol]['price']*1.005,quotes[symbol]['price'],risk)
   if qty<sizing['quantity_before_risk']:sizing['binding_constraints']=['risk_budget']
   sizing['quantity_after_risk']=qty
   if qty>0:fills+=self.execute(portfolio,symbol,'BUY',qty,signals[symbol],clock,realtime,sizing)
   else:
    with connect(self.path) as db:event(db,portfolio,'blocked','No whole share fits the current risk or position budget.',symbol,clock=clock,details=dict(sizing,blocked_by='risk_budget' if sizing['quantity_before_risk'] else 'position_cap',requested_quantity=0,filled_quantity=0,risk_used_inr=0))
  with connect(self.path) as db:
   rules=capacity_rules(portfolio,get_state(db,'auto_config',{}).get(portfolio,{}))
   buy_count=daily_buy_count(db,portfolio,clock)
   position_count=db.execute('SELECT COUNT(*) FROM positions WHERE portfolio=? AND quantity>0',(portfolio,)).fetchone()[0]
  if buy_count>=rules['max_buys_per_day']:
   message=f'Scan complete: {len(candidates)} qualifying stocks; daily new-position limit reached ({buy_count}/{rules["max_buys_per_day"]}). Exits continue; new buys resume next session.'
  elif position_count>=rules['max_positions']:
   message=f'Scan complete: {len(candidates)} qualifying stocks; maximum holdings reached ({position_count}/{rules["max_positions"]}). Exits continue.'
  else:
   message=f'Scan complete: {len(candidates)} stocks meet entry rules. Rechecking every 15 seconds; position, cash and daily limits apply.'
  return {'state':'monitoring','message':message,'candidates':len(candidates),'fills_this_scan':fills,'buy_count':buy_count,'position_count':position_count,'max_positions':rules['max_positions'],'max_buys_per_day':rules['max_buys_per_day']}
 def execute(self,portfolio,symbol,side,qty,signal,clock,realtime=False,audit_context=None):
  if realtime:clock=datetime.now(timezone.utc)
  order={'portfolio':portfolio,'symbol':symbol,'side':side,'quantity':qty,'request_id':f'auto-{portfolio}-{uuid.uuid4()}'}
  try:
   trade(self.path,order,None if realtime else clock,auto_guard=self.guard(portfolio,signal,clock),audit_context=audit_context);return 1
  except ValueError as error:
   with connect(self.path) as db:event(db,portfolio,'blocked',str(error),symbol,clock=clock,details=getattr(error,'order_audit',None))
   return 0
 def refresh_daily_candles(self,clock=None):
  if self.refresh is None:return
  clock=clock or datetime.now(timezone.utc);local=clock.astimezone(IST)
  if local.hour<6:return
  with connect(self.path) as db:
   observed=get_state(db,'auto_observed_session')
   target=observed if observed and observed<session(clock) else get_state(db,'auto_previous_session')
   if not target or target>=session(clock):return
   if not any(c.get('enabled') for c in get_state(db,'auto_config',{}).values()):return
   if get_state(db,'refresh',{}).get('status')=='running':return
   previous=get_state(db,'auto_candle_refresh',{})
   if previous.get('session')==target:return
  try:self.refresh.start([],'candles')
  except ValueError:return
  with connect(self.path) as db:
   set_state(db,'auto_candle_refresh',{'session':target,'started_at':clock.isoformat()})
   event(db,'system','data_refresh','Daily candle refresh started for the last observed session: '+target+'.')

 def refresh_quality_gaps(self):
  if self.refresh is None:return
  with connect(self.path) as db:
   queued=get_state(db,'quality_repair_queue',{})
   current=get_state(db,'refresh',{})
   if queued.get('status')!='queued' or current.get('status')=='running':return
  if current.get('status')=='interrupted' and current.get('scope')=='all' and current.get('mode')=='fundamentals':
   try:self.refresh.start([],'fundamentals')
   except ValueError:pass
   return
  try:self.refresh.start(queued.get('symbols',[]),'candles')
  except ValueError:return
  with connect(self.path) as db:
   queued.update(status='started',started_at=now());set_state(db,'quality_repair_queue',queued)

 def run(self):
  while not self.stop_event.is_set():
   try:
    self.membership_refresh.start_if_due()
    if time.monotonic()-self.quality_checked>300:
     audit(self.path);self.quality_checked=time.monotonic()
    self.cycle()
    self.refresh_quality_gaps()
    self.refresh_daily_candles()
   except Exception as error:
    with connect(self.path) as db:
     cfg=get_state(db,'auto_config',{})
     for p in RULES:
      if cfg.get(p,{}).get('enabled'):
       cfg[p]['enabled']=False;event(db,p,'error','Engine paused after '+type(error).__name__+'. Review before enabling again.')
     set_state(db,'auto_config',cfg)
   self.stop_event.wait(15)


def event_snapshot(db):
 rows=[dict(r) for r in db.execute('SELECT * FROM auto_events ORDER BY id DESC LIMIT 30')]
 for r in rows:
  evidence=db.execute('SELECT details FROM auto_event_evidence WHERE event_id=?',(r['id'],)).fetchone()
  r['audit']=json.loads(evidence[0]) if evidence else {'blocked_by':blocked_category(r['reason']) if r['kind']=='blocked' else None,'evidence':'Legacy reason only; sizing inputs unverified'}
 return rows
