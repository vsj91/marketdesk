"""The five quant basket strategies. Pure functions: a market snapshot and the current book in,
a target plan out. The engine turns plans into orders; nothing here touches cash or fills.

Snapshot (built by quant.data or by tests):
  day         'YYYY-MM-DD', the completed session the signals are computed on
  bars        {symbol: [{day,open,high,low,close,volume}, ...]} oldest first, ending at `day`
  fundamentals{symbol: {ebit,ev,market_cap,equity,net_profit,ocf,total_debt,dividend_yield,financial}}
  sectors     {symbol: sector}
  size        {symbol: 'Large cap'|'Mid cap'|'Small cap'}
  benchmark   [index closes] oldest first, ending at `day`
  vix         optional [VIX closes]
  confirmed   optional {symbol: {day}} large moves NSE verified as ordinary market moves

Plan returned by every strategy:
  targets {symbol: {weight|shares|keep, reason, score}}  -- what the basket should hold
  exits   {symbol: reason}                                -- holdings to liquidate
  meta    {...}                                           -- regime, candidate counts, notes
Holdings missing from both targets and exits are liquidated by the engine as 'dropped'."""
import math
from . import calc

MIN_PRICE=20
MIN_TURNOVER=10_000_000      # Rs 1 crore average daily traded value
MAX_DAILY_JUMP=.25           # a >25% close-to-close move is an unadjusted corporate action unless NSE verified it

DEFAULTS={
 'multifactor':dict(rebalance='monthly',n=15,sector_cap=.30,factor_weights=dict(value=.25,quality=.30,momentum=.30,lowvol=.15),
    roe_min=.15,require_positive_accruals=True,min_coverage=.5),
 'risk_parity':dict(rebalance='biweekly',n=20,sector_cap=.30,vol_window=60,min_coverage=.4,
    factor_weights=dict(value=.25,quality=.30,momentum=.30,lowvol=.15)),
 'stat_arb':dict(rebalance='daily',max_positions=10,entry_z=-2.,exit_z=0.,stop_z=-3.2,z_window=20,
    sizes=('Large cap','Mid cap'),require_fundamentals=True),
 'trend':dict(rebalance='daily',max_positions=20,breakout_window=50,volume_ratio=1.5,risk_pct=.01,atr_mult=2.,
    trail_atr=3.,max_weight=.20),
 'regime':dict(rebalance='monthly',confirm_days=3,sleeve_size=8,sector_cap=.30,bear_defensive='low_beta',roe_min=.15,
    allocations={'bull':{'momentum':.70,'quality':.30},'choppy':{'quality':.50,'lowvol':.50},
                 'bear':{'defensive':.60,'value':.40}}),
}

def config(strategy,overrides=None):
    out={**DEFAULTS[strategy]}
    for k,v in (overrides or {}).items():out[k]=v
    return out

# ---------------------------------------------------------------- features

def features(snapshot):
    """Per-symbol indicators on the completed session; symbols failing data guards are left out.
    Cached on the snapshot so five baskets on the same session compute them once."""
    if '_features' in snapshot:return snapshot['_features']
    out=snapshot['_features']={};bench=snapshot.get('benchmark') or [];moves=snapshot.get('confirmed') or {}
    for sym,bars in snapshot['bars'].items():
        if not bars or bars[-1]['day']!=snapshot['day']:continue        # no print today: stale
        closes=[b['close'] for b in bars]
        if any(not (calc.finite(c) and c>0) for c in closes):continue
        recent=bars[-calc.TRADING_DAYS-1:];verified=moves.get(sym,())
        if any(b['day'] not in verified and abs(b['close']/a['close']-1)>MAX_DAILY_JUMP for a,b in zip(recent,recent[1:])):continue
        turnover=sum(b['close']*b['volume'] for b in bars[-20:])/min(20,len(bars))
        adv=sum(b['volume'] for b in bars[-20:])/min(20,len(bars))
        f=dict(close=closes[-1],bars=len(bars),turnover20=turnover,adv20=adv,
            vol252=calc.realized_vol(closes,calc.TRADING_DAYS),vol60=calc.realized_vol(closes,60),
            daily_vol=calc.realized_vol(closes,20),
            momentum=calc.momentum_12_1(closes),dma50=calc.sma(closes,50),dma200=calc.sma(closes,200),
            slope_up=calc.sma_slope_up(closes),atr14=calc.atr(bars[-60:]),
            breakout=calc.donchian_breakout(bars),volume_multiple=calc.volume_surge(bars),
            beta=calc.beta(closes,bench) if bench else None)
        if f['daily_vol']:f['daily_vol']/=math.sqrt(calc.TRADING_DAYS)
        out[sym]=f
    return out

