"""Decision rules for the two news baskets. Pure functions; the live engine and the backtest share them.

News Momentum Trader (event-driven swing, 1-20 sessions)
  A verified positive filing becomes a candidate. It is confirmed on the first COMPLETED daily session at or
  after publication (relative volume, positive reaction that is not already priced in, EMA20 > EMA50 trend,
  ATR band, relative strength against Nifty 500, market regime, room to the next resistance). A confirmed
  candidate buys during the NEXT session at a live price inside the gap limit. An AI/impact score alone never trades.

News Catalyst Investor (fundamental event-driven, 3-24 months)
  A verified business catalyst plus an explainable Investor Opportunity Score (catalyst 25, earnings growth 20,
  financial quality 20, valuation 15, management/execution 10, risk 10; initial hypotheses, not fitted weights).
  Missing fundamentals are flagged and excluded from the weighted mean; critical gaps block buys. Holdings are
  reviewed for thesis failure, deterioration, excessive valuation, governance events and concentration; negative
  sentiment alone never sells."""
import math,statistics
from .indicators import ema_series,atr_series,finite

MOMENTUM_DEFAULTS=dict(min_impact=60.,min_confidence=.5,max_news_age_hours=36.,min_turnover_cr=5.,min_price=30.,
    min_rel_volume=1.5,max_reaction_pct=10.,max_run_up_pct=15.,min_atr_pct=1.,max_atr_pct=6.,require_regime=True,
    # A 2-ATR stop and a 4-ATR target cap risk-reward at exactly 2.0, so a 2.0 minimum rejected every setup with
    # any overhead resistance; 1.8 (as in the EMA basket) leaves room for a resistance at least 3.6 ATR away.
    stop_atr_mult=2.,target_atr_mult=4.,min_rr=1.8,trail_atr_mult=2.5,resistance_lookback=120,
    risk_pct=1.,max_position_pct=15.,max_positions=6,max_per_sector=2,max_entries_per_day=3,max_gap_pct=4.,
    max_hold_sessions=20,follow_through_sessions=5,signal_expiry_sessions=2,exit_on_negative_news=True,min_exit_impact=50.,
    slippage_bps=10.,max_slippage_pct=.5)
MOMENTUM_LIMITS=dict(min_impact=(40.,90.),min_rel_volume=(1.,5.),max_reaction_pct=(2.,20.),stop_atr_mult=(1.,4.),target_atr_mult=(2.,8.),
    min_rr=(1.5,3.),risk_pct=(.1,2.),max_position_pct=(5.,30.),max_positions=(1,12),max_per_sector=(1,5),max_hold_sessions=(1,20),
    max_gap_pct=(1.,10.),require_regime=(True,False),exit_on_negative_news=(True,False),trail_atr_mult=(1.,5.),max_entries_per_day=(1,6))

CATALYST_DEFAULTS=dict(weights=dict(catalyst=25,earnings=20,quality=20,valuation=15,management=10,risk=10),
    min_catalyst_impact=55.,min_score=65.,min_coverage=.75,max_debt_equity=1.,min_interest_cover=3.,min_roe=10.,max_pe=60.,
    min_turnover_cr=2.,min_price=20.,require_uptrend=True,catalyst_lookback_days=90,max_positions=10,position_pct=10.,
    max_sector_pct=30.,max_entries_per_day=2,signal_expiry_sessions=5,review_days=7,exit_score=40.,exit_pe=90.,
    catalyst_review_days=270,hard_stop_pct=25.,reduce_sector_pct=40.,max_slippage_pct=.5)
CATALYST_LIMITS=dict(min_score=(50.,90.),min_catalyst_impact=(40.,90.),max_debt_equity=(.2,3.),min_interest_cover=(1.,10.),min_roe=(0.,30.),
    max_pe=(10.,150.),max_positions=(1,20),position_pct=(2.,20.),max_sector_pct=(10.,60.),review_days=(1,90),exit_score=(10.,60.),
    exit_pe=(20.,200.),hard_stop_pct=(10.,50.),require_uptrend=(True,False),max_entries_per_day=(1,5))

