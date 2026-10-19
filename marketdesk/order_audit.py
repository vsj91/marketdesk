"""Additive order provenance. Original ledger rows are never rewritten."""
import hashlib,json
from .storage import get_state

CONFIG_KEYS=('strategy','nifty500_filter_enabled','full_session_buying','live_breakout',
 'max_positions','max_buys_per_day','allow_stale_held_valuation','max_round_trip_fee_pct',
 'portfolio_risk_cap_pct','buy_target_inr','buy_universe')

def legacy_identity(t):
    reason=t.get('reason','');automatic=t.get('source')=='automatic'
    if reason.startswith('User-requested'):
        return 'user-directed/legacy','User-directed adjustment',{'evidence':'recorded reason; historical configuration unknown'}
    if automatic and t.get('request_id','').startswith('claude-') and reason.startswith(('Original Claude V2','Claude V2 (Nifty 500 filter off)')):
        live=' live breakout ' in reason;off='filter off' in reason;full='full-session buying' in reason
        key='claude-v2/'+('live' if live else 'completed')+'/'+('filter-off' if off else 'filter-on')+'/'+('full-session' if full else 'window-unknown')+'/legacy'
        label='Claude V2 · '+('live breakout' if live else 'completed breakout')+' · '+('index filter off' if off else 'index filter on')
        return key,label,{'evidence':'recorded automatic request ID and reason','historical_configuration':'unknown','full_session_buying':full}
    if automatic and t.get('request_id','').startswith('auto-investor-'):
        return 'investor-fundamental/legacy','Investor fundamentals · legacy',{'evidence':'automatic request prefix','historical_configuration':'unknown'}
    return ('manual/legacy','Manual · legacy',{'evidence':'manual source'}) if not automatic else ('automatic-unknown/legacy','Automatic · unidentified legacy',{'evidence':'insufficient strategy evidence'})

def current_identity(db,portfolio,automatic):
    if not automatic:return 'manual/v1','Manual paper order',{'mode':'manual'}
    cfg=get_state(db,'auto_config',{}).get(portfolio,{})
    from .autotrading import capacity_rules
    from .basket_rules import BASKETS
    from .quant.names import LIVE_BASKETS
    if portfolio in BASKETS:
        from .baskets import rules as basket_rules
        rules=basket_rules(portfolio,cfg);family='basket-'+portfolio
    elif portfolio in LIVE_BASKETS:
        from .quant.live import rules as quant_rules
        rules=quant_rules(portfolio,cfg);family='quant-'+portfolio
    elif portfolio=='trader' and cfg.get('strategy')=='claude-original-v2':
        from .claude_trader import active_rule
        rules=active_rule(cfg);family='claude-v2'
    else:rules=capacity_rules(portfolio,cfg);family=portfolio+'-fundamental' if portfolio=='investor' else 'trader-daily'
    definition={'schema':1,'rules':rules,'config':{k:cfg[k] for k in CONFIG_KEYS if k in cfg}}
    digest=hashlib.sha256(json.dumps(definition,sort_keys=True).encode()).hexdigest()[:16]
    return family+'/'+digest,rules['name'],definition

def save_identity(db,trade_id,identity,provenance):
    key,label,definition=identity
    db.execute('INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?)',(key,label,json.dumps(definition,sort_keys=True)))
    db.execute('INSERT OR IGNORE INTO order_strategy VALUES(?,?,?)',(trade_id,key,provenance))

