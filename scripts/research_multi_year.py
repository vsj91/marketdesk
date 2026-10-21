#!/usr/bin/env python3
"""Multi-year backtests for the baskets scripts/research_baskets.py does not cover, and the summary behind
the Backtests page (/backtests.html). Read-only: never touches ledgers or the candles table. Paper only.

  trader    Live Trader V2 rules (claude_trader.features/eligible/quantity) on 2018-2026 adjusted daily candles,
            point-in-time top-500 universe, Nifty 500 200-day filter; exits as scripts/backtest_trader_v2.py.
  ema       EMA + Price Action (backtest_signal_baskets.ema_backtest) on Upstox 5-minute candles 2022-2026; each year
            trades the 40 most traded stocks (close >= 50) of the 35 days before 1 January; capital resets yearly.
  news      News Momentum (backtest_signal_baskets.news_backtest) on NSE announcements 2019-2026 (gzip cache under
            data/backtests/news-archive) and adjusted daily candles; entries only inside that month's top 500.
  quant     The four quant baskets in the live BasketEngine (paper mode, temporary ledger) from June 2023 on
            point-in-time annual records (period end + 60 days). FY2023-FY2025 lack finance costs, borrowings and
            leases, so earnings yield and debt/equity drop out of value and quality before FY2026.
  investor  Investor proxy from June 2024: the live entry ranking and gates on point-in-time records (period end +
            75 days); the interest-cover and net-debt gates apply only to FY2026 records, which alone have them.
  summary   Writes data/backtests/basket-backtests.json from all results, including the uniqueness check
            (excess-return correlation <= 0.65 and holdings overlap <= 50% against every active rule basket).

Run research_baskets.py fetch/run first; it caches the daily candles and point-in-time universe used here."""
import argparse,bisect,calendar,gzip,inspect,importlib.util,json,pickle,sqlite3,sys,time
from datetime import date,datetime,timedelta
from multiprocessing import Pool
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
import research_baskets as rb
import marketdesk.claude_trader as ct
from marketdesk.execution import charges
from marketdesk.history_rules import is_financial
OUT=ROOT/'data/backtests';DB=ROOT/'data/marketdesk.sqlite3';SLIP=.0025
FILES=dict(trader='trader-10y.json',ema='ema-2022-2026.json',news='news-momentum-2019-2026.json',quant='quant-pit-2023-2026.json',investor='investor-pit-2024-2026.json',vinay='vinay-intraday-2022-2026.json')