def merged(defaults,cfg):return {**defaults,**{k:v for k,v in (cfg or {}).items() if k in defaults}}

# ---- shared daily context -------------------------------------------------------------------------

def daily_context(bars,bench=None):
    """Indicators from completed daily candles (oldest first): EMA20/50, DMA200, ATR14, turnover, volume, RS."""
    if len(bars)<60:return None
    closes=[b['close'] for b in bars]
    e20=ema_series(closes,20)[-1];e50=ema_series(closes,50)[-1];atr=atr_series(bars,14)[-1]
    w=min(200,len(closes));rets=[math.log(b/a) for a,b in zip(closes[-61:],closes[-60:]) if a>0 and b>0]
    out=dict(as_of=str(bars[-1].get('ts',''))[:10],close=closes[-1],prev_close=closes[-2],ema20=e20,ema50=e50,dma200=sum(closes[-w:])/w,
        atr=atr,atr_pct=atr/closes[-1]*100 if atr else None,turnover20_cr=sum(b['close']*b['volume'] for b in bars[-20:])/20/1e7,
        volume=bars[-1]['volume'],avg_volume20=sum(b['volume'] for b in bars[-21:-1])/20,
        ret20=closes[-1]/closes[-21]-1,vol60=statistics.pstdev(rets)*math.sqrt(250) if len(rets)>=20 else None,
        high252=max(b['high'] for b in bars[-252:]))
    out['rel_volume']=out['volume']/out['avg_volume20'] if out['avg_volume20']>0 else None
    if bench and len(bench)>=51:
        out['bench_ret20']=bench[-1]/bench[-21]-1;out['regime_ok']=bench[-1]>sum(bench[-50:])/50
        out['relative_strength']=out['ret20']-out['bench_ret20']
    else:out.update(bench_ret20=None,regime_ok=None,relative_strength=None)
    return out

def resistance_above(bars,price,lookback):
    """Highest high of the lookback window when it sits above `price` (overhead supply), else None."""
    window=bars[-lookback-1:-1] if len(bars)>lookback else bars[:-1]
    high=max((b['high'] for b in window),default=None)
    return high if high and high>price else None

# ---- News Momentum ---------------------------------------------------------------------------------

def momentum_screen(analysis,cfg):
    """First pass on the news alone: REJECTED or WATCHLIST (awaiting market confirmation)."""
    cfg=merged(MOMENTUM_DEFAULTS,cfg);r=[]
    if analysis['verification']!='verified':r.append('Failed source/identity verification: '+analysis['verification'])
    if analysis['direction']<=0:r.append(f"{analysis['label']} event ({analysis['category']})")
    elif not analysis['momentum_eligible']:r.append(f"Category {analysis['category']} is not a momentum catalyst")
    if analysis['age_hours']<0:r.append('Publication time is in the future; rejected')
    if analysis['age_hours']>cfg['max_news_age_hours']:r.append(f"Stale news: {analysis['age_hours']:.0f}h old (limit {cfg['max_news_age_hours']:g}h)")
    if analysis['impact_score']<cfg['min_impact']:r.append(f"Impact score {analysis['impact_score']:.0f} below {cfg['min_impact']:g}")
    if analysis['confidence']<cfg['min_confidence']:r.append(f"Confidence {analysis['confidence']:.2f} below {cfg['min_confidence']:g}")
    if analysis['components'].get('novelty',100)<=10:r.append('Previously announced information (not new)')
    if analysis['category'] in ('order_win','acquisition','capacity_expansion') and analysis.get('materiality_ratio') is None:
        r.append('Financial materiality unknown (event value or revenue missing)')
    return ('REJECTED',r) if r else ('WATCHLIST',['Awaiting a completed session to confirm volume and price reaction'])

