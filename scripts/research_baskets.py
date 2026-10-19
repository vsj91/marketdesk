#!/usr/bin/env python3
"""Ten-year strategy research for new paper baskets (read-only; never touches ledgers or the candles table).

  fetch  Upstox V3 adjusted daily candles, Jan 2017 onward, for every listed instrument plus the Nifty 500
         index, cached under data/backtests/daily-long (one JSON per symbol; reruns skip cached files).
  run    Simulates each candidate and checks it against rules fixed before looking at results.

Universe: each month, the 500 most traded stocks by 126-session average value as of the prior month-end,
from all ~2,300 listed NSE stocks. This approximates historical Nifty 500 membership without hindsight;
testing today's Nifty 500 over ten years roughly doubled equal-weight returns through survivorship bias.
Stocks delisted before 2026 are still missing, so a smaller bias remains.
Fills follow scripts/backtest_baskets.py: completed close of t-1 signals, fill at the open of t with 0.25%
adverse slippage, stops at the stop level or the open on a gap, live delivery charges, and the 1.5%
round-trip fee cap. Positions are equal weight of current equity (₹1,00,000 start); no dividends.

Pass rules (all required): beat the equal-weight universe hold over 2018-2026 and in both halves
(2018-21, 2022-26); max drawdown within hold + 5 points; profit without the 5 best trades; still ahead
with doubled slippage and with 15 holdings on ₹1,50,000; and ahead of the equal-weight hold of the 200
most traded stocks when restricted to them, overall and in both halves.

Usage: .venv/bin/python scripts/research_baskets.py fetch
       .venv/bin/python scripts/research_baskets.py run [--only mom121,alpha] [--rebuild]"""
import argparse,json,math,sqlite3,sys,time
from pathlib import Path
import numpy as np,pandas as pd,requests
from urllib.parse import quote
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from marketdesk.execution import charges
from marketdesk.working_universe import symbol_filter
CACHE=ROOT/'data/backtests/daily-long';PANEL=ROOT/'data/backtests/daily-all-panel.pkl';OUT=ROOT/'data/backtests/research-baskets.json'
START,H1,H2='2018-01-01',('2018-01-01','2021-12-31'),('2022-01-01','2026-12-31')

def fetch(first='2017-01-01',last=None):
    from datetime import date
    from marketdesk.feed import load_token
    last=last or date.today().isoformat();CACHE.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(f'file:{ROOT}/data/marketdesk.sqlite3?mode=ro',uri=True)
    keys=dict(db.execute('SELECT symbol,instrument_key FROM instruments'));keys['_NIFTY500']='NSE_INDEX|Nifty 500'
    headers={'Authorization':'Bearer '+load_token(),'Accept':'application/json'};stamp=0;failed=[]
    for i,(s,k) in enumerate(sorted(keys.items())):
        f=CACHE/f'{s}.json'
        if f.exists():continue
        for attempt in range(4):
            time.sleep(max(0,.3-(time.monotonic()-stamp)));stamp=time.monotonic()   # under the 4 requests/second limit
            try:
                r=requests.get('https://api.upstox.com/v3/historical-candle/'+quote(k,safe='')+f'/days/1/{last}/{first}',headers=headers,timeout=30)
                if r.status_code==429:time.sleep(5*(attempt+1));continue
                r.raise_for_status();f.write_text(json.dumps([[c[0][:10],*c[1:6]] for c in sorted(r.json()['data']['candles'])]));break
            except Exception as error:
                if attempt==3:failed.append((s,str(error)[:80]))
                time.sleep(2)
        if i%100==0:print(i,s,flush=True)
    print('cached',len(keys)-len(failed),'failed',failed)

def fee(v,side):
    """Float form of execution.charges for delivery orders (DP on sells); checked against it below."""
    if side=='BUY':return 20+v*(.001+.0000307+.000001+.00015)+.18*(20+v*.0000307)
    return 20+v*(.001+.0000307+.000001)+20+.18*(40+v*.0000307)
assert abs(fee(10000,'BUY')-charges(10000,'BUY')['total'])<.05 and abs(fee(10000,'SELL')-charges(10000,'SELL',True)['total'])<.05