def tradable(f):return f['close']>=MIN_PRICE and f['turnover20']>=MIN_TURNOVER

def ratios(fund):
    """Derived fundamental ratios; financials skip EV and leverage, where debt is the business."""
    if not fund:return {}
    out={};eq=fund.get('equity');np_=fund.get('net_profit');cap=fund.get('market_cap')
    if calc.finite(eq) and eq>0:
        if calc.finite(np_):out['roe']=np_/eq
        if calc.finite(cap) and cap>0:out['book_to_price']=eq/cap
        if not fund.get('financial') and calc.finite(fund.get('total_debt')):out['debt_to_equity']=fund['total_debt']/eq
    if not fund.get('financial') and calc.finite(fund.get('ebit')) and calc.finite(fund.get('ev')) and fund['ev']>0:
        out['earnings_yield']=fund['ebit']/fund['ev']
    if calc.finite(fund.get('ocf')) and calc.finite(np_) and np_>0:out['ocf_to_ni']=fund['ocf']/np_
    if calc.finite(fund.get('dividend_yield')):out['dividend_yield']=fund['dividend_yield']
    return out

def factor_scores(feats,funds,universe):
    """{factor: {symbol: z}} for value, quality, momentum, lowvol over `universe`.
    Each factor is the mean of its sub-factor z-scores; sub-factors are z-scored across the universe."""
    r={s:ratios(funds.get(s)) for s in universe}
    def z(fn):return calc.zscores({s:fn(s) for s in universe})
    def blend(*maps):
        out={}
        for s in universe:
            got=[m[s] for m in maps if s in m]
            if got:out[s]=sum(got)/len(got)
        return calc.zscores(out)
    value=blend(z(lambda s:r[s].get('earnings_yield')),z(lambda s:r[s].get('book_to_price')))
    quality=blend(z(lambda s:r[s].get('roe')),z(lambda s:-r[s]['debt_to_equity'] if 'debt_to_equity' in r[s] else None),
                  z(lambda s:r[s].get('ocf_to_ni')))
    momentum=z(lambda s:feats[s]['momentum'])
    lowvol=z(lambda s:1/feats[s]['vol252'] if feats[s]['vol252'] else None)
    return dict(value=value,quality=quality,momentum=momentum,lowvol=lowvol),r

def eligible(feats,min_bars=calc.TRADING_DAYS+1):
    return [s for s,f in feats.items() if tradable(f) and f['bars']>=min_bars]

def quality_screen(r,roe_min,require_accruals=True):
    if 'roe' not in r or r['roe']<=roe_min:return False
    if require_accruals and r.get('ocf_to_ni') is not None and r['ocf_to_ni']<=0:return False
    return True

def score_card(sym,parts,comp=None):
    card={f'{k}_z':round(v[sym],3) for k,v in parts.items() if sym in v}
    if comp is not None and sym in comp:card['composite']=round(comp[sym],3)
    return card

# ---------------------------------------------------------------- A. multi-factor smart beta

def multifactor(snapshot,book,cfg):
    feats=features(snapshot);universe=eligible(feats)
    parts,r=factor_scores(feats,snapshot.get('fundamentals',{}),universe)
    screened=[s for s in universe if quality_screen(r[s],cfg['roe_min'],cfg['require_positive_accruals'])]
    comp=calc.composite({k:{s:v for s,v in m.items() if s in screened} for k,m in parts.items()},cfg['factor_weights'],cfg['min_coverage'])
    ranked=sorted(comp,key=lambda s:-comp[s])
    picks=calc.pick_with_sector_cap(ranked,snapshot.get('sectors',{}),cfg['n'],cfg['sector_cap'])
    w=1/cfg['n']
    targets={s:dict(weight=w,score=score_card(s,parts,comp),
        reason=f"multi-factor rank {ranked.index(s)+1}/{len(ranked)}, composite z {comp[s]:+.2f}") for s in picks}
    exits={s:(f"Fell to rank {ranked.index(s)+1} (outside top {cfg['n']} after sector cap)" if s in comp
              else 'Failed quality screen or lost factor coverage') for s in book if s not in targets}
    return dict(targets=targets,exits=exits,meta=dict(universe=len(universe),screened=len(screened),ranked=len(ranked),
        note=None if picks else 'No stock passed the quality screen with enough factor coverage; holding cash.'))