def momentum_confirm(analysis,ctx,bars,cfg,sector_count=0):
    """Confirmation on the completed news session. Returns (status, reasons, plan)."""
    cfg=merged(MOMENTUM_DEFAULTS,cfg);r=[]
    if not ctx:return 'REJECTED',['Not enough completed daily history (60 sessions)'],None
    if ctx['close']<cfg['min_price']:r.append(f"Price {ctx['close']:.2f} below {cfg['min_price']:g}")
    if ctx['turnover20_cr']<cfg['min_turnover_cr']:r.append(f"Liquidity: 20-day turnover ₹{ctx['turnover20_cr']:.1f} cr below {cfg['min_turnover_cr']:g} cr")
    reaction=(ctx['close']/ctx['prev_close']-1)*100
    if reaction<=0:r.append(f'No positive price reaction on the news session ({reaction:+.2f}%)')
    elif reaction>cfg['max_reaction_pct']:r.append(f"Already priced in: {reaction:+.1f}% on the news session (limit {cfg['max_reaction_pct']:g}%)")
    run_up=analysis.get('run_up_pct')
    if run_up is not None and run_up>cfg['max_run_up_pct']:r.append(f'Already rallied {run_up:+.1f}% in the 5 sessions before the news')
    if ctx['rel_volume'] is None or ctx['rel_volume']<cfg['min_rel_volume']:r.append(f"Relative volume {ctx['rel_volume'] or 0:.2f}x below {cfg['min_rel_volume']:g}x")
    if not (ctx['close']>ctx['ema20']>ctx['ema50']):r.append('Trend not confirmed (needs close > EMA20 > EMA50)')
    if not ctx['atr'] or not cfg['min_atr_pct']<=ctx['atr_pct']<=cfg['max_atr_pct']:r.append(f"ATR {ctx['atr_pct'] or 0:.2f}% outside {cfg['min_atr_pct']:g}-{cfg['max_atr_pct']:g}%")
    if ctx['relative_strength'] is None:r.append('Nifty 500 benchmark unavailable for relative strength')
    elif ctx['relative_strength']<0:r.append(f"Weaker than Nifty 500 over 20 sessions ({ctx['relative_strength']*100:+.1f} pts)")
    if cfg['require_regime'] and ctx['regime_ok'] is False:r.append('Market regime filter: Nifty 500 below its 50-day average')
    if sector_count>=cfg['max_per_sector']:r.append(f"Sector exposure: already {sector_count} position(s) in this sector")
    plan=None
    if ctx['atr']:
        entry=ctx['close'];stop=entry-cfg['stop_atr_mult']*ctx['atr'];target=entry+cfg['target_atr_mult']*ctx['atr']
        res=resistance_above(bars,entry,cfg['resistance_lookback'])
        if res and res<target:target=res
        rr=(target-entry)/(entry-stop)
        plan=dict(reference=entry,stop=stop,target=target,risk_reward=rr,resistance=res,atr=ctx['atr'],reaction_pct=reaction,rel_volume=ctx['rel_volume'])
        if rr<cfg['min_rr']-1e-9:r.append(f"Risk-reward {rr:.2f} below {cfg['min_rr']:g} (resistance at {res:.2f})" if res else f'Risk-reward {rr:.2f} below {cfg["min_rr"]:g}')
    return ('REJECTED' if r else 'BUY_CANDIDATE'),r,plan

def momentum_exit(position,price,ctx,held_sessions,negative_news,cfg):
    """Reason to exit a momentum holding, or None. `position` carries entry, stop, target, atr, best_close."""
    cfg=merged(MOMENTUM_DEFAULTS,cfg)
    if price<=position['stop']:return 'STOP_LOSS',f"Price {price:.2f} at/below stop {position['stop']:.2f}"
    if price>=position['target']:return 'TARGET',f"Target {position['target']:.2f} reached"
    if negative_news and cfg['exit_on_negative_news']:return 'NEWS_INVALIDATION',negative_news
    if ctx and ctx['as_of']>position['entry_session'] and ctx['close']<ctx['ema20']:return 'TREND_FAILURE',f"Completed close {ctx['close']:.2f} below EMA20 {ctx['ema20']:.2f}"
    if held_sessions>=cfg['max_hold_sessions']:return 'TIME_EXIT',f"Held {held_sessions} sessions (maximum {cfg['max_hold_sessions']})"
    risk=position['entry']-position['initial_stop']
    if held_sessions>=cfg['follow_through_sessions'] and position.get('best_close',position['entry'])<position['entry']+risk:
        return 'NO_FOLLOW_THROUGH',f"No close above +1R ({position['entry']+risk:.2f}) within {cfg['follow_through_sessions']} sessions"
    return None

