from pathlib import Path

def replace_once(s,old,new):
 assert s.count(old)==1,old[:100]
 return s.replace(old,new,1)

def implement(s):
 s=replace_once(s,'from .paper import trade','from .paper import trade\nfrom .execution import charges,money')
 block='''def investor_stop_risk(cost):
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

'''
 s=replace_once(s,'def equity(db,portfolio,clock):',block+'def equity(db,portfolio,clock):')
 s=replace_once(s,"  for p in rules:\n",'''  cap=cfg.get('investor',{}).get('portfolio_risk_cap_pct')
  if cap is not None:
   rules['investor']['portfolio_risk_cap_pct']=cap
   base=rules['investor'].get('risk','A 2% daily equity loss stops new buys for that session; exits remain active.')
   rules['investor']['risk']=base+f' New buys must keep combined risk at the existing 10% stops within {cap}% of post-fill equity, including buy/sell fees and a 0.25%-per-side slippage reserve. Existing holdings remain until their stop or trend exit triggers.'
  for p in rules:
''')
 s=replace_once(s,"   if total>budget:raise ValueError('Position budget exceeded at the latest price.')\n",'''   if total>budget:raise ValueError('Position budget exceeded at the latest price.')
   if portfolio=='investor':
    risk=investor_portfolio_risk(db,execution_clock)
    mark=db.execute('SELECT price FROM quotes WHERE symbol=?',(order['symbol'],)).fetchone()[0]
    if risk and not investor_buy_fits(quote.get('fill_quantity',order['quantity']),price,mark,risk,quote.get('fill_fee')):
     raise ValueError(f"Investor portfolio risk cap ({risk['cap_pct']:g}%) exceeded including fees and slippage; new buy blocked. Exits remain active.")
''')
 s=replace_once(s,"  if not okay:return {'state':'risk_wait','message':risk_message,'fills_this_scan':fills}\n",'''  if not okay:return {'state':'risk_wait','message':risk_message,'fills_this_scan':fills}
  if portfolio=='investor':
   try:
    with connect(self.path) as db:risk=investor_portfolio_risk(db,datetime.now(timezone.utc) if realtime else clock)
   except ValueError as error:return {'state':'risk_wait','message':str(error),'fills_this_scan':fills}
   if risk and risk['risk']>=risk['limit']:
    return {'state':'risk_wait','message':f"Investor stop risk including fees and slippage is ₹{risk['risk']:.2f} ({risk['risk_pct']:.3f}%), above or at its {risk['cap_pct']:g}% cap of ₹{risk['limit']:.2f}. New buys wait; existing holdings retain their stops and trend exits.",'fills_this_scan':fills,'portfolio_risk':risk}
''')
 # Size only downward from the existing cash/position-budget quantity; final fill rechecks everything.
 s=replace_once(s,"   if qty>0:fills+=self.execute(portfolio,symbol,'BUY',qty,signals[symbol],clock,realtime)\n",'''   if portfolio=='investor' and qty>0:
    try:
     with connect(self.path) as db:risk=investor_portfolio_risk(db,datetime.now(timezone.utc) if realtime else clock)
    except ValueError as error:return {'state':'risk_wait','message':str(error),'fills_this_scan':fills}
    if risk:qty=investor_risk_quantity(qty,quotes[symbol]['price']*1.005,quotes[symbol]['price'],risk)
   if qty>0:fills+=self.execute(portfolio,symbol,'BUY',qty,signals[symbol],clock,realtime)
''')
 return s

b=Path('tmp/investor-portfolio-cap-change')
p=Path('marketdesk/autotrading.py')
assert p.read_bytes()==(b/'workspace.before/marketdesk/autotrading.py').read_bytes(),'Workspace changed; inspect first'
p.write_text(implement(p.read_text()))
(b/'runtime.candidate.py').write_text(implement((b/'runtime.before.py').read_text()))