def load(rebuild=False):
    if PANEL.exists() and not rebuild:return pd.read_pickle(PANEL)
    frames={}
    for f in CACHE.glob('*.json'):
        rows=json.loads(f.read_text())
        if rows:df=pd.DataFrame(rows,columns=['d','o','h','l','c','v']).set_index('d');frames[f.stem]=df[~df.index.duplicated(keep='last')]
    index=frames.pop('_NIFTY500');cal=index.index
    P={k:pd.DataFrame({s:df[k] for s,df in frames.items()}).reindex(cal) for k in 'ohlcv'}
    db=sqlite3.connect(f'file:{ROOT}/data/marketdesk.sqlite3?mode=ro',uri=True)
    P['sector']=dict(db.execute('SELECT symbol,sector FROM instruments'));P['index']=index['c']
    P['members']=[r[0] for r in db.execute('SELECT symbol FROM instruments WHERE '+symbol_filter(db))]
    pd.to_pickle(P,PANEL);return P

def indicators(P):
    """Every value is known at the close of its row's session."""
    O,H,L,C,V=(P[k] for k in 'ohlcv');I={}
    r=C/C.shift(1)-1;lr=np.log(C/C.shift(1));I['ret']=r;I['n']=C.notna().cumsum()
    I['dma200']=C.rolling(200,min_periods=200).mean();I['dma50']=C.rolling(50,min_periods=50).mean()
    I['turn20']=(C*V).rolling(20,min_periods=20).mean()
    jump=(r.abs()>.25).astype(float).where(r.notna());I['clean']=jump.rolling(126,min_periods=1).max().fillna(1)==0   # live rule: >25% day = break
    rank=(C*V).rolling(126,min_periods=100).mean().where(I['n']>=130).rank(axis=1,ascending=False)
    month=pd.Series(C.index.str[:7],index=C.index);first=month!=month.shift(1)
    I['pit500']=(rank<=500).astype(float).shift(1).where(first).ffill().fillna(0).astype(bool)
    I['cur500']=pd.DataFrame({s:s in set(P['members']) for s in C.columns},index=C.index)
    I['tradable']=(C>=20)&(I['turn20']>=1e7)&I['clean']&(I['n']>=130)&I['pit500'];I['trend']=C>I['dma200']
    I['mom61']=C.shift(21)/C.shift(126)-1;I['mom121']=C.shift(21)/C.shift(252)-1
    I['vol120']=lr.rolling(120,min_periods=120).std()*math.sqrt(250);I['high52']=C/H.rolling(252,min_periods=252).max()
    m=np.log(P['index']/P['index'].shift(1))
    beta=lr.rolling(126,min_periods=110).cov(m).div(m.rolling(126,min_periods=110).var(),axis=0)
    R=lr.rolling(105,min_periods=95).sum().shift(21);M=m.rolling(105,min_periods=95).sum().shift(21)
    I['resmom']=(R-beta.mul(M,axis=0))/(lr.sub(beta.mul(m,axis=0)).rolling(126,min_periods=110).std()*math.sqrt(105))   # Blitz-Huij-Martens
    beta1=lr.rolling(250,min_periods=200).cov(m).div(m.rolling(250,min_periods=200).var(),axis=0)
    I['alpha']=lr.rolling(250,min_periods=200).mean()-beta1.mul(m.rolling(250,min_periods=200).mean(),axis=0)   # NSE Alpha-style, 1 year
    sig=lr.rolling(250,min_periods=200).std()*math.sqrt(250);I['r6_vol']=I['mom61']/sig;I['r12_vol']=I['mom121']/sig  # NSE Momentum 30-style
    pos=(r>0).astype(float).where(r.notna()).rolling(231,min_periods=200).mean().shift(21)
    neg=(r<0).astype(float).where(r.notna()).rolling(231,min_periods=200).mean().shift(21)
    I['fip_id']=np.sign(I['mom121'])*(neg-pos)                       # Da-Gurun-Warachka information discreteness; lower = smoother
    I['overnight']=np.log(O/C.shift(1)).rolling(105,min_periods=95).sum().shift(21)   # Lou-Polk-Skouras overnight returns
    I['volratio']=V.rolling(5,min_periods=5).mean()/V.shift(5).rolling(50,min_periods=40).mean()
    I['ret5']=C/C.shift(5)-1;I['ret21']=C/C.shift(21)-1;I['max21']=r.rolling(21,min_periods=21).max()
    I['gap']=O/C.shift(1)-1;I['gapvol']=V/V.shift(1).rolling(50,min_periods=40).mean()
    I['mkt_up']=P['index']>P['index'].rolling(200,min_periods=200).mean()
    ends=C.groupby(C.index.str[:7]).last();mr=ends/ends.shift(1)-1;seas=pd.DataFrame(index=mr.index,columns=C.columns,dtype=float)
    for ym in mr.index:                                                   # Heston-Sadka: same calendar month, 5-10 prior years
        prior=[p for p in (f'{int(ym[:4])-k}{ym[4:]}' for k in range(1,11)) if p in mr.index]
        if len(prior)>=5:seas.loc[ym]=mr.loc[prior].mean()
    nxt=pd.Series(C.index,index=C.index).shift(-1).fillna(C.index[-1])    # score for the month the next session trades in
    I['seasonal']=pd.DataFrame(seas.reindex(nxt.str[:7].values).values,index=C.index,columns=C.columns)
    return I