def signal_module():
    spec=importlib.util.spec_from_file_location('bsb',ROOT/'scripts/backtest_signal_baskets.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
def ro():db=sqlite3.connect(f'file:{DB}?mode=ro',uri=True);db.row_factory=sqlite3.Row;return db
def years(e):return {y:round(100*(g.iloc[-1]/g.iloc[0]-1),1) for y,g in e.groupby(e.index.str[:4])}
def rows_of(P,s):
    c=P['c'][s].dropna();o,h,l,v=(P[k][s].reindex(c.index) for k in 'ohlv')
    return [dict(ts=d+'T00:00:00+05:30',open=o[d],high=h[d],low=l[d],close=c[d],volume=v[d]) for d in c.index]

# ---- Trader ----------------------------------------------------------------------------------------

def _features(job):
    s,rows=job;f=ct.features(rows);return s,({x['as_of']:x for x in f} if f else {})

def trader_features(P,I):
    cache=OUT/'trader-features.pkl';pit=I['pit500'];ever=[s for s in pit.columns if pit[s].any()]
    if cache.exists():return pickle.load(open(cache,'rb'))+(ever,)
    jobs=[(s,rows_of(P,s)) for s in ever];by_day={s:{r['ts'][:10]:r for r in rows} for s,rows in jobs}
    with Pool(8) as pool:feats=dict(pool.map(_features,jobs,chunksize=8))
    pickle.dump((feats,by_day),open(cache,'wb'));return feats,by_day,ever

def trader():
    P=rb.load();I=rb.indicators(P);feats,by_day,ever=trader_features(P,I);pit=I['pit500']
    member={s:pit[s].to_dict() for s in ever};idx=P['index'];gate=(idx>idx.rolling(200,min_periods=200).mean()).to_dict()
    def run(use_gate,compound,start=rb.START,capital=1e5):
        days=[d for d in P['c'].index if d>=start];cash=capital;pos={};trades=[];curve=[];prev=None;fees=0.
        for i,day in enumerate(days):
            for s in list(pos):
                p=pos[s];bar=by_day[s].get(day)
                if not bar:continue
                px=bar['open'] if bar['open']<=p['stop'] else p['stop'] if bar['low']<=p['stop'] else bar['open'] if p['ma_exit'] else None
                if px:
                    v=p['qty']*px*(1-SLIP);f=charges(round(v,2),'SELL',True)['total'];cash+=v-f;fees+=f
                    trades.append(dict(s=s,entry=p['d'],exit=day,pnl=v-f-p['cost']));del pos[s];continue
                p['hi']=max(p['hi'],bar['close']);f=feats[s].get(day)
                if f and p['i']<i:p['stop']=max(p['stop'],p['hi']-3*f['atr'])      # 3-ATR trail from the next session
                if f and bar['close']<f['dma50']:p['ma_exit']=True
            if prev and (not use_gate or gate.get(prev)):
                clock=datetime.fromisoformat(day+'T09:20:00+05:30');cands=[]
                for s in ever:
                    f=feats[s].get(prev)
                    if f and s not in pos and member[s].get(day) and by_day[s].get(day) and ct.eligible(f,by_day[s][day]['open'],clock,dict(as_of=prev,allowed=True),use_market_filter=False):cands.append((f['rank'],s,f))
                for _,s,f in sorted(cands,reverse=True):
                    if len(pos)>=10:break
                    bar=by_day[s][day];price=bar['open']*(1+SLIP)
                    eq=cash+sum(q['qty']*by_day[k].get(day,{}).get('close',q['px']) for k,q in pos.items())
                    heat=sum(max(0,q['cost']-q['qty']*q['stop']+charges(round(q['qty']*q['stop'],2),'SELL',True)['total'])+q['qty']*q['px']*.005 for q in pos.values())
                    qty=ct.quantity(price*1.005,f['atr'],eq,heat,min(cash,(eq if compound else capital)*.1))
                    if qty<1:continue
                    v=qty*price;f2=charges(round(v,2),'BUY')['total']
                    if v+f2>cash or f2+charges(round(v,2),'SELL',True)['total']>v*.015:continue
                    cash-=v+f2;fees+=f2;pos[s]=dict(qty=qty,px=price,cost=v+f2,stop=price-2*f['atr'],hi=bar['close'],ma_exit=False,d=day,i=i)
            curve.append(cash+sum(p['qty']*by_day[s].get(day,{}).get('close',p['px']) for s,p in pos.items()));prev=day
        return pd.Series(curve,index=days)/capital*1e5,trades,fees
    ew=rb.hold_curve(I);out={}
    for tag,g,c in (('live_rules_nifty_filter',True,False),('nifty_filter_compounding',True,True),('no_filter',False,False)):
        e,tr,fees=run(g,c);wins=[t for t in tr if t['pnl']>0];loss=-sum(t['pnl'] for t in tr if t['pnl']<=0)
        out[tag]=dict(window=[e.index[0],e.index[-1]],full=rb.stats(e),h1=rb.stats(e,*rb.H1),h2=rb.stats(e,*rb.H2),trades=len(tr),win_rate=round(len(wins)/len(tr),3) if tr else None,
            profit_factor=round(sum(t['pnl'] for t in wins)/loss,2) if loss else None,fees=round(fees),years=years(e))
        print(tag,json.dumps(out[tag]),flush=True)
    out['bench']=dict(ew=dict(full=rb.stats(ew),h1=rb.stats(ew,*rb.H1),h2=rb.stats(ew,*rb.H2)))
    (OUT/FILES['trader']).write_text(json.dumps(out,indent=1))

# ---- EMA + Price Action ----------------------------------------------------------------------------

def ema():
    bsb=signal_module();P=rb.load();C,V=P['c'],P['v'];db=bsb.ro(DB);out={}
    for y in range(2022,date.today().year+1):
        a=f'{y}-01-01';b=min(f'{y}-12-31',C.index[-1]);lo=(date.fromisoformat(a)-timedelta(days=35)).isoformat()
        w=(C*V)[(C.index>=lo)&(C.index<a)];ok=C[(C.index>=lo)&(C.index<a)].min()>=50
        uni=list(w.mean()[ok].sort_values(ascending=False).index[:40]);data=bsb.ema_data(db,uni,a,b)
        days=sorted({x['ts'][:10] for v in data.values() for x in v});res={}
        for tag,cfg in (('defaults',{}),('min_rr_1.5',{'min_rr':1.5})):
            r=bsb.ema_backtest(data,cfg,start=days[3]);res[tag]=dict(metrics=r['metrics'],exits=r['exits'])
            print(y,tag,{k:r['metrics'][k] for k in ('trades','win_rate_pct','profit_factor','net_return_pct','max_drawdown_pct','costs')},flush=True)
        idx=P['index'][(P['index'].index>=a)&(P['index'].index<=b)];res.update(nifty500_pct=round(100*(idx.iloc[-1]/idx.iloc[0]-1),2),universe=uni,window=[days[0],days[-1]])
        out[str(y)]=res
    (OUT/FILES['ema']).write_text(json.dumps(out,indent=1,default=float))

# ---- News Momentum ---------------------------------------------------------------------------------

def news_fetch(first=date(2019,1,1)):
    from marketdesk.signal_baskets.news import NseAnnouncements
    folder=OUT/'news-archive';folder.mkdir(parents=True,exist_ok=True);p=NseAnnouncements(min_interval=2.);a=first
    while a<=date.today():
        b=(a.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1);f=folder/f'{a:%Y-%m}.json.gz'
        if not f.exists() and b<date.today():                        # completed months only
            for attempt in range(4):
                try:f.write_bytes(gzip.compress(json.dumps(p.fetch(a,b)).encode()));print('news',a,flush=True);break
                except Exception as error:print(a,'retry',error,flush=True);time.sleep(10*(attempt+1));p=NseAnnouncements(min_interval=2.)
        a=b+timedelta(days=1)

def news(start='2019-01-01'):
    news_fetch();bsb=signal_module();P=rb.load();I=rb.indicators(P);pit=I['pit500'];end=P['c'].index[-1]
    src=inspect.getsource(bsb.news_backtest)
    for old,new in (   # same rules; candles and benchmark from the ten-year cache, entries inside the point-in-time top 500
     ("""    candles=collections.defaultdict(list)
    for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles ORDER BY symbol,ts'):candles[r['symbol']].append(dict(r))
    bench=[(r['day'],r['close']) for r in db.execute('SELECT day,close FROM claude_benchmark ORDER BY day')]""","    candles=CANDLES;bench=BENCH"),
     ("    def before(s,day):return [b for b in candles.get(s,[]) if b['ts'][:10]<day]","    def before(s,day):return candles.get(s,[])[:bisect.bisect_left(DATES.get(s,[]),day)]"),
     ("bars=[b for b in candles.get(s,[]) if b['ts'][:10]>=sess]","bars=candles.get(s,[])[bisect.bisect_left(DATES.get(s,[]),sess):]"),
     ("if status=='BUY_CANDIDATE':orders","if status=='BUY_CANDIDATE' and MEMBER.get(s,{}).get(bars[1]['ts'][:10]):orders"),
     ("    days=sorted({b['ts'][:10] for s in candles for b in candles[s][-200:]})","    days=DAYS"),
     ("ctx=news_rules.daily_context(candles[s][:i+1])","ctx=news_rules.daily_context(candles[s][max(0,i-299):i+1])")):
        if old not in src:raise RuntimeError('news_backtest changed; update the adapter: '+old[:50])
        src=src.replace(old,new)
    candles={};dates={}
    for s in P['c'].columns:
        rows=rows_of(P,s)
        if rows:candles[s]=[dict(r,symbol=s) for r in rows];dates[s]=[r['ts'][:10] for r in rows]
    ns=dict(vars(bsb));ns.update(CANDLES=candles,BENCH=list(P['index'].items()),DATES=dates,DAYS=[d for d in P['c'].index if start<=d<=end],bisect=bisect,
        MEMBER={s:pit[s][pit[s]].to_dict() for s in pit.columns if pit[s].any()});exec(src,ns)
    rows=[]
    for f in sorted((OUT/'news-archive').glob('*.json.gz')):
        if start[:7]<=f.name[:7]<=end[:7]:rows+=json.loads(gzip.decompress(f.read_bytes()))
    r=ns['news_backtest'](bsb.ro(DB),rows,{},split='2023-01-01');by={}
    for t in r['trades']:by.setdefault(t['day'][:4],[]).append(t['pnl'])
    r.update(window=[start,end],announcements=len(rows),by_year={y:dict(trades=len(v),pnl=round(sum(v)),wins=sum(x>0 for x in v)) for y,v in sorted(by.items())})
    print(json.dumps({k:r[k] for k in ('metrics','exits','by_year')},default=float),flush=True)
    (OUT/FILES['news']).write_text(json.dumps(r,indent=1,default=float))

# ---- Fundamentals (quant baskets, Investor proxy) -----------------------------------------------------

def fundamentals_records(db):
    """{symbol: [annual record...]} oldest first, from fundamentals.annual plus revenue/net profit/operating cash flow histories."""
    def end(p):d=datetime.strptime(p,'%b %Y');return date(d.year,d.month,calendar.monthrange(d.year,d.month)[1])
    def hist(payload,section,cat):
        try:rows=json.loads(payload)['data'].get(section) or []
        except Exception:return {}
        for r in rows:
            if r.get('category')==cat:return {h['period']:h['value'] for h in r.get('history') or [] if isinstance(h.get('value'),(int,float))}
        return {}
    pay={(r['symbol'],r['endpoint']):r['payload'] for r in db.execute("SELECT symbol,endpoint,payload FROM payloads WHERE endpoint IN ('income_statement','cash_flow')")}
    sectors=dict(db.execute('SELECT symbol,sector FROM instruments'));out={}
    for r in db.execute('SELECT symbol,details FROM fundamentals'):
        d=json.loads(r['details']);s=r['symbol'];sector=d.get('sector') or sectors.get(s)
        rev=hist(pay.get((s,'income_statement')),'income_statement','revenue');np_=hist(pay.get((s,'income_statement')),'income_statement','net_profit')
        ocf=hist(pay.get((s,'cash_flow')),'cash_flow','operating');merged={}
        for a in d.get('annual',[]):
            if a.get('period'):
                m=merged.setdefault(a['period'],{})
                for k,v in a.items():
                    if isinstance(v,(int,float)) and m.get(k) is None:m[k]=v
        recs=[]
        for p,a in merged.items():
            try:pe=end(p)
            except ValueError:continue
            prev=f'{p[:4]}{int(p[4:])-1}';g=lambda h:(h[p]/h[prev]-1)*100 if p in h and prev in h and h[prev] and h[prev]>0 else None
            recs.append(dict(period=p,period_end=pe.isoformat(),pbt=a.get('profit_before_tax'),finance=a.get('finance_costs'),equity=a.get('equity'),
                cash=a.get('cash_and_cash_equivalents'),total_debt=a['total_borrowings']+(a.get('total_lease_liabilities') or 0) if a.get('total_borrowings') is not None else None,
                net_profit=np_.get(p),ocf=ocf.get(p),rev_growth=g(rev),np_growth=g(np_),financial=is_financial(sector or '',s),
                ebit=a['profit_before_tax']+a['finance_costs'] if a.get('profit_before_tax') is not None and a.get('finance_costs') is not None else None))
        out[s]=sorted(recs,key=lambda x:x['period_end'])
    return out

def pit_record(records,s,day,lag):
    best=None
    for x in records.get(s,[]):
        if (date.fromisoformat(x['period_end'])+timedelta(days=lag)).isoformat()<=day:best=x
    return best

def quant(start='2023-06-01'):
    from marketdesk.quant.engine import BasketEngine
    from marketdesk.storage import connect
    P=rb.load();I=rb.indicators(P);pit=I['pit500'];C=P['c'];db=ro();records=fundamentals_records(db)
    inst={r['symbol']:dict(r) for r in db.execute('SELECT symbol,isin,sector FROM instruments')}
    amfi=json.loads((db.execute("SELECT value FROM state WHERE key='amfi_classification'").fetchone() or ['{}'])[0]);by_isin=amfi.get('records',{});period=amfi.get('period')
    size={s:by_isin[i['isin']]['label'] for s,i in inst.items() if i.get('isin') in by_isin}
    caps={s:by_isin[i['isin']].get('average_market_cap_crore') for s,i in inst.items() if i.get('isin') in by_isin}
    lo=(date.fromisoformat(period)-timedelta(days=182)).isoformat();win=C[(C.index>=lo)&(C.index<=period)];anchor=win.mean()[win.count()>=60].to_dict()
    members={s:set(pit.index[pit[s]]) for s in pit.columns if pit[s].any()};bars_all={};days_all={}
    for s in members:
        rows=rows_of(P,s);bars_all[s]=[dict(day=r['ts'][:10],open=r['open'],high=r['high'],low=r['low'],close=r['close'],volume=r['volume']) for r in rows];days_all[s]=[r['ts'][:10] for r in rows]
    bench=list(P['index'].items());bdays=[d for d,_ in bench];ledger=OUT/'quant-pit.sqlite3'
    for f in OUT.glob('quant-pit.sqlite3*'):f.unlink()
    eng=BasketEngine(ledger)
    for name,strategy in {'smart_beta':'multifactor','risk_parity':'risk_parity','stat_arb':'stat_arb','regime':'regime'}.items():eng.create(name,strategy,100000,day=start)
    for k,day in enumerate(d for d in C.index if d>=start):
        with connect(ledger) as l:held={r[0] for r in l.execute('SELECT symbol FROM qb_positions')}
        bars={};funds={}
        for s in members:
            if day not in members[s] and s not in held:continue        # universe of the day plus anything still held
            n=bisect.bisect_right(days_all[s],day)
            if not n:continue
            bars[s]=bars_all[s][max(0,n-320):n];rec=pit_record(records,s,day,60)
            if rec:
                cap=caps.get(s);a=anchor.get(s)
                if cap and a:cap=cap*bars[s][-1]['close']/a              # AMFI average cap scaled by price, as quant.data
                funds[s]=dict(rec,market_cap=cap,dividend_yield=None,ev=cap+(rec['total_debt'] or 0)-(rec['cash'] or 0) if cap and rec['total_debt'] is not None else None)
        n=bisect.bisect_right(bdays,day)
        eng.run_day(dict(day=day,bars=bars,fundamentals=funds,sectors={s:(inst.get(s) or {}).get('sector') for s in bars},size=size,
            benchmark=[c for _,c in bench[max(0,n-320):n]],benchmark_day=bdays[n-1],confirmed={}))
        if k%60==0:print('quant',day,flush=True)
    res={};ew=rb.hold_curve(I,start=start);h1,h2=(start,'2024-12-31'),('2025-01-01','2026-12-31')
    with connect(ledger) as l:
        for name in eng.names():
            rows=l.execute('SELECT day,nav,fees FROM qb_nav WHERE basket=? ORDER BY day',(name,)).fetchall()
            e=pd.Series([r[1] for r in rows],index=[r[0] for r in rows]);e=e/e.iloc[0]*1e5
            res[name]=dict(window=[rows[0][0],rows[-1][0]],full=rb.stats(e),h1=rb.stats(e,*h1),h2=rb.stats(e,*h2),fees=round(sum(r[2] or 0 for r in rows)),
                fills=l.execute('SELECT COUNT(*) FROM qb_fills WHERE basket=?',(name,)).fetchone()[0],years=years(e))
            print(name,json.dumps(res[name]),flush=True)
    res['bench']=dict(ew=dict(full=rb.stats(ew),h1=rb.stats(ew,*h1),h2=rb.stats(ew,*h2),years=years(ew)),nifty500=rb.stats(P['index'][P['index'].index>=start]))
    (OUT/FILES['quant']).write_text(json.dumps(res,indent=1))

def investor(start='2024-06-14'):
    P=rb.load();I=rb.indicators(P);C=P['c'];pit=I['pit500'];records=fundamentals_records(ro());feats,by_day,_=trader_features(P,I)
    members={s:set(pit.index[pit[s]]) for s in pit.columns if pit[s].any()}
    def score(s,f,price):
        rec=pit_record(records,s,f['as_of'],75)
        if not rec or price<20 or f['turnover20']<1e7 or abs(price/f['close']-1)>.15 or price<=f['dma200']:return None
        if (date.fromisoformat(f['as_of'])-date.fromisoformat(rec['period_end'])).days>550:return None
        pbt,eq,cash,rg,pg=rec['pbt'],rec['equity'],rec['cash'],rec['rev_growth'],rec['np_growth']
        if None in (pbt,eq,cash,rg,pg) or pbt<=0 or eq<=0 or cash<0 or min(rg,pg)<=0:return None
        if rec['finance'] is not None and rec['total_debt'] is not None:   # live debt gates where the record has the inputs
            if rec['finance'] and (pbt+rec['finance'])/rec['finance']<3 or (rec['total_debt']-cash)/eq>.5:return None
        return .5*(min(rg,100)+min(pg,100))/200+.3*min(max(pbt/eq,0),.4)/.4+.2*min(max(price/f['dma200']-1,0),.5)/.5   # autotrading.entry_signal ranking
    days=[d for d in C.index if d>=start];cash=1e5;pos={};trades=[];curve=[];prev=None;fees=0.
    for day in days:
        for s in list(pos):
            p=pos[s];bar=by_day[s].get(day)
            if not bar:continue
            lvl=p['px']*.9;px=bar['open'] if bar['open']<=lvl or p['ma_exit'] else lvl if bar['low']<=lvl else None
            if px:
                v=p['qty']*px*(1-SLIP);f=charges(round(v,2),'SELL',True)['total'];cash+=v-f;fees+=f;trades.append(dict(s=s,exit=day,pnl=v-f-p['cost']));del pos[s];continue
            f=feats[s].get(day)
            if f and bar['close']<f['dma200']:p['ma_exit']=True
        if prev:
            cands=sorted(((sc,s) for s in members if s not in pos and day in members[s] and by_day.get(s,{}).get(day)
                for f in [feats.get(s,{}).get(prev)] if f for sc in [score(s,f,by_day[s][day]['open'])] if sc is not None),reverse=True)
            for _,s in cands:
                if len(pos)>=10:break
                price=by_day[s][day]['open']*(1+SLIP);qty=int(min(cash,10000)//price)
                if qty<1:continue
                v=qty*price;f=charges(round(v,2),'BUY')['total']
                if v+f>cash:continue
                cash-=v+f;fees+=f;pos[s]=dict(qty=qty,px=price,cost=v+f,ma_exit=False)
        curve.append(cash+sum(p['qty']*by_day[s].get(day,{}).get('close',p['px']) for s,p in pos.items()));prev=day
    e=pd.Series(curve,index=days);ew=rb.hold_curve(I,start=start);h1,h2=(start,'2025-07-31'),('2025-08-01','2026-12-31')
    res=dict(window=[days[0],days[-1]],full=rb.stats(e),h1=rb.stats(e,*h1),h2=rb.stats(e,*h2),trades=len(trades),open_positions=len(pos),
        win_rate=round(sum(t['pnl']>0 for t in trades)/len(trades),3) if trades else None,fees=round(fees),years=years(e),
        bench=dict(ew=dict(full=rb.stats(ew),h1=rb.stats(ew,*h1),h2=rb.stats(ew,*h2),years=years(ew)),nifty500=rb.stats(P['index'][P['index'].index>=start])))
    print(json.dumps(res),flush=True);(OUT/FILES['investor']).write_text(json.dumps(res,indent=1))

# ---- Summary for the Backtests page -------------------------------------------------------------------

RULE_BASKETS={'ref_momentum':'momentum','ref_lowvol':'lowvol','ref_high52':'high52','mom121':'momentum12','alpha':'alpha',
    'overnight':'overnight','gap_drift':'gap_drift','sector':'sector_rotation','clenow':'clenow',
    # Research baskets traded on paper for comparison (marketdesk/research_live.py).
    'volmom':'vol_momentum','resmom':'residual_momentum','fip':'smooth_momentum','volume':'volume_surge','pullback':'pullback','lowmax':'low_max',
    'strev':'reversal','minervini':'minervini','conservative':'conservative','turtle':'turtle','connors':'connors','dual_mom':'dual_momentum',
    'golden_cross':'golden_cross','macd':'macd','bollinger_rsi':'bollinger_rsi'}
FUND_BASKETS={'quality_value':'quality_value','magic_proxy':'magic_formula','net_buyback':'net_buyback','dividend_quality':'dividend_quality','value_momentum':'value_momentum'}
TITLES={'ref_momentum':'Momentum 6-1 (existing)','ref_lowvol':'Low volatility (existing)','ref_high52':'52-week high (retired)','mom121':'Momentum 12-1',
    'alpha':'Alpha (NSE Alpha-style)','volmom':'Volatility-adjusted momentum (NSE Momentum 30-style)','resmom':'Residual momentum','fip':'Smooth momentum',
    'sector':'Sector rotation','overnight':'Overnight momentum','volume':'Volume surge','gap_drift':'Gap-up drift','pullback':'Pullback in market leaders',
    'seasonal':'Same-month seasonality','lowmax':'Low max-return (lottery avoidance)','strev':'Short-term reversal, large caps',
    'minervini':'Minervini trend template','clenow':'Stocks on the Move (Clenow)','conservative':'Conservative Formula, price only',
    'turtle':'Turtle breakout 55/20','connors':'Connors RSI-2 dip buying','dual_mom':'Dual Momentum (Antonacci)',
    'golden_cross':'Golden cross 50/200 EMA + ADX','macd':'MACD signal-line cross','bollinger_rsi':'Bollinger Band + RSI-14 dip buying'}
FAMILIES={'mom121':'Jegadeesh-Titman','alpha':'one-year Jensen alpha vs Nifty 500','volmom':'6- and 12-month return over volatility','resmom':'Blitz-Huij-Martens',
    'fip':'Da-Gurun-Warachka frog-in-the-pan','sector':'Moskowitz-Grinblatt','overnight':'Lou-Polk-Skouras','volume':'Gervais-Kaniel-Mingelgrin',
    'gap_drift':'post-announcement drift proxy','pullback':'leaders at the 50-day average','seasonal':'Heston-Sadka','lowmax':'Bali-Cakici-Whitelaw','strev':'Jegadeesh one-month reversal',
    'minervini':'SEPA trend template + IBD-style relative strength, 8% stop','clenow':'90-day regression slope x R squared, index filter',
    'conservative':'van Vliet-Blitz: 12-1 momentum in the calmer half','turtle':'Dennis Turtles: 55-day breakout, 20-day low exit, 2 ATR stop',
    'connors':'RSI(2) under 5 above the 200-day average, exit above the 5-day average','dual_mom':'12-1 momentum, cash when the index trails T-bills',
    'golden_cross':'retail algo guides: 50/200 EMA cross with ADX(14) > 20','macd':'retail algo guides: MACD(12,26,9) cross above zero',
    'bollinger_rsi':'retail algo guides: lower Bollinger band with RSI(14) < 30'}
FUND_TITLES={'quality_value':'Quality at a reasonable price (Buffett-style)','magic_proxy':'Magic Formula (Greenblatt), proxy','net_buyback':'Net buybacks',
    'dividend_quality':'Dividend yield with quality','value_momentum':'Value and momentum'}
FUND_FAMILIES={'quality_value':'ROE 15%+ two years, rising profit, interest cover 5+, cheapest earnings first','magic_proxy':'EBIT / market cap + EBIT / equity; no debt or cash before FY2023',
    'net_buyback':'Pontiff-Woodgate: share count down 1%+ in a year','dividend_quality':'trailing dividend yield, ROE 12%+, payout within profit',
    'value_momentum':'Asness-Moskowitz-Pedersen: earnings yield + 12-1 momentum'}
TWINS={'alpha':'Added by request as a twin of Momentum 12-1, despite failing the uniqueness rule'}
OVERRIDES={'strev':'Same idea as Stat-arb and the retired Mean reversion: buying sharp dips in rising large caps',
    'seasonal':'Needs 5+ years of monthly history; the app stores about 18 months'}
FAILS={'beats_hold':'behind the hold','both_halves':'behind in one half','drawdown':'deeper fall than the hold','ex_top5':'loses without its 5 best trades',
    'n15':'fails with 15 holdings','slip2x':'fails with double slippage','top200':'behind among the 200 most traded'}

def uniqueness(P,I,cands,active=('ref_momentum','ref_lowvol','mom121'),max_corr=.65,max_overlap=.5):
    """Greedy, best backtest first: a candidate is unique if it stays within both limits against every active
    rule basket and every candidate already accepted. Returns {name: (accepted, reason)}."""
    ew=rb.hold_curve(I).pct_change();excess={};held={}
    for n,spec in cands.items():
        e,tr,_=rb.simulate(P,I,start='2022-01-01' if n=='seasonal' else rb.START,**spec);excess[n]=e.pct_change()-ew.reindex(e.index);months={}
        for t in tr:
            for m in pd.period_range(t['entry'][:7],(t['exit'] or P['c'].index[-1])[:7],freq='M'):months.setdefault(str(m),set()).add(t['s'])
        held[n]=months
    corr=pd.DataFrame(excess).corr()
    def overlap(a,b):
        v=[len(held[a][m]&held[b][m])/max(1,min(len(held[a][m]),len(held[b][m]))) for m in held[a] if m in held[b]];return float(np.mean(v)) if v else 0.
    taken=list(active);out={}
    for n in sorted((n for n in cands if n not in active and n!='ref_high52'),key=lambda n:-(rb.stats(rb.simulate(P,I,**cands[n])[0])['cagr'] if n!='seasonal' else -99)):
        clash=[(o,corr.loc[n,o],overlap(n,o)) for o in taken if corr.loc[n,o]>max_corr or overlap(n,o)>max_overlap]
        if clash:
            o,c,v=max(clash,key=lambda x:x[1]);out[n]=(False,f'Too close to {TITLES[o]}: {c:.2f} excess-return correlation, {100*v:.1f}% shared holdings')
        else:out[n]=(True,None);taken.append(n)
    return out

def summary():
    R=json.loads((OUT/'research-baskets.json').read_text());P=rb.load();I=rb.indicators(P);ew=rb.hold_curve(I)
    def hold(a,b):h=rb.hold_curve(I,start=a);return rb.stats(h[h.index<=b])['cagr']
    def judge(full,h1,h2,hf,h1h,h2h):
        if full is None:return 'untested'
        return 'pass' if full>hf and h1>h1h and h2>h2h else 'mixed' if full>hf else 'fail'
    baskets={};universe='Point-in-time top 500 of all listed stocks'
    for key,name in RULE_BASKETS.items():
        r=R['results'][key];b=r['base']
        baskets[name]=dict(window=[rb.START if key!='seasonal' else '2022-01-01',P['c'].index[-1]],universe=universe,method='Basket rules on the point-in-time top 500, next-open fills, 0.25% slippage, live fees',
            full=b['full'],h1=b['h1'],h2=b['h2'],trades=b['trades'],years=b['years'],hold_cagr=R['bench']['ew']['full']['cagr'],
            verdict='pass' if r['passed'] else 'mixed' if 'beats_hold' not in r['failed'] else 'fail',
            note=('Failed: '+', '.join(FAILS[f] for f in r['failed']) if r['failed'] else 'Passed every check')+(f'. {TWINS[key]}' if key in TWINS else ''))
    files={k:json.loads((OUT/v).read_text()) for k,v in FILES.items() if (OUT/v).exists()}
    if 'trader' in files:
        t=files['trader']['live_rules_nifty_filter'];e=files['trader']['bench']['ew']
        baskets['trader']=dict(window=t['window'],universe=universe,method='Live Trader V2 rules with the Nifty 500 200-day filter',full=t['full'],h1=t['h1'],h2=t['h2'],
            trades=t['trades'],years=t['years'],hold_cagr=e['full']['cagr'],verdict=judge(t['full']['cagr'],t['h1']['cagr'],t['h2']['cagr'],e['full']['cagr'],e['h1']['cagr'],e['h2']['cagr']),
            note=f"Win rate {round(100*t['win_rate'])}%, profit factor {t['profit_factor']}")
    if 'investor' in files:
        t=files['investor'];e=t['bench']['ew']
        baskets['investor']=dict(window=t['window'],universe=universe,method='Proxy: live ranking and gates; debt and interest gates only on FY2026 records',full=t['full'],h1=t['h1'],h2=t['h2'],
            trades=t['trades'],years=t['years'],hold_cagr=e['full']['cagr'],verdict=judge(t['full']['cagr'],t['h1']['cagr'],t['h2']['cagr'],e['full']['cagr'],e['h1']['cagr'],e['h2']['cagr']),
            note=f"Win rate {round(100*t['win_rate'])}% on {t['trades']} closed trades; {t['open_positions']} still open")
    if 'quant' in files:
        q=files['quant'];e=q['bench']['ew']
        for name in ('smart_beta','risk_parity','stat_arb','regime'):
            t=q[name];baskets[name]=dict(window=t['window'],universe=universe,method='Live quant engine on point-in-time annual records; no earnings yield or debt/equity before FY2026',
                full=t['full'],h1=t['h1'],h2=t['h2'],trades=t['fills'],years=t['years'],hold_cagr=e['full']['cagr'],
                verdict=judge(t['full']['cagr'],t['h1']['cagr'],t['h2']['cagr'],e['full']['cagr'],e['h1']['cagr'],e['h2']['cagr']),note='Trades column counts fills')
    if 'news' in files:
        t=files['news'];m=t['metrics'];a,b=t['window'];yrs=(date.fromisoformat(b)-date.fromisoformat(a)).days/365.25
        cagr=round(100*((1+m['net_return_pct']/100)**(1/yrs)-1),2);mid='2023-01-01';split=t.get('in_sample',{}),t.get('out_of_sample',{})
        baskets['news_momentum']=dict(window=[a,b],universe=universe,method=f"Live screen, confirm and exit rules on {t['announcements']:,} NSE announcements",
            full=dict(cagr=cagr,maxdd=m['max_drawdown_pct'],sharpe=m['sharpe']),trades=m['trades'],hold_cagr=hold(a,b),
            years={y:round(100*v['pnl']/1e5,1) for y,v in t['by_year'].items()},
            verdict='pass' if cagr>hold(a,b) and split[0].get('pnl',0)>0 and split[1].get('pnl',0)>0 else 'mixed' if cagr>hold(a,b) else 'fail',
            note=f"Win rate {m['win_rate_pct']}%, profit factor {m['profit_factor']}; yearly figures are P&L as % of starting cash")
    if 'ema' in files:
        t=files['ema'];ys=sorted(t);growth=1.;dd=0.;trades=0
        for y in ys:m=t[y]['defaults']['metrics'];growth*=1+m['net_return_pct']/100;dd=max(dd,m['max_drawdown_pct']);trades+=m['trades']
        a,b=t[ys[0]]['window'][0],t[ys[-1]]['window'][1];yrs=(date.fromisoformat(b)-date.fromisoformat(a)).days/365.25;cagr=round(100*(growth**(1/yrs)-1),2)
        baskets['ema_price_action']=dict(window=[a,b],universe='40 most traded stocks as of each 1 January',method='Live 5-minute rules (defaults); capital resets each year',
            full=dict(cagr=cagr,maxdd=round(dd,2),sharpe=None),trades=trades,hold_cagr=hold(a,b),years={y:t[y]['defaults']['metrics']['net_return_pct'] for y in ys},
            verdict='pass' if cagr>hold(a,b) else 'fail',note='Worst fall is the worst within a single year')
    if 'vinay' in files:
        t=files['vinay'];a,b=t['window'];yrs=(date.fromisoformat(b)-date.fromisoformat(a)).days/365.25
        cagr=round(100*((1+t['compounded_pct']/100)**(1/yrs)-1),2)
        baskets['vinay_intraday']=dict(window=[a,b],universe=t['universe'],method='Live 5-minute rules (defaults, scripts/backtest_vinay_intraday.py); compounding, Upstox intraday charges',
            full=dict(cagr=cagr,maxdd=max(m['max_drawdown_pct'] for m in t['years'].values()),sharpe=None),trades=t['trades'],hold_cagr=hold(a,b),
            years={y:m['net_return_pct'] for y,m in t['years'].items()},verdict='pass' if cagr>hold(a,b) else 'fail',
            note='Lost money after fees in every year; worst fall is the worst within a single year')
    baskets['news_catalyst']=dict(verdict='untested',note='Holds 3-24 months on verified catalysts plus point-in-time financials; not testable with the stored history')
    baskets['reversion']=dict(verdict='fail',note='Retired: fees outweighed gains in the one-year test (scripts/backtest_baskets.py)')
    cands=rb.candidates(P,I,I['tradable']&I['trend']);unique=uniqueness(P,I,cands);candidates=[]
    for key,r in R['results'].items():
        b=r['base'];ok,why=unique.get(key,(key in RULE_BASKETS,None))
        candidates.append(dict(key=key,title=TITLES.get(key,key),family=FAMILIES.get(key),cagr=b['full']['cagr'],h1=(b['h1'] or {}).get('cagr'),h2=b['h2']['cagr'],maxdd=b['full']['maxdd'],
            top200=r['top200']['full']['cagr'],passed=r['passed'],failed=[FAILS[f] for f in r['failed']],basket=RULE_BASKETS.get(key),
            why_not=None if key in RULE_BASKETS else OVERRIDES.get(key) or why,twin_note=f'{TWINS[key]}: {why}' if key in TWINS and why else None))
    # Fundamentals rules (scripts/research_fundamentals.py) run on a shorter window with its own hold; each row says so.
    notes=[]
    if (OUT/'research-fundamentals.json').exists():
        Fr=json.loads((OUT/'research-fundamentals.json').read_text());fh=Fr['bench']['ew']['full']['cagr']
        for key,r in Fr['results'].items():
            b=r['base'];u=Fr.get('uniqueness',{}).get(key,{})
            candidates.append(dict(key=key,title=FUND_TITLES[key],family=f"{FUND_FAMILIES[key]} · tested Jun 2019–2026 vs {fh}% a year for the hold; first half from Jun 2019",
                cagr=b['full']['cagr'],h1=b['h1']['cagr'],h2=b['h2']['cagr'],maxdd=b['full']['maxdd'],top200=r['top200']['full']['cagr'],passed=r['passed'],
                failed=[FAILS[f] for f in r['failed']],basket=FUND_BASKETS.get(key),why_not=None if key in FUND_BASKETS else u.get('reason'),twin_note=None))
            if key in FUND_BASKETS:
                baskets[FUND_BASKETS[key]]=dict(window=Fr['window'],universe=universe,method='Point-in-time NSE annual results from period end + 60 days, next-open fills, 0.25% slippage, live fees',
                    full=b['full'],h1=b['h1'],h2=b['h2'],trades=b['trades'],years=b['years'],hold_cagr=fh,
                    verdict='pass' if r['passed'] else 'mixed' if 'beats_hold' not in r['failed'] else 'fail',
                    note=('Failed: '+', '.join(FAILS[f] for f in r['failed']) if r['failed'] else 'Passed every check')+(f". {u['reason']}" if u.get('reason') else ''))
        m=Fr.get('magic_faithful')
        if m:notes.append(f"Fundamentals: NSE annual results FY2018-FY2026 (scripts/fundamentals_history.py), from period end + 60 days. Greenblatt's formula with real debt and cash exists only from June 2023: {m['result']['cagr']}% a year vs {m['hold']['cagr']}% for the hold, {m['result']['maxdd']}% worst fall, {m['trades']} trades. Too short to judge.")
    out=dict(generated=time.strftime('%Y-%m-%d'),benchmarks=dict(ew_cagr=R['bench']['ew']['full']['cagr'],nifty500_cagr=R['bench']['nifty500']['full']['cagr'],ew_years=years(ew)),
        method=['Prices: Upstox adjusted daily candles from January 2017 for all ~2,300 listed NSE stocks; 5-minute candles from 2022 for EMA.',
            'Universe: each month, the 500 most traded stocks as of the prior month-end, so stocks that later fell out of the Nifty 500 are included. Delisted stocks are still missing.',
            'Hold: an equal-weight daily hold of that same universe, without costs. It is the bar every basket has to clear.',
            'Fills: signals from completed closes, fills at the next open with 0.25% adverse slippage, stops at the stop or the gapped open, live delivery charges and the 1.5% round-trip fee cap.',
            'Fundamentals: each annual report counts only from 60 days (quant) or 75 days (Investor) after its period end. Finance costs, borrowings and leases exist only for FY2026.',
            'Uniqueness for new baskets: excess-return correlation at most 0.65 and at most 50% shared holdings against every active rule basket and every basket already added.',
            *notes,'No dividends, no taxes. A backtest is evidence about the past, not a forecast.'],
        baskets=baskets,candidates=sorted(candidates,key=lambda c:-c['cagr']))
    (OUT/'basket-backtests.json').write_text(json.dumps(out,indent=1,default=float));print('saved',OUT/'basket-backtests.json')

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('command',choices=('trader','ema','news','quant','investor','summary'));cmd=a.parse_args().command
    globals()[cmd]()
