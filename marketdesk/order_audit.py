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
    if portfolio=='trader' and cfg.get('strategy')=='claude-original-v2':
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
