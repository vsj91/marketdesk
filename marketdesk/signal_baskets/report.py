"""Read-only payload for the Paper baskets screen: signals, positions with stops/targets/stage, news analysis,
state-machine log and performance metrics for each signal basket. Nothing here changes the ledger."""
import json,math,statistics
from datetime import datetime,timezone
from ..storage import connect,get_state
from .engine import params,rules,LIMITS,session
from .names import SIGNAL_BASKETS,TITLES,STRATEGY_VERSIONS,NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION
from .runner import STATUS_KEY

def round_trips(trades):
    """Closed round trips from a basket's trades (oldest first) using average cost including buy fees."""
    book={};closed=[]
    for t in trades:
        b=book.setdefault(t['symbol'],dict(qty=0,cost=0.,fees=0.,realized=0.,opened=None,buys=0))
        if t['side']=='BUY':
            if b['qty']==0:b.update(opened=t['created_at'],realized=0.,fees=0.,invested=0.)
            b['qty']+=t['quantity'];b['cost']+=t['quantity']*t['price']+t['fee'];b['fees']+=t['fee'];b['invested']=b.get('invested',0)+t['quantity']*t['price']
        elif b['qty']>0:
            q=min(t['quantity'],b['qty']);avg=b['cost']/b['qty']
            b['realized']+=q*t['price']-t['fee']-avg*q;b['fees']+=t['fee'];b['cost']-=avg*q;b['qty']-=q
            if b['qty']==0:
                minutes=(datetime.fromisoformat(t['created_at'])-datetime.fromisoformat(b['opened'])).total_seconds()/60
                closed.append(dict(symbol=t['symbol'],opened=b['opened'],closed=t['created_at'],pnl=round(b['realized'],2),fees=round(b['fees'],2),
                    invested=round(b['invested'],2),return_pct=b['realized']/b['invested']*100 if b['invested'] else None,holding_minutes=round(minutes,1)))
                b['cost']=0.
    return closed