def init_metadata(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS strategy_versions(id TEXT PRIMARY KEY,label TEXT NOT NULL,definition TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_strategy(trade_id TEXT PRIMARY KEY REFERENCES trades(id),strategy_version_id TEXT NOT NULL REFERENCES strategy_versions(id),provenance TEXT NOT NULL);''')
    for row in db.execute('SELECT t.* FROM trades t LEFT JOIN order_strategy m ON m.trade_id=t.id WHERE m.trade_id IS NULL').fetchall():
        t=dict(row);save_identity(db,t['id'],legacy_identity(t),'legacy inference; original ledger unchanged')

def annotate(db,trades):
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='order_strategy'").fetchone()
    saved={r['trade_id']:dict(r) for r in db.execute('SELECT m.*,v.label FROM order_strategy m JOIN strategy_versions v ON v.id=m.strategy_version_id')} if exists else {}
    for t in trades:
        record=saved.get(t.get('id'))
        if record:t.update(strategy_version_id=record['strategy_version_id'],strategy_label=record['label'],strategy_provenance=record['provenance'])
        else:
            key,label,_=legacy_identity(t);t.update(strategy_version_id=key,strategy_label=label,strategy_provenance='read-only legacy inference')
    return trades


def init_attempts(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS order_attempts(
      id TEXT PRIMARY KEY,request_id TEXT,created_at TEXT NOT NULL,portfolio TEXT,symbol TEXT,side TEXT,
      strategy_version_id TEXT NOT NULL,status TEXT NOT NULL,details TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS auto_event_evidence(event_id INTEGER PRIMARY KEY,details TEXT NOT NULL);''')


def blocked_category(reason):
    text=reason.lower()
    if 'fees' in text and ('too small' in text or 'exceed' in text and 'fill value' in text):return 'fee_floor'
    if 'sector' in text:return 'sector_cap'
    if 'risk' in text or 'heat' in text:return 'risk_budget'
    if 'depth' in text:return 'depth'
    if 'cash' in text:return 'cash'
    if 'budget' in text or 'position cap' in text:return 'position_cap'
    if 'limit' in text and ('position' in text or 'holdings' in text):return 'capacity'
    if 'breakout no longer confirmed' in text:return 'signal_confirmation'
    if 'stale' in text or 'fresh' in text or 'price' in text and '30 seconds' in text:return 'price_freshness'
    return 'other'


def diagnostics(db,order,fill,guard,clock,context=None):
    """Observe existing formulas only. Never set quantities, stops or eligibility."""
    from .execution import charges,money
    result=dict(context or {})
    q=fill['quantity'];price=fill['price'];fee=charges(fill['value'],order['side'],order['side']=='SELL')['total']
    result.update(requested_quantity=order['quantity'],filled_quantity=0,matched_quantity=q,
      fill_price=price,depth_reduced=q<order['quantity'],risk_used_inr=None,risk_pct_equity=None,
      binding_constraints=list(result.get('binding_constraints',[])))
    if q<order['quantity']:result['binding_constraints'].append('depth')
    if guard is None:
        result['risk_evidence']='Manual order: no strategy stop supplied';return result
    signal=getattr(guard,'audit_signal',None);portfolio=order['portfolio']
    account=db.execute('SELECT * FROM portfolios WHERE name=?',(portfolio,)).fetchone()
    try:
        if portfolio=='trader' and getattr(guard,'audit_strategy',None)=='claude-original-v2':
            from .claude_trader import limits,holding_stop_risk
            eq,heat,budget=limits(db,account,clock)
            result.update(equity_before=eq,portfolio_risk_before=heat,portfolio_risk_cap_pct=3,position_budget=budget)
            if order['side']=='BUY' and signal:
                distance=2*signal['atr'];risk=q*distance+fee+charges(q*(price-distance),'SELL',True)['total']+q*price*.005
                result.update(stop_distance=distance,stop_price=price-distance,risk_used_inr=risk,risk_pct_equity=risk/eq*100)
                # Which existing constraints would reject one additional share at this fill price?
                n=q+1;buy=charges(n*price,'BUY')['total'];nrisk=n*distance+buy+charges(n*(price-distance),'SELL',True)['total']+n*price*.005
                mark=db.execute('SELECT price FROM quotes WHERE symbol=?',(order['symbol'],)).fetchone()[0]
                if nrisk>.005*eq:result['binding_constraints'].append('risk_per_trade')
                if heat+nrisk>.03*(eq-buy+n*(mark-price)):result['binding_constraints'].append('risk_budget')
                if n*price+buy>budget:result['binding_constraints'].append('cash' if account['cash']<=account['initial_cash']*.1 else 'position_cap')
            elif order['side']=='SELL':
                p=db.execute('SELECT * FROM positions WHERE portfolio=? AND symbol=?',(portfolio,order['symbol'])).fetchone()
                s=db.execute('SELECT * FROM claude_positions WHERE symbol=?',(order['symbol'],)).fetchone()
                if p and s:
                    left=dict(p);left.update(quantity=p['quantity']-q,cost=money(p['cost']*(p['quantity']-q)/p['quantity']))
                    released=holding_stop_risk(p,s)-(holding_stop_risk(left,s) if left['quantity'] else 0)
                    result.update(stop_price=s['stop'],risk_released_inr=released,risk_used_inr=0,risk_pct_equity=0)
        elif portfolio=='investor':
            from .autotrading import investor_portfolio_risk,investor_stop_risk,investor_buy_fits
            r=investor_portfolio_risk(db,clock)
            if r:result.update(equity_before=r['equity'],portfolio_risk_before=r['risk'],portfolio_risk_cap_pct=r['cap_pct'])
            if order['side']=='BUY':
                cost=fill['value']+fee;risk=investor_stop_risk(cost)
                result.update(stop_price=cost/q*.9,stop_distance=cost/q*.1,risk_used_inr=risk,risk_pct_equity=risk/r['equity']*100 if r else None)
                if r and not investor_buy_fits(q+1,price,price,r):result['binding_constraints'].append('risk_budget')
            else:
                p=db.execute('SELECT * FROM positions WHERE portfolio=? AND symbol=?',(portfolio,order['symbol'])).fetchone()
                if p:
                    remaining=money(p['cost']*(p['quantity']-q)/p['quantity'])
                    result.update(stop_price=p['cost']/p['quantity']*.9,risk_used_inr=0,risk_pct_equity=0,
                      risk_released_inr=investor_stop_risk(p['cost'])-(investor_stop_risk(remaining) if remaining else 0))
        result['risk_evidence']='Observed before execution using existing strategy formulas'
    except (ValueError,KeyError,TypeError,ZeroDivisionError) as error:
        result['risk_evidence']='Unavailable: '+str(error)
    result['binding_constraints']=list(dict.fromkeys(result['binding_constraints']))
    result['constraint_basis']='Scan sizing reductions plus constraints rejecting one more share at the matched price; simultaneous constraints retained'
    return result


def save_attempt(db,order,identity,status,details,clock):
    import uuid
    key,label,definition=identity
    db.execute('INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?)',(key,label,json.dumps(definition,sort_keys=True)))
    db.execute('INSERT INTO order_attempts VALUES(?,?,?,?,?,?,?,?,?)',
      (str(uuid.uuid4()),order.get('request_id'),clock.isoformat(),order.get('portfolio'),order.get('symbol'),order.get('side'),key,status,json.dumps(details,sort_keys=True)))