# ---------------------------------------------------------------- B. risk parity / inverse volatility

def risk_parity(snapshot,book,cfg):
    feats=features(snapshot);universe=eligible(feats)
    parts,_=factor_scores(feats,snapshot.get('fundamentals',{}),universe)
    comp=calc.composite(parts,cfg['factor_weights'],cfg['min_coverage'])
    ranked=sorted(comp,key=lambda s:-comp[s]);sectors=snapshot.get('sectors',{})
    picks=calc.pick_with_sector_cap(ranked,sectors,cfg['n'],cfg['sector_cap'])
    vols={s:feats[s]['vol60'] for s in picks}
    w=calc.cap_weights(calc.inverse_vol_weights(vols),{s:sectors.get(s) or 'Unclassified' for s in picks},cfg['sector_cap'])
    targets={s:dict(weight=x,score={**score_card(s,parts,comp),'vol60':round(vols[s],4)},
        reason=f"inverse-vol weight {x:.1%} (60-day vol {vols[s]:.1%})") for s,x in w.items()}
    exits={s:'Left the selection set at rebalance' for s in book if s not in targets}
    return dict(targets=targets,exits=exits,meta=dict(universe=len(universe),ranked=len(ranked)))

# ---------------------------------------------------------------- C. statistical arbitrage / mean reversion

def stat_arb(snapshot,book,cfg):
    feats=features(snapshot);funds=snapshot.get('fundamentals',{});size=snapshot.get('size',{})
    exits={};targets={}
    for s,pos in book.items():
        f=feats.get(s)
        if not f:exits[s]='No valid completed session for a held name';continue
        zv=calc.rolling_z([b['close'] for b in snapshot['bars'][s]],cfg['z_window'])
        if zv is None:exits[s]='Z-score unavailable';continue
        if zv<cfg['stop_z']:exits[s]=f"Stop: Z-score {zv:.2f} breached {cfg['stop_z']}"
        elif zv>=cfg['exit_z']:exits[s]=f"Reverted to mean: Z-score {zv:+.2f}"
        else:targets[s]=dict(keep=True,score=dict(z=round(zv,3)),reason=f'Holding: Z-score {zv:.2f}')
    def strong(s):
        r=ratios(funds.get(s))
        if not r:return not cfg['require_fundamentals']
        return r.get('roe',0)>0 and r.get('ocf_to_ni',1)>0
    cands=[]
    for s,f in feats.items():
        if s in book or not tradable(f) or size.get(s) not in cfg['sizes'] or not f['slope_up'] or not strong(s):continue
        zv=calc.rolling_z([b['close'] for b in snapshot['bars'][s]],cfg['z_window'])
        if zv is not None and cfg['stop_z']<=zv<cfg['entry_z']:cands.append((zv,s))
    free=cfg['max_positions']-len(targets);w=1/cfg['max_positions']
    for zv,s in sorted(cands)[:max(0,free)]:
        targets[s]=dict(weight=w,score=dict(z=round(zv,3)),reason=f'Z-score reached {zv:.2f} with a rising 200-day SMA')
    return dict(targets=targets,exits=exits,meta=dict(candidates=len(cands)))

# ---------------------------------------------------------------- D. volatility-adjusted trend following