def performance(db,name,initial):
    trades=[dict(r) for r in db.execute('SELECT symbol,side,quantity,price,fee,created_at FROM trades WHERE portfolio=? ORDER BY created_at',(name,))]
    closed=round_trips(trades);wins=[c['pnl'] for c in closed if c['pnl']>0];losses=[c['pnl'] for c in closed if c['pnl']<=0]
    curve=[dict(r) for r in db.execute('SELECT ts,equity,cash,invested FROM sb_equity WHERE portfolio=? ORDER BY ts',(name,))]
    peak=initial;mdd=0.
    for p in curve:peak=max(peak,p['equity']);mdd=max(mdd,(1-p['equity']/peak)*100)
    daily={}
    for p in curve:daily[p['ts'][:10]]=p['equity']
    series=[initial]+list(daily.values());rets=[b/a-1 for a,b in zip(series,series[1:]) if a>0]
    sharpe=round(statistics.mean(rets)/statistics.stdev(rets)*math.sqrt(252),2) if len(rets)>=20 and statistics.stdev(rets)>0 else None
    gross_win=sum(wins);gross_loss=-sum(losses)
    out=dict(trades=len(closed),wins=len(wins),losses=len(losses),win_rate_pct=round(len(wins)/len(closed)*100,1) if closed else None,
        average_win=round(gross_win/len(wins),2) if wins else None,average_loss=round(sum(losses)/len(losses),2) if losses else None,
        profit_factor=round(gross_win/gross_loss,2) if gross_loss>0 else (None if not wins else math.inf),expectancy=round(sum(c['pnl'] for c in closed)/len(closed),2) if closed else None,
        realized_pnl=round(sum(c['pnl'] for c in closed),2),fees=round(sum(t['fee'] for t in trades),2),max_drawdown_pct=round(mdd,2),
        sharpe=sharpe,sharpe_note=None if sharpe is not None else f'Needs 20+ daily returns ({len(rets)} so far)',
        average_holding_minutes=round(sum(c['holding_minutes'] for c in closed)/len(closed),1) if closed else None,
        capital_utilization_pct=round(sum(p['invested']/p['equity'] for p in curve if p['equity'])/len(curve)*100,1) if curve else None,
        closed_trades=closed[-50:][::-1])
    if out['profit_factor']==math.inf:out['profit_factor']=None;out['profit_factor_note']='No losing trades yet'
    step=max(1,len(curve)//240);out['equity_curve']=[dict(ts=p['ts'],equity=round(p['equity'],2)) for p in curve[::step]]
    return out

def basket_payload(db,name,clock):
    cfg=get_state(db,'auto_config',{}).get(name,{});p=params(name,cfg)
    initial,cash=db.execute('SELECT initial_cash,cash FROM portfolios WHERE name=?',(name,)).fetchone()
    today=session(clock)
    signals=[]
    for r in db.execute('SELECT * FROM sb_signals WHERE portfolio=? ORDER BY updated_at DESC LIMIT 80',(name,)):
        s=dict(r);d=json.loads(s.pop('details') or '{}')
        if name==EMA_PRICE_ACTION:s['evidence']={k:d.get(k) for k in ('trigger_ts','ema9','ema21','ema200','rsi14','atr14','atr_pct','structural_level','structural_stop','atr_stop','atr_target','resistance_level','structural_target','reasons')}
        else:
            a=d.get('analysis') or d.get('catalyst') or {}
            s['evidence']=dict(category=a.get('category'),label=a.get('label'),impact=a.get('impact_score'),confidence=a.get('confidence'),components=a.get('components'),
                missing=a.get('missing'),notes=a.get('notes'),plan=d.get('plan'),reasons=d.get('reasons') or d.get('confirmation_reasons'),
                score=d.get('score'),profile=d.get('profile'))
        signals.append(s)
    counts={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM sb_signals WHERE portfolio=? AND session=? GROUP BY status',(name,today))}
    positions=[]
    for r in db.execute('SELECT s.*,q.price FROM sb_positions s LEFT JOIN quotes q USING(symbol) WHERE s.portfolio=?',(name,)):
        st=json.loads(r['state']);positions.append(dict(symbol=r['symbol'],stage=r['stage'],entry_session=r['entry_session'],entry_at=r['entry_at'],signal_id=r['signal_id'],
            price=r['price'],**{k:st.get(k) for k in ('entry','qty','original_qty','stop','initial_stop','target','target_active','be_trigger','partial_trigger','partial_done',
            'trail_active','entry_atr','atr','best_close','best_price','average_cost','entry_score','last_review','last_score','pending_exit','adopted')}))
    log=[dict(r) for r in db.execute('SELECT * FROM sb_transitions WHERE portfolio=? ORDER BY id DESC LIMIT 100',(name,))]
    news=[]
    if name in (NEWS_MOMENTUM,NEWS_CATALYST):
        key='momentum_eligible' if name==NEWS_MOMENTUM else 'catalyst_eligible'
        for r in db.execute(f'''SELECT n.id,n.symbol,n.company,n.subject,n.headline,n.url,n.published_at,n.retrieved_at,n.verification,a.category,a.label,a.impact_score,a.confidence,a.details
            FROM sb_news n JOIN sb_news_analysis a ON a.news_id=n.id WHERE n.duplicate_of IS NULL AND a.category NOT IN ('routine','other')
            AND (json_extract(a.details,'$.{key}')=1 OR a.label LIKE '%Negative%') ORDER BY n.published_at DESC LIMIT 40'''):
            x=dict(r);d=json.loads(x.pop('details'));x.update(components=d.get('components'),missing=d.get('missing'),notes=d.get('notes'),amount_crore=d.get('amount_crore'),materiality_ratio=d.get('materiality_ratio'));news.append(x)
    runner=get_state(db,STATUS_KEY,{})
    return dict(name=name,title=TITLES[name],version=STRATEGY_VERSIONS[name],enabled=bool(cfg.get('enabled')),capital=initial,cash=cash,params=p,
        limits={k:list(v) for k,v in LIMITS[name].items()},rules=rules(name,cfg),signals=signals,today=counts,positions=positions,transitions=log,news=news,
        performance=performance(db,name,initial),runner=runner,checkpoints=dict(news=get_state(db,'signal_news_checkpoint',{}),ema=get_state(db,'signal_ema_checkpoint',{})),
        paper_only=True,limitations=LIMITATIONS[name])

LIMITATIONS={
 NEWS_MOMENTUM:['News comes from NSE corporate announcements only (no BSE, wires or social media); the feed is public but undocumented.',
   'Order values are read from the filing summary and the first PDF pages; margins and execution timelines are rarely disclosed and are flagged, never assumed.',
   'Confirmation waits for the completed news-session daily candle (refreshed after 06:00 IST), so entries happen at the earliest on the next session.',
   'Sector-wide effects and analyst expectations are not modeled. No LLM is connected; the analysis is rule-based.'],
 NEWS_CATALYST:['Fundamentals are the latest stored annual record; quarterly figures, free cash flow (capex), promoter holding and pledging are not available and are flagged.',
   'Valuation uses AMFI six-month average market cap scaled by price; it is an estimate.',
   'The 3-24 month horizon cannot be validated by a backtest over the news history available.'],
 EMA_PRICE_ACTION:['Signals use Upstox 5-minute candles fetched for a liquidity-ranked universe (default 50 Nifty 500 stocks) to stay inside API rate limits.',
   'Live triggers, stops and targets are checked on last-trade observations every 15 seconds; fills use live depth, so exits can be worse than the stop (gaps, thin books).',
   'With the default hybrid stop (>= 1.5 ATR) and an ATR target capped at 3 ATR, risk-reward cannot exceed 2.0, and tick rounding pulls it just below 2.0, so a 2.0 minimum admits almost no trades. The default minimum is therefore 1.8 (configurable 1.5-2.0); setups still need the ATR stop to bind or a close structural stop.',
   'Intraday charges are an estimate (not a contract note). Shorts are research-only; the paper ledger is long-only.'],
}

def snapshot(path,clock=None):
    clock=clock or datetime.now(timezone.utc)
    from .runner import clean
    with connect(path) as db:
        db.execute('BEGIN')
        return clean(dict(baskets=[basket_payload(db,n,clock) for n in SIGNAL_BASKETS],paper_only=True))