def momentum_trail(position,ctx,cfg):
    """Chandelier trail on completed closes: stop = max(stop, best close - k x ATR). Never loosens."""
    cfg=merged(MOMENTUM_DEFAULTS,cfg)
    if not ctx or ctx['as_of']<=position['entry_session']:return None
    best=max(position.get('best_close',position['entry']),ctx['close']);position['best_close']=best
    candidate=best-cfg['trail_atr_mult']*position['atr']
    if candidate>position['stop']:old=position['stop'];position['stop']=round(candidate,2);return f'Trailing stop {old:.2f} -> {position["stop"]:.2f}'
    return None

# ---- News Catalyst Investor ------------------------------------------------------------------------

def band(x,steps):
    for limit,score in steps:
        if x>=limit:return score
    return steps[-1][1]

def fundamental_profile(details,financial,price=None,market_cap_cr=None,operating_cash=None):
    """Explainable metrics from the stored annual record. Every unavailable field is listed in `missing`."""
    latest=(details or {}).get('latest') or {};summary=(details or {}).get('summary') or {};missing=[];na=[]
    g=lambda d,k:d.get(k) if finite(d.get(k)) else None
    revenue,profit=g(summary,'revenue'),g(summary,'net_profit')
    rg,pg=g(summary,'revenue_growth_yoy'),g(summary,'net_profit_growth_yoy')
    equity,pbt,fc=g(latest,'equity'),g(latest,'profit_before_tax'),g(latest,'finance_costs')
    debt=g(latest,'total_borrowings');lease=g(latest,'total_lease_liabilities')
    if debt is not None and lease is not None:debt+=lease
    p=dict(period=latest.get('period') or summary.get('revenue_period'),period_end=latest.get('period_end'),revenue=revenue,net_profit=profit,
        revenue_growth=rg,profit_growth=pg,equity=equity,debt=debt,operating_cash=operating_cash,financial=financial)
    for k,v in (('revenue',revenue),('net profit',profit),('equity',equity),('revenue growth',rg),('profit growth',pg)):
        if v is None:missing.append(k)
    p['roe']=profit/equity*100 if profit is not None and equity and equity>0 else None
    p['net_margin']=profit/revenue*100 if profit is not None and revenue else None
    if financial:na+=['debt/equity','interest cover','ROCE']
    else:
        p['debt_equity']=debt/equity if debt is not None and equity and equity>0 else None
        p['interest_cover']=(pbt+fc)/fc if pbt is not None and fc else (math.inf if fc==0 and pbt is not None else None)
        p['roce']=(pbt+fc)/(equity+debt)*100 if None not in (pbt,fc,equity,debt) and equity+debt>0 else None
        for k in ('debt_equity','interest_cover','roce'):
            if p[k] is None:missing.append(k.replace('_','/'))
    p['pe']=market_cap_cr/profit if market_cap_cr and profit and profit>0 else (math.inf if market_cap_cr and profit is not None and profit<=0 else None)
    if market_cap_cr is None:missing.append('market cap (valuation)')
    if operating_cash is None:missing.append('operating cash flow')
    missing+=['free cash flow (capex not in the stored record)','promoter holding and pledging (no data source connected)']
    p.update(missing=missing,not_applicable=na,critical_gaps=[k for k in ('revenue','net profit','equity') if k in missing])
    return p

