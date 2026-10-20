#!/usr/bin/env python3
"""Fundamentals-based candidate baskets on point-in-time NSE annual data (scripts/fundamentals_history.py), tested with
the simulator, costs and pass rules of scripts/research_baskets.py. Read-only research; never touches ledgers.

Window: June 2019 onward, the first month two audited fiscal years (FY2018, FY2019) are public for most stocks.
Halves: June 2019-2021 and 2022-2026. The bar is the equal-weight hold of the same universe over the same window.
Rules were fixed before any result was seen, from each author's published definition; where NSE's files lack an
input before FY2023 the proxy is named:
  quality_value  Buffett-style quality at a reasonable price: ROE (owners' continuing earnings / equity) at least 15% in each of the
                 latest two fiscal years, net profit up year on year, interest cover ((PBT + finance costs) / finance
                 costs) at least 5 or no finance cost, ranked by earnings yield (earnings / market cap), highest first.
  magic_proxy    Greenblatt's Magic Formula ranking with proxies: earnings yield = EBIT / market cap (debt and cash are
                 not filed before FY2023) and return on capital = EBIT / equity (no fixed assets or working capital).
                 Lowest combined rank first; financials and utilities excluded; each pick is held 250 sessions.
  net_buyback    Net share issuance (Pontiff-Woodgate; Ikenberry's buyback drift): shares outstanding, in today's units,
                 fell at least 1% over the latest fiscal year and the company is profitable; biggest reduction first.
  dividend_quality  Dividend yield with quality: trailing-12-month dividends per share / price, ROE at least 12%, profitable,
                 dividends no more than profit; highest yield first.
  value_momentum Value and momentum together (Asness-Moskowitz-Pedersen): average of the cross-sectional z-scores of
                 earnings yield and 12-1 momentum; highest first, positive momentum only.
Informational only (too short for the halves check): Greenblatt's formula as published, earnings yield = EBIT / (market
cap + borrowings - cash) and return on capital = EBIT / (equity + borrowings - cash), from June 2023, the first month
NSE files carry debt and cash.
Piotroski's F-score needs total assets, operating cash flow and leverage for two consecutive years; NSE files carry
them only from FY2022-FY2023, so it cannot be tested before mid-2024 and is not run.
A fiscal year counts from period end + 60 days and is dropped 13 months after the next year was due, so a stock whose
filing is missing is not ranked on stale figures. Market cap is the adjusted close times shares in today's units.

Usage: .venv/bin/python scripts/research_fundamentals.py"""
import json,sys,time
from datetime import date,timedelta
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
import research_baskets as rb
import research_multi_year as rm
from marketdesk.history_rules import is_financial
SRC=ROOT/'data/backtests/fundamentals-history.json';OUT=ROOT/'data/backtests/research-fundamentals.json'
START='2019-06-01';H1=(START,'2021-12-31');H2=('2022-01-01','2026-12-31')
ACTIVE=('ref_momentum','ref_lowvol','mom121','alpha','overnight','gap_drift','sector','clenow')

def frames(P):
    """Daily point-in-time metric frames (rows = sessions, columns = stocks); NaN where not public or not usable."""
    R=json.loads(SRC.read_text())['records'];cal=P['c'].index;cols=P['c'].columns
    keys=('np','np_prev','roe','roe_prev','pbt','fin','equity','shares','issuance');data={k:pd.DataFrame(np.nan,index=cal,columns=cols) for k in keys}
    for s,years in R.items():
        if s not in cols:continue
        fys=sorted(int(y) for y in years);shares=None
        for fy in fys:
            r=years[str(fy)];prev=years.get(str(fy-1))
            if r.get('shares_now_units'):shares=r['shares_now_units']
            ps=(prev or {}).get('shares_now_units')
            a=r['available_from'];b=(date(fy+1,3,31)+timedelta(days=60+395)).isoformat()   # stale 13 months after FY+1 was due
            nxt=next((years[str(f)]['available_from'] for f in fys if f>fy),None);b=min(b,nxt) if nxt else b
            rows=(cal>=a)&(cal<b)
            if not rows.any():continue
            def ratio(x,y):return x/y if x is not None and y is not None and y>0 else np.nan
            same=prev is not None and prev.get('basis')==r.get('basis')
            # Earnings = owners' continuing earnings (continuing EPS x shares; scripts/fundamentals_history.py).
            vals=dict(np=r.get('earnings'),np_prev=prev.get('earnings') if same else None,roe=ratio(r.get('earnings'),r.get('equity')),
                roe_prev=ratio(prev.get('earnings'),prev.get('equity')) if same else np.nan,pbt=r.get('pbt'),fin=r.get('finance_costs'),equity=r.get('equity'),shares=shares,
                issuance=r['shares_now_units']/ps-1 if r.get('shares_now_units') and ps else None)
            for k,v in vals.items():
                if v is not None and not (isinstance(v,float) and np.isnan(v)):data[k].loc[rows,s]=v
    data['mcap']=P['c']*data['shares']                                           # crore: adjusted close x crore shares, today's units
    # Trailing-12-month dividends per share (today's units), counted from each ex-date's session.
    D=json.loads(SRC.read_text()).get('dividends',{});paid=pd.DataFrame(0.,index=cal,columns=cols);known=pd.Series(False,index=cols)
    for s,rows in D.items():
        if s not in cols:continue
        known[s]=True
        for ex,amount in rows:
            i=cal.searchsorted(ex)
            if i<len(cal):paid.iloc[i,cols.get_loc(s)]+=amount
    data['div12']=paid.rolling(250,min_periods=1).sum().loc[:,known.values].reindex(columns=cols)   # no actions data: missing, not zero
    return data