def candidates(P,I,elig):
    C=P['c'];T=I['tradable'];m121=I['mom121'];lead=m121.where(elig&(m121>0)).rank(axis=1,pct=True)
    sec=pd.Series(P['sector']).reindex(C.columns)
    secmom=I['mom61'].where(T).T.groupby(sec.values).mean().T.where(T.T.groupby(sec.values).sum().T>=5)
    top3=secmom.rank(axis=1,ascending=False)<=3
    insec=pd.DataFrame({s:top3[sec[s]] if sec[s] in top3 else False for s in C.columns},index=C.index)
    big100=I['turn20'].where(T).rank(axis=1,ascending=False)<=100
    gap=(I['gap']>=.04)&(I['gapvol']>=3)&(C>=P['o'])
    z=lambda X:X.where(elig).sub(X.where(elig).mean(axis=1),axis=0).div(X.where(elig).std(axis=1),axis=0)
    return {
     'ref_momentum':dict(score=I['mom61'].where(elig&(I['mom61']>0)),keep=25,stop=.20),     # live Momentum basket
     'ref_lowvol':dict(score=(-I['vol120']).where(elig),keep=30,stop=.15),                 # live Low volatility basket
     'ref_high52':dict(score=I['high52'].where(elig),keep=30,stop=.20),                    # live 52-week high basket
     'mom121':dict(score=m121.where(elig&(m121>0)),keep=30,stop=.20),
     'alpha':dict(score=I['alpha'].where(elig&(I['alpha']>0)),keep=30,stop=.20),
     'volmom':dict(score=((z(I['r6_vol'])+z(I['r12_vol']))/2).where(elig&(m121>0)),keep=30,stop=.20),
     'resmom':dict(score=I['resmom'].where(elig&(I['resmom']>0)),keep=30,stop=.20),
     'fip':dict(score=(-I['fip_id']).where(lead>=.8),keep=30,stop=.20),
     'sector':dict(score=I['mom61'].where(elig&insec&(I['mom61']>0)),keep=30,stop=.20),
     'overnight':dict(score=I['overnight'].where(elig&(I['overnight']>0)),keep=30,stop=.20),
     'volume':dict(score=I['volratio'].where(elig&(I['volratio']>=2)&(I['ret5']>0)),keep=30,stop=.15),
     'gap_drift':dict(score=I['gapvol'].where(elig&gap),monthly=False,max_hold=60,stop=.15),
     'pullback':dict(score=m121.where((lead>=.9)&(C<=I['dma50']*1.02)),exit_score=m121.where(lead>=.8),keep=10**6,stop=.15),
     'seasonal':dict(score=I['seasonal'].where(elig),keep=30,stop=.20),
     'lowmax':dict(score=(-I['max21']).where(elig),keep=30,stop=.15),
     'strev':dict(score=(-I['ret21']).where(elig&big100),keep=30,stop=.15),
    }

