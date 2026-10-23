"""Read-only payload for the Paper baskets screen: signals, positions with stops/targets/stage, news analysis,
state-machine log and performance metrics for each signal basket. Nothing here changes the ledger."""
import json,math,statistics
from datetime import datetime,timezone
from ..storage import connect,get_state
from .engine import params,rules,LIMITS,session
from .names import SIGNAL_BASKETS,TITLES,STRATEGY_VERSIONS,NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION,VINAY_INTRADAY,NEW_INTRADAY_BASKETS,SECTOR_RELATIVE_STRENGTH
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
        elif name==VINAY_INTRADAY:s['evidence']={k:d.get(k) for k in ('trigger_ts','day_change_pct','gap_pct','or_high','vwap','vwap_extension_pct','rvol','atr14','close_location','stop_pct','reasons')}
        elif name in NEW_INTRADAY_BASKETS:s['evidence']={k:d.get(k) for k in ('trigger_ts','vwap','atr14','rvol','depth_ratio','daily_ema50','level','opening_range_high','opening_range_low','opening_range_mid','swing_low','bb_upper','bb_lower','bb_width_pct','compressed','initial_stop','initial_target','stop_pct','reasons')}
        elif name==SECTOR_RELATIVE_STRENGTH:s['evidence']={k:d.get(k) for k in ('sector','momentum','sector_excess_return','avg_turnover','weekly_atr','as_of')}
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
            'trail_active','trail_trigger','trail_atr_mult','exit_now','candles_held','entry_atr','atr','best_close','best_price','average_cost','entry_score','last_review','last_score','pending_exit','adopted',
            'sector','momentum','sector_excess_return','weekly_atr','last_review_week')}))
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
        performance=performance(db,name,initial),runner=runner,checkpoints=dict(news=get_state(db,'signal_news_checkpoint',{}),ema=get_state(db,'signal_ema_checkpoint',{}),live=get_state(db,'signal_live_checkpoint',{}),sector=get_state(db,'sector_rotation_state',{}).get(name,{})),
        paper_only=True,limitations=LIMITATIONS[name])

LIMITATIONS={
 NEWS_MOMENTUM:['News comes from NSE corporate announcements only (no BSE, wires or social media); the feed is public but undocumented.',
   'Order values are read from the filing summary and the first PDF pages; margins and execution timelines are rarely disclosed and are flagged, never assumed.',
   'Confirmation waits for the completed news-session daily candle (refreshed after 06:00 IST), so entries happen at the earliest on the next session.',
   'Sector-wide effects and analyst expectations are not modeled. No LLM is connected; the analysis is rule-based.'],
 NEWS_CATALYST:['Fundamentals are the latest stored annual record; quarterly figures, free cash flow (capex), promoter holding and pledging are not available and are flagged.',
   'Valuation uses AMFI six-month average market cap scaled by price; it is an estimate.',
   'Catalyst and fundamental scores use fixed, explainable rules; no AI model is connected.',
   'The 3-24 month horizon cannot be validated by a backtest over the news history available.'],
 EMA_PRICE_ACTION:['Signals use Upstox 5-minute candles fetched for a liquidity-ranked universe (default 50 Nifty 500 stocks) to stay inside API rate limits.',
   'Live triggers, stops and targets are checked on last-trade observations every 15 seconds; fills use live depth, so exits can be worse than the stop (gaps, thin books).',
   'With the default hybrid stop (>= 1.5 ATR) and an ATR target capped at 3 ATR, risk-reward cannot exceed 2.0, and tick rounding pulls it just below 2.0, so a 2.0 minimum admits almost no trades. The default minimum is therefore 1.8 (configurable 1.5-2.0); setups still need the ATR stop to bind or a close structural stop.',
   'Intraday charges are an estimate (not a contract note). Shorts are research-only; the paper ledger is long-only.'],
 VINAY_INTRADAY:['No rule set can promise no losses: the 2.5% stop can fill lower on gaps and thin books, and most days the stock simply drifts until the 15:00 square-off.',
   'Scans every Nifty 500 stock on 5-minute candles built from the live LTPC feed: highs and lows are the ticks the feed delivered, volume is booked from a batch quote about 15 seconds after each candle closes, and candles exist only while the app was running from 09:15 (a later start skips that day). Relative volume (off by default) needs 3 prior sessions.',
   'Paper ledger is cash-only, so positions use the basket\'s own money (no MIS leverage). Intraday charges are an estimate, not a contract note.',
   'Long only: falling stocks are never shorted. Backtest (scripts/backtest_vinay_intraday.py, 5-minute candles 2022-2026, top 40 stocks a year plus Nifty 500 ranks 41-200 for 2025-2026): lost money after fees every year, -5% to -24%.'],
 **{n:[
   'Experimental paper strategy; no profitability is implied. Starts switched off and uses the same paper ledger, fresh live depth fill simulation and fee estimate as other MIS baskets.',
   'The source feed is LTPC. Five-minute bars are constructed only while MarketDesk is running; high/low reflect received ticks and volume is booked from a batch quote shortly after each candle closes.',
   'The local 2022–2026 candle archive covers 40 symbols/year in 2022–2024 and a larger but incomplete set in 2025–2026, not a point-in-time full Nifty 500. Historical order-book depth is unavailable; any order-flow backtest uses a clearly labeled candle proxy and is not an order-flow validation.',
   'Paper ledger is cash-only; intraday fees are estimates, not contract notes. Stops can slip on gaps or thin books; square-off requires a fresh executable paper quote.'
 ] for n in NEW_INTRADAY_BASKETS}
}
LIMITATIONS[SECTOR_RELATIVE_STRENGTH]=[
 'The local archive has Nifty 500 constituents and benchmark closes but no historical sector-index series. Sector returns are equal-weight constituent proxies, not official sector indices.',
 'Historical point-in-time Nifty 500 constituents and sector classifications are not available for all of 2022–2026; results have survivorship and classification limitations.',
 'The 20-day turnover filter is computed from stored daily close × volume. Paper fills require current verified Nifty 500 membership, fresh live prices and order-book depth.',
 'Delivery charges and DP fees are estimates, not contract notes. The system blocks a buy if the 0.1% round-trip cap or ₹500 monthly budget cannot be met at the 10% allocation.'
]

def snapshot(path,clock=None):
    clock=clock or datetime.now(timezone.utc)
    from .runner import clean
    with connect(path) as db:
        db.execute('BEGIN')
        return clean(dict(baskets=[basket_payload(db,n,clock) for n in SIGNAL_BASKETS],paper_only=True))