def trend(snapshot,book,cfg,equity):
    feats=features(snapshot);targets={};exits={}
    for s,pos in book.items():
        f=feats.get(s)
        if not f or not f['atr14']:exits[s]='No valid completed session or ATR for a held name';continue
        high=max(pos.get('high_close') or 0,f['close']);stop=high-cfg['trail_atr']*f['atr14']
        if f['close']<stop:exits[s]=f"Trailing stop: close {f['close']:.2f} below {stop:.2f} ({cfg['trail_atr']:g}xATR under high {high:.2f})";continue
        shares=min(calc.atr_shares(equity,f['atr14'],cfg['risk_pct'],cfg['atr_mult']),math.floor(equity*cfg['max_weight']/f['close']))
        entry_atr=(pos.get('meta') or {}).get('entry_atr')
        why=(f"ATR volatility expansion (ATR14 {entry_atr:.2f} -> {f['atr14']:.2f})" if entry_atr and f['atr14']>entry_atr
             else f"ATR risk sizing (ATR14 {f['atr14']:.2f})")
        # Trims only: the risk budget never adds to a winner, it only shrinks a position whose ATR grew.
        targets[s]=dict(shares=min(shares,pos['qty']),score=dict(atr14=round(f['atr14'],3),trail_stop=round(stop,2)),reason=why)
    cands=[]
    for s,f in feats.items():
        if s in book or not tradable(f) or f['bars']<201 or not f['atr14']:continue
        if f['breakout'] and f['dma50']>f['dma200'] and (f['volume_multiple'] or 0)>cfg['volume_ratio']:
            cands.append((-f['volume_multiple'],s))
    free=cfg['max_positions']-len(targets)
    for _,s in sorted(cands)[:max(0,free)]:
        f=feats[s];shares=min(calc.atr_shares(equity,f['atr14'],cfg['risk_pct'],cfg['atr_mult']),math.floor(equity*cfg['max_weight']/f['close']))
        if shares<1:continue
        targets[s]=dict(shares=shares,score=dict(atr14=round(f['atr14'],3),volume_multiple=round(f['volume_multiple'],2)),
            meta=dict(entry_atr=f['atr14']),
            reason=f"50-day breakout, 50>200 DMA, volume {f['volume_multiple']:.1f}x; {shares} sh = 1% equity per 2xATR")
    return dict(targets=targets,exits=exits,meta=dict(candidates=len(cands)))

# ---------------------------------------------------------------- E. regime-switching dynamic basket

def regime(snapshot,book,cfg):
    reg=calc.confirmed_regime(snapshot.get('benchmark') or [],cfg.get('current_regime'),cfg['confirm_days'],snapshot.get('vix'))
    if not reg:
        return dict(targets={},exits={},meta=dict(regime=None,note='Benchmark history too short for regime detection; no change.'),hold=True)
    feats=features(snapshot);universe=eligible(feats);sectors=snapshot.get('sectors',{})
    parts,r=factor_scores(feats,snapshot.get('fundamentals',{}),universe);n=cfg['sleeve_size']
    def top(scores,filt=lambda s:True):
        ranked=sorted((s for s in scores if filt(s)),key=lambda s:-scores[s])
        return calc.pick_with_sector_cap(ranked,sectors,n,cfg['sector_cap'])
    def equal(picks):return {s:1/len(picks) for s in picks} if picks else {}
    def invvol(picks):return calc.inverse_vol_weights({s:feats[s]['vol60'] for s in picks})
    def sleeve(name):
        if name=='momentum':return equal(top(parts['momentum']))
        if name=='quality':return equal(top(parts['quality'],lambda s:quality_screen(r[s],cfg['roe_min'])))
        if name=='lowvol':return invvol(top(parts['lowvol']))
        if name=='value':
            dy=calc.zscores({s:r[s].get('dividend_yield') for s in universe})
            score={s:(v+dy[s])/2 if s in dy else v for s,v in parts['value'].items()}
            return equal(top(score))
        if name=='defensive':
            if cfg['bear_defensive']=='cash':return {}
            low={s:-feats[s]['beta'] for s in universe if feats[s]['beta'] is not None}
            return invvol(top(low))
        raise ValueError(name)
    targets={};notes=[]
    for name,alloc in cfg['allocations'][reg['regime']].items():
        w=sleeve(name)
        if not w:notes.append(f'{name} sleeve empty: {alloc:.0%} held in cash');continue
        for s,x in w.items():
            t=targets.setdefault(s,dict(weight=0.,score=score_card(s,parts),reason=''))
            t['weight']+=alloc*x;t['reason']=(t['reason']+' + ' if t['reason'] else f"{reg['regime']} regime: ")+f'{name} sleeve'
    # Each sleeve is capped on its own; cap the combined basket too, so overlapping sleeves cannot stack one sector.
    capped=calc.cap_weights({s:t['weight'] for s,t in targets.items()},{s:sectors.get(s) or 'Unclassified' for s in targets},cfg['sector_cap'])
    for s,x in capped.items():targets[s]['weight']=x
    exits={s:f"Not in the {reg['regime']} regime sleeves" for s in book if s not in targets}
    return dict(targets=targets,exits=exits,meta=dict(regime=reg['regime'],regime_detail={k:(round(v,4) if isinstance(v,float) else v) for k,v in reg.items() if k!='regime'},notes=notes))

STRATEGIES={'multifactor':multifactor,'risk_parity':risk_parity,'stat_arb':stat_arb,'trend':trend,'regime':regime}