def opportunity_score(catalyst,profile,ctx,cfg):
    """Investor Opportunity Score 0-100 with each component, its evidence and the weight coverage."""
    cfg=merged(CATALYST_DEFAULTS,cfg);w=cfg['weights'];comp={};why={}
    if catalyst:comp['catalyst']=catalyst['impact_score'];why['catalyst']=f"{catalyst['category']} · {catalyst['label']} · impact {catalyst['impact_score']:.0f}"
    if profile['revenue_growth'] is not None and profile['profit_growth'] is not None:
        avg=(profile['revenue_growth']+min(profile['profit_growth'],100))/2
        comp['earnings']=band(avg,((30,100),(20,80),(10,60),(0,35),(-1e9,10)));why['earnings']=f"revenue {profile['revenue_growth']:+.1f}%, profit {profile['profit_growth']:+.1f}% YoY"
    q=[]
    if profile['roe'] is not None:q.append(band(profile['roe'],((20,100),(15,80),(10,55),(5,30),(-1e9,10))))
    if not profile['financial']:
        if profile.get('roce') is not None:q.append(band(profile['roce'],((20,100),(15,80),(10,55),(5,30),(-1e9,10))))
        if profile.get('debt_equity') is not None:q.append(band(-profile['debt_equity'],((-.3,100),(-.7,70),(-1,45),(-2,20),(-1e9,0))))
        if profile.get('interest_cover') is not None:q.append(band(profile['interest_cover'],((8,100),(4,70),(2,40),(-1e9,10))))
    if profile['operating_cash'] is not None and profile['net_profit']:
        q.append(100 if profile['operating_cash']>=.8*profile['net_profit']>0 else 60 if profile['operating_cash']>0 else 0)
    if q:comp['quality']=sum(q)/len(q);why['quality']=f"ROE {profile['roe']:.1f}%" if profile['roe'] is not None else 'partial'
    if profile['pe'] is not None:
        comp['valuation']=0 if profile['pe']==math.inf else band(-profile['pe'],((-15,100),(-25,80),(-40,55),(-60,30),(-1e9,10)))
        why['valuation']='loss-making' if profile['pe']==math.inf else f"P/E {profile['pe']:.1f}"
    if profile['revenue_growth'] is not None and profile['profit_growth'] is not None:
        both=(profile['revenue_growth']>0)+(profile['profit_growth']>0)
        comp['management']=(20,50,80)[both];why['management']='Proxy: growth consistency (governance/promoter data not connected)'
    if ctx and ctx.get('vol60') is not None:
        comp['risk']=band(-ctx['vol60']*100,((-25,100),(-35,70),(-50,40),(-1e9,15)))
        if ctx.get('high252'):comp['risk']=round((comp['risk']+band(ctx['close']/ctx['high252']-1,((-.15,100),(-.3,60),(-1,30))))/2,1)
        why['risk']=f"60-day volatility {ctx['vol60']*100:.0f}%"
    have={k:w[k] for k in w if k in comp};total=sum(w.values())
    score=round(sum(comp[k]*w[k] for k in have)/sum(have.values()),1) if have else 0.
    return dict(score=score,components=comp,evidence=why,coverage=round(sum(have.values())/total,2),missing_components=[k for k in w if k not in comp])