def candidates(P,I,F,elig):
    C=P['c'];sec=pd.Series(P['sector']).reindex(C.columns).fillna('')
    nonfin=pd.Series([not is_financial(x) for x in sec],index=C.columns);util=pd.Series(['power' in x.lower() or 'utilit' in x.lower() for x in sec],index=C.columns)
    rows=lambda m:pd.DataFrame(np.repeat(m.values[None,:],len(C),axis=0),index=C.index,columns=C.columns)
    ebit=F['pbt']+F['fin'];cover=(ebit/F['fin']).where(F['fin']>0)
    safe=(cover>=5)|((F['fin']==0)&(F['pbt']>0))
    quality=(F['roe']>=.15)&(F['roe_prev']>=.15)&(F['np']>F['np_prev'])&(F['np_prev']>0)&safe&rows(nonfin)
    ep=(F['np']/F['mcap']).where(F['mcap']>0)
    ok=elig&rows(nonfin&~util)&(ebit>0)&(F['equity']>0)&(F['mcap']>0)
    ey=(ebit/F['mcap']).where(ok);roc=(ebit/F['equity']).where(ok)
    combined=ey.rank(axis=1,ascending=False)+roc.rank(axis=1,ascending=False)
    z=lambda X:X.sub(X.mean(axis=1),axis=0).div(X.std(axis=1),axis=0)
    pool=elig&rows(nonfin)&(F['mcap']>0)&(I['mom121']>0)
    vm=((z(ep.where(pool&ep.notna()&I['mom121'].notna()))+z(I['mom121'].where(pool&ep.notna())))/2)
    dy=(F['div12']/C).where(F['div12']>0);payout=(F['div12']*F['shares']/F['np']).where(F['np']>0)
    return {'quality_value':dict(score=ep.where(elig&quality&(ep>0)),keep=30,stop=.20),
            'net_buyback':dict(score=(-F['issuance']).where(elig&rows(nonfin)&(F['issuance']<=-.01)&(F['np']>0)),keep=30,stop=.20),
            'dividend_quality':dict(score=dy.where(elig&rows(nonfin)&(F['roe']>=.12)&(F['np']>0)&(payout<=1)),keep=30,stop=.20),
            'value_momentum':dict(score=vm.where(pool),keep=30,stop=.20),
            'magic_proxy':dict(score=(-combined).where(ok),monthly=False,max_hold=250,stop=1.)}

def faithful_magic(P,I,F,elig,start='2023-06-01'):
    """Greenblatt with real debt and cash (FY2023+), as an informational short-window result."""
    C=P['c'];R=json.loads(SRC.read_text())['records'];debt=pd.DataFrame(np.nan,index=C.index,columns=C.columns);cash=debt.copy()
    for s,ys in R.items():
        if s not in C.columns:continue
        fys=sorted(int(y) for y in ys)
        for fy in fys:
            r=ys[str(fy)]
            if fy<2023 or r.get('borrowings') is None or r.get('cash') is None:continue
            nxt=next((ys[str(f)]['available_from'] for f in fys if f>fy),'9999');rows=(C.index>=r['available_from'])&(C.index<nxt)
            debt.loc[rows,s]=r['borrowings'];cash.loc[rows,s]=r['cash']
    sec=pd.Series(P['sector']).reindex(C.columns).fillna('');keep=np.array([not is_financial(x) and 'power' not in x.lower() and 'utilit' not in x.lower() for x in sec])
    ebit=F['pbt']+F['fin'];ev=F['mcap']+debt-cash;capital=F['equity']+debt-cash
    ok=elig&pd.DataFrame(np.repeat(keep[None,:],len(C),axis=0),index=C.index,columns=C.columns)&(ebit>0)&(ev>0)&(capital>0)
    score=-((ebit/ev).where(ok).rank(axis=1,ascending=False)+(ebit/capital).where(ok).rank(axis=1,ascending=False))
    e,tr,_=rb.simulate(P,I,score=score.where(ok),monthly=False,max_hold=250,stop=1.,start=start)
    return dict(window=[start,C.index[-1]],result=rb.stats(e),trades=len([t for t in tr if t['exit']]),hold=rb.stats(rb.hold_curve(I,start=start)))

