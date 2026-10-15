import json
from datetime import datetime,timezone
from pathlib import Path
from marketdesk.storage import connect,get_state,DEFAULT_DB
from marketdesk.paper import portfolios
from marketdesk.autotrading import IST,entry_signal,exit_signal
from marketdesk.claude_trader import features,eligible,fits
from marketdesk.quality import require_buy_quality
from marketdesk.quote_health import fresh
clock=datetime.now(timezone.utc)
accounts=portfolios(DEFAULT_DB)
report={'checked_at':clock.astimezone(IST).isoformat(),'portfolios':accounts}
with connect() as db:
 db.execute('BEGIN')
 report['config']=get_state(db,'auto_config');report['status']=get_state(db,'auto_status')
 report['stream']={k:v for k,v in get_state(db,'stream',{}).items() if k in ('status','market_status','last_message_at')}
 report['daily_risk']=[dict(r) for r in db.execute('SELECT * FROM auto_days ORDER BY session DESC LIMIT 6')]
 report['recent_events']=[dict(r) for r in db.execute("SELECT created_at,portfolio,kind,symbol,reason FROM auto_events WHERE kind IN ('blocked','risk_halt','error','fill') ORDER BY id DESC LIMIT 50")]
 report['held_audit']=[]
 for a in accounts:
  for p in a['positions']:
   symbol=p['symbol'];portfolio=a['name']
   rows=list(reversed([dict(r) for r in db.execute('SELECT * FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT 260',(symbol,))]))
   frow=db.execute('SELECT * FROM fundamentals WHERE symbol=?',(symbol,)).fetchone();f=json.loads(frow['details']) if frow else {}
   q=db.execute('SELECT * FROM quotes WHERE symbol=?',(symbol,)).fetchone()
   last=rows[-201:];vol=[r['volume'] for r in last];prev=sum(vol[-40:-10])/30
   signal=dict(as_of=last[-1]['ts'],close=last[-1]['close'],dma50=sum(r['close'] for r in last[-50:])/50,dma200=sum(r['close'] for r in last[-200:])/200,high20=max(r['high'] for r in last[-20:]),volume_ratio=sum(vol[-10:])/10/prev if prev else 0,turnover20=sum(r['close']*r['volume'] for r in last[-20:])/20,fundamentals=f)
   try:require_buy_quality(db,symbol,portfolio,clock);quality='pass'
   except ValueError as e:quality=str(e)
   buys=[dict(r) for r in db.execute("SELECT * FROM trades WHERE portfolio=? AND symbol=? AND side='BUY' ORDER BY rowid",(portfolio,symbol))]
   entry=buys[-1];execution=json.loads(entry['execution']);annual=f.get('latest') or {};summary=f.get('summary') or {}
   r=dict(portfolio=portfolio,symbol=symbol,quantity=p['quantity'],cost=p['cost'],mark=p['mark'],pnl=round(p['unrealized_pnl'],2),fresh=fresh(q,clock),trade_age_s=round(clock.timestamp()-q['trade_time']/1000,1),entry_at=entry['created_at'],entry_price=entry['price'],entry_fee=entry['fee'],entry_requested=entry['requested_quantity'],entry_reason=entry['reason'],entry_execution_status=execution.get('status'),entry_slippage_bps=execution.get('slippage_vs_ltp_bps'),gross_pnl=round(p['quantity']*(p['mark']-entry['price']),2),quality=quality,latest_daily=last[-1]['ts'],dma200=signal['dma200'],current_entry_pass=bool(entry_signal(portfolio,signal,p['mark'],clock)),financial_status=f.get('status'),source_conflicts=f.get('source_conflicts'),annual_period=annual.get('period'),source=f.get('selected_source'),growth_revenue=summary.get('revenue_growth_yoy'),growth_profit=summary.get('net_profit_growth_yoy'),annual={k:annual.get(k) for k in ('profit_before_tax','finance_costs','equity','total_borrowings','total_lease_liabilities','cash_and_cash_equivalents')})
   if portfolio=='investor':
    r['stop']=p['cost']/p['quantity']*.9;r['exit_now']=exit_signal(portfolio,signal,p,p['mark'])
   else:
    state=db.execute('SELECT * FROM claude_positions WHERE symbol=?',(symbol,)).fetchone();r['stop_state']=dict(state) if state else None
    r['exit_now']=bool(state and (p['mark']<=state['stop'] or state['ma_exit']))
    r['features']=features(rows)[-1]
   report['held_audit'].append(r)
 report['all_trades']=[dict(r) for r in db.execute('SELECT * FROM trades ORDER BY rowid')]
 # Validate every recorded depth fill using saved execution evidence (no new orders).
 report['execution_audit']=[]
 for t in report['all_trades']:
  ex=json.loads(t['execution']);book=ex.get('book');errors=[]
  if book:
   stamp=datetime.fromisoformat(t['created_at'])
   for field in ('timestamp','received_at'):
    age=(stamp-datetime.fromisoformat(book[field])).total_seconds()
    if not 0<=age<=5:errors.append(field+' age '+str(age))
   age=stamp.timestamp()-book['trade_time']/1000
   if not 0<=age<=30:errors.append('depth trade age '+str(age))
   if not 0<=stamp.timestamp()-t['quote_time']/1000<=30:errors.append('quote trade stale')
   if book['symbol']!=t['symbol']:errors.append('identity mismatch')
   if t['side']=='BUY' and t['quantity']*t['price']+t['fee']>100000.001:errors.append('position budget exceeded')
  else:errors.append('no saved depth')
  report['execution_audit'].append(dict(symbol=t['symbol'],portfolio=t['portfolio'],side=t['side'],time=t['created_at'],errors=errors))
Path('tmp/portfolio-audit/current.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k not in ('portfolios','all_trades','execution_audit')},indent=2))
print('PERFORMANCE',json.dumps([{k:v for k,v in a.items() if k not in ('positions','trades')} for a in accounts],indent=2))
print('EXECUTION_CHECKS',json.dumps(dict(fills=len(report['execution_audit']),failures=[r for r in report['execution_audit'] if r['errors']]),indent=2))