def catalyst_decision(catalyst,profile,result,ctx,cfg,sector_pct=0.):
    """BUY_CANDIDATE / WATCHLIST / REJECTED with every reason. `result` is opportunity_score output."""
    cfg=merged(CATALYST_DEFAULTS,cfg);r=[];watch=[]
    if not catalyst or catalyst['verification']!='verified':r.append('No verified catalyst')
    elif catalyst['direction']<=0 or not catalyst['catalyst_eligible']:r.append(f"{catalyst['category']} is not a positive business catalyst")
    elif catalyst['impact_score']<cfg['min_catalyst_impact']:watch.append(f"Catalyst impact {catalyst['impact_score']:.0f} below {cfg['min_catalyst_impact']:g}")
    if profile['critical_gaps']:r.append('Critical financial data missing: '+', '.join(profile['critical_gaps']))
    if result['coverage']<cfg['min_coverage']:r.append(f"Score covers only {result['coverage']*100:.0f}% of the weights (missing {', '.join(result['missing_components'])})")
    if result['score']<cfg['min_score']:watch.append(f"Opportunity score {result['score']:.0f} below {cfg['min_score']:g}")
    if profile['roe'] is not None and profile['roe']<cfg['min_roe']:r.append(f"Weak fundamentals: ROE {profile['roe']:.1f}% below {cfg['min_roe']:g}%")
    if profile['profit_growth'] is not None and profile['profit_growth']<0 and profile['revenue_growth'] is not None and profile['revenue_growth']<0:r.append('Earnings deteriorating: revenue and profit both down YoY')
    if not profile['financial']:
        if profile.get('debt_equity') is not None and profile['debt_equity']>cfg['max_debt_equity']:r.append(f"Debt/equity {profile['debt_equity']:.2f} above {cfg['max_debt_equity']:g}")
        ic=profile.get('interest_cover')
        if ic is not None and ic!=math.inf and ic<cfg['min_interest_cover']:r.append(f"Interest cover {ic:.1f} below {cfg['min_interest_cover']:g}")
    pe=profile['pe']
    if pe is None:r.append('Valuation unavailable (no market cap)')
    elif pe==math.inf:r.append('Loss-making: P/E not meaningful')
    elif pe>cfg['max_pe']:r.append(f"Expensive: P/E {pe:.1f} above {cfg['max_pe']:g}")
    if not ctx:r.append('Not enough completed daily history')
    else:
        if ctx['turnover20_cr']<cfg['min_turnover_cr']:r.append(f"Liquidity: 20-day turnover ₹{ctx['turnover20_cr']:.1f} cr below {cfg['min_turnover_cr']:g} cr")
        if ctx['close']<cfg['min_price']:r.append('Price below minimum')
        if cfg['require_uptrend'] and ctx['close']<ctx['dma200']:watch.append('Below the 200-day average; waiting for the trend')
    if sector_pct>=cfg['max_sector_pct']:r.append(f"Sector already {sector_pct:.0f}% of equity (limit {cfg['max_sector_pct']:g}%)")
    if r:return 'REJECTED',r+watch
    if watch:return 'WATCHLIST',watch
    return 'BUY_CANDIDATE',[f"Score {result['score']:.0f} with {catalyst['category']} catalyst"]

def catalyst_review(holding,profile,result,price,governance_news,held_days,cfg,sector_pct=0.):
    """Thesis review. Returns (kind, reason) to exit/reduce, or None. Negative sentiment alone never exits."""
    cfg=merged(CATALYST_DEFAULTS,cfg)
    if price<=holding['average_cost']*(1-cfg['hard_stop_pct']/100):return 'RISK_STOP',f"Down {cfg['hard_stop_pct']:g}% from average cost"
    if governance_news:return 'GOVERNANCE',governance_news
    if profile and profile['profit_growth'] is not None and profile['revenue_growth'] is not None and profile.get('period')!=holding.get('entry_period') \
        and profile['profit_growth']<0 and profile['revenue_growth']<0:return 'THESIS_FAILED',f"New period {profile['period']}: revenue {profile['revenue_growth']:+.1f}%, profit {profile['profit_growth']:+.1f}%"
    if result and result['coverage']>=cfg['min_coverage'] and result['score']<cfg['exit_score']:return 'QUALITY_DETERIORATED',f"Opportunity score fell to {result['score']:.0f} (exit below {cfg['exit_score']:g})"
    if profile and profile['pe'] not in (None,math.inf) and profile['pe']>cfg['exit_pe']:return 'VALUATION_EXCESSIVE',f"P/E {profile['pe']:.1f} above {cfg['exit_pe']:g}"
    if held_days>=cfg['catalyst_review_days'] and (not profile or profile.get('period')==holding.get('entry_period')):
        return 'CATALYST_NOT_MATERIALIZED',f"No newer reported period after {held_days} days"
    if sector_pct>cfg['reduce_sector_pct']:return 'CONCENTRATION',f"Sector at {sector_pct:.0f}% of equity (reduce above {cfg['reduce_sector_pct']:g}%)"
    return None