def simulate(P,I,score,exit_score=None,n=10,keep=30,stop=.20,max_hold=None,slip=.0025,mkt=False,start=START,end='9999',monthly=True,capital=1e5):
    """score/exit_score: higher is better, NaN is ineligible, read at the prior session's close.
    monthly: holdings outside the top `keep` of exit_score sell at the first session of a month; empty slots
    refill daily from the top `n`. Otherwise exits are stop and max_hold only. mkt: no buys while Nifty 500 < 200-DMA."""
    O,L,C=P['o'].values,P['l'].values,P['c'].values;cal=list(P['c'].index);cols=list(P['c'].columns);col={s:j for j,s in enumerate(cols)}
    Sv=score.values;Ev=(exit_score if exit_score is not None else score).values;up=I['mkt_up'].values
    days=[(t,d) for t,d in enumerate(cal) if start<=d<=end and t>0]
    cash=float(capital);hold={};trades=[];curve=[];fees=0.;mark={}
    def order(row):
        idx=np.where(~np.isnan(row))[0];return [cols[j] for j in idx[np.argsort(-row[idx],kind='stable')]]
    for k,(t,d) in enumerate(days):
        p=t-1;equity=cash+sum(h['q']*mark.get(s,h['px']) for s,h in hold.items());sells=set()
        if monthly and d[:7]!=cal[p][:7]:sells={s for s in hold if s not in set(order(Ev[p])[:keep])}
        if max_hold:sells|={s for s,h in hold.items() if k-h['k']>=max_hold}
        sold=set()
        for s in list(hold):
            o=O[t,col[s]];h=hold[s];level=h['px']*(1-stop)
            if np.isnan(o):continue
            px=o if s in sells or o<=level else level if L[t,col[s]]<=level else None
            if px is None:continue
            v=h['q']*px*(1-slip);f=fee(v,'SELL');cash+=v-f;fees+=f;sold.add(s)
            trades.append(dict(s=s,entry=h['d'],exit=d,pnl=v-f-h['cost'],days=k-h['k']));del hold[s]
        if not mkt or up[p]:
            for s in order(Sv[p])[:n]:
                if len(hold)>=n:break
                o=O[t,col[s]]
                if s in hold or s in sold or np.isnan(o) or o<=0:continue
                price=o*(1+slip);q=int((min(cash,equity/n)-25)//(price*1.0025))
                if q<1:continue
                v=q*price;f=fee(v,'BUY')
                if v+f>cash or f+fee(v,'SELL')>v*.015:continue                 # live 1.5% round-trip fee cap
                cash-=v+f;fees+=f;hold[s]=dict(q=q,px=price,cost=v+f,d=d,k=k)
        for s in hold:
            c=C[t,col[s]]
            if not np.isnan(c):mark[s]=c
        curve.append(cash+sum(h['q']*mark.get(s,h['px']) for s,h in hold.items()))
    trades+=[dict(s=s,entry=h['d'],exit=None,pnl=h['q']*mark.get(s,h['px'])-h['cost'],days=None) for s,h in hold.items()]
    return pd.Series(curve,index=[d for _,d in days])/capital*1e5,trades,fees

def hold_curve(I,start=START,mask=None):
    """Equal-weight daily return of the tradable universe, no costs: the survivorship-matched benchmark."""
    m=I['tradable'] if mask is None else I['tradable']&mask;d=I['ret'].where(m.shift(1,fill_value=False)).mean(axis=1).fillna(0)
    return 1e5*(1+d[d.index>=start]).cumprod()

def stats(e,a=None,b=None):
    if a:e=e[(e.index>=a)&(e.index<=b)];e=e/e.iloc[0]*1e5
    r=e.pct_change().dropna();cagr=(e.iloc[-1]/e.iloc[0])**(250/len(e))-1
    return dict(cagr=round(100*cagr,2),maxdd=round(-100*(e/e.cummax()-1).min(),1),sharpe=round(r.mean()/r.std()*math.sqrt(250),2) if r.std()>0 else None)

def research(only=None,rebuild=False):
    P=load(rebuild);I=indicators(P);T=I['tradable'];E=T&I['trend']
    big200=I['turn20'].where(T).rank(axis=1,ascending=False)<=200
    base,free,large=candidates(P,I,E),candidates(P,I,T),candidates(P,I,E&big200)
    ew,ewL,idx=hold_curve(I),hold_curve(I,mask=big200),P['index'][P['index'].index>=START]
    bench={k:dict(full=stats(c),h1=stats(c,*H1),h2=stats(c,*H2)) for k,c in (('ew',ew),('ew_top200',ewL),('nifty500',idx))}
    out=dict(created=time.strftime('%Y-%m-%d %H:%M'),universe_per_year=T.groupby(T.index.str[:4]).mean().sum(axis=1).round().to_dict(),
        share_in_current_nifty500=round(float((T&I['cur500']).sum().sum()/T.sum().sum()),3),bench=bench,results={});curves={}
    print('benchmarks',json.dumps(bench),flush=True)
    for name,spec in base.items():
        if only and name not in only:continue
        start='2022-01-01' if name=='seasonal' else START;res={}
        for var,kw in {'base':{},'n15':dict(n=15,capital=1.5e5,keep=min(10**6,int(spec.get('keep',30)*1.5))),'slip2x':dict(slip=.005),'mkt':dict(mkt=True)}.items():
            e,tr,fees=simulate(P,I,start=start,**{**spec,**kw});closed=[x for x in tr if x['exit']]
            gain=sum(x['pnl'] for x in closed if x['pnl']>0);loss=-sum(x['pnl'] for x in closed if x['pnl']<=0)
            total=e.iloc[-1]*(kw.get('capital',1e5)/1e5)-kw.get('capital',1e5);top5=sum(sorted((x['pnl'] for x in tr),reverse=True)[:5])
            res[var]=dict(full=stats(e),h1=stats(e,*H1) if start<H1[1] else None,h2=stats(e,*H2),trades=len(closed),
                win_rate=round(sum(x['pnl']>0 for x in closed)/len(closed),3) if closed else None,profit_factor=round(gain/loss,2) if loss else None,
                fees=round(fees),pnl=round(total),pnl_ex_top5=round(total-top5),years={y:round(100*(g.iloc[-1]/g.iloc[0]-1),1) for y,g in e.groupby(e.index.str[:4])})
            if var=='base':curves[name]=e.pct_change()
        res['no_trend_filter']=dict(full=stats(simulate(P,I,start=start,**free[name])[0]))
        e=simulate(P,I,start=start,**large[name])[0];res['top200']=dict(full=stats(e),h1=stats(e,*H1) if start<H1[1] else None,h2=stats(e,*H2))
        b=res['base'];h1ok=start>=H1[1] or (b['h1']['cagr']>bench['ew']['h1']['cagr'] and res['top200']['h1']['cagr']>bench['ew_top200']['h1']['cagr'])
        checks=dict(beats_hold=b['full']['cagr']>bench['ew']['full']['cagr'],both_halves=h1ok and b['h2']['cagr']>bench['ew']['h2']['cagr'],
            drawdown=b['full']['maxdd']<=bench['ew']['full']['maxdd']+5,ex_top5=b['pnl_ex_top5']>0,
            n15=res['n15']['full']['cagr']>bench['ew']['full']['cagr'],slip2x=res['slip2x']['full']['cagr']>bench['ew']['full']['cagr'],
            top200=res['top200']['full']['cagr']>bench['ew_top200']['full']['cagr'] and res['top200']['h2']['cagr']>bench['ew_top200']['h2']['cagr'])
        res['failed']=[k for k,v in checks.items() if not v];res['passed']=not res['failed'];out['results'][name]=res
        print(f"{name:13} cagr {b['full']['cagr']:6} h1 {(b['h1'] or {}).get('cagr','-'):>6} h2 {b['h2']['cagr']:6} dd {b['full']['maxdd']:5} trades {b['trades']:4} | top200 {res['top200']['full']['cagr']:6} | "+('PASS' if res['passed'] else 'fail: '+','.join(res['failed'])),flush=True)
    out['correlation']=pd.DataFrame(curves).corr().round(2).to_dict()
    OUT.write_text(json.dumps(out,indent=1));print('saved',OUT)

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('command',choices=('fetch','run'));a.add_argument('--only');a.add_argument('--rebuild',action='store_true')
    args=a.parse_args()
    if args.command=='fetch':fetch()
    else:research(set(args.only.split(',')) if args.only else None,args.rebuild)