def run():
    P=rb.load();I=rb.indicators(P);F=frames(P);T=I['tradable'];E=T&I['trend']
    big200=I['turn20'].where(T).rank(axis=1,ascending=False)<=200
    base,free,large=candidates(P,I,F,E),candidates(P,I,F,T),candidates(P,I,F,E&big200)
    ew,ewL=rb.hold_curve(I,start=START),rb.hold_curve(I,start=START,mask=big200)
    bench={k:dict(full=rb.stats(c),h1=rb.stats(c,*H1),h2=rb.stats(c,*H2)) for k,c in (('ew',ew),('ew_top200',ewL))}
    sec=pd.Series(P['sector']).reindex(T.columns).fillna('');nf=T&pd.DataFrame(np.repeat(np.array([not is_financial(x) for x in sec])[None,:],len(T),axis=0),index=T.index,columns=T.columns)
    def share(X):v=X.where(nf).notna().sum(axis=1)/nf.sum(axis=1);v=v[v.index>=START];return {y:round(float(g.mean()),3) for y,g in v.groupby(v.index.str[:4])}
    covered=None
    out=dict(created=time.strftime('%Y-%m-%d %H:%M'),window=[START,P['c'].index[-1]],bench=bench,
        coverage=dict(earnings=share(F['np']),equity=share(F['equity']),market_cap=share(F['mcap']),two_year_roe=share(F['roe_prev']),ebit=share(F['pbt']+F['fin'])),results={})
    print('bench',json.dumps(bench),flush=True)
    for k,v in out['coverage'].items():print('coverage (share of tradable non-financial stocks)',k,v,flush=True)
    for name,spec in base.items():
        res={}
        for var,kw in {'base':{},'n15':dict(n=15,capital=1.5e5,keep=int(spec.get('keep',30)*1.5)),'slip2x':dict(slip=.005),'mkt':dict(mkt=True)}.items():
            e,tr,fees=rb.simulate(P,I,start=START,**{**spec,**kw});closed=[x for x in tr if x['exit']]
            gain=sum(x['pnl'] for x in closed if x['pnl']>0);loss=-sum(x['pnl'] for x in closed if x['pnl']<=0)
            total=e.iloc[-1]*(kw.get('capital',1e5)/1e5)-kw.get('capital',1e5);top5=sum(sorted((x['pnl'] for x in tr),reverse=True)[:5])
            res[var]=dict(full=rb.stats(e),h1=rb.stats(e,*H1),h2=rb.stats(e,*H2),trades=len(closed),win_rate=round(sum(x['pnl']>0 for x in closed)/len(closed),3) if closed else None,
                profit_factor=round(gain/loss,2) if loss else None,fees=round(fees),pnl=round(total),pnl_ex_top5=round(total-top5),
                years={y:round(100*(g.iloc[-1]/g.iloc[0]-1),1) for y,g in e.groupby(e.index.str[:4])})
        res['no_trend_filter']=dict(full=rb.stats(rb.simulate(P,I,start=START,**free[name])[0]))
        e=rb.simulate(P,I,start=START,**large[name])[0];res['top200']=dict(full=rb.stats(e),h1=rb.stats(e,*H1),h2=rb.stats(e,*H2))
        b=res['base'];w=bench['ew'];L=bench['ew_top200']
        checks=dict(beats_hold=b['full']['cagr']>w['full']['cagr'],both_halves=b['h1']['cagr']>w['h1']['cagr'] and b['h2']['cagr']>w['h2']['cagr'] and res['top200']['h1']['cagr']>L['h1']['cagr'],
            drawdown=b['full']['maxdd']<=w['full']['maxdd']+5,ex_top5=b['pnl_ex_top5']>0,n15=res['n15']['full']['cagr']>w['full']['cagr'],
            slip2x=res['slip2x']['full']['cagr']>w['full']['cagr'],top200=res['top200']['full']['cagr']>L['full']['cagr'] and res['top200']['h2']['cagr']>L['h2']['cagr'])
        res['failed']=[k for k,v in checks.items() if not v];res['passed']=not res['failed'];out['results'][name]=res
        print(f"{name:14} cagr {b['full']['cagr']:6} h1 {b['h1']['cagr']:6} h2 {b['h2']['cagr']:6} dd {b['full']['maxdd']:5} trades {b['trades']:4} | top200 {res['top200']['full']['cagr']:6} | "+('PASS' if res['passed'] else 'fail: '+','.join(res['failed'])),flush=True)
    out['magic_faithful']=faithful_magic(P,I,F,E);print('magic_faithful (informational)',out['magic_faithful'],flush=True)
    # Uniqueness over the same window against every active rule basket.
    rm.TITLES.update(quality_value='Quality at a reasonable price',magic_proxy='Magic Formula (proxy)',net_buyback='Net buybacks',
        dividend_quality='Dividend yield with quality',value_momentum='Value and momentum')
    price=rb.candidates(P,I,E);pick={**{k:price[k] for k in ACTIVE},**base};saved=rb.START;rb.START=START
    try:out['uniqueness']={k:dict(zip(('unique','reason'),v)) for k,v in rm.uniqueness(P,I,pick,active=ACTIVE).items() if k in base}
    finally:rb.START=saved
    print('uniqueness',out['uniqueness'])
    OUT.write_text(json.dumps(out,indent=1,default=float));print('saved',OUT)

if __name__=='__main__':run()
