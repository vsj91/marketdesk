#!/usr/bin/env python3
"""Independent backtests for the EMA + Price Action Trader and the AI News Momentum Trader.

Uses the same rule functions as the live engine (signal_baskets.ema_rules / news / news_rules); paper only.

EMA + Price Action: real Upstox 5-minute candles (cached under data/backtests/ema5), completed candles only.
  Entry: stop-limit after the trigger candle; fills on a later candle that trades at least one tick through the
  trigger, at max(open, trigger) + slippage, only if that is within the limit; gap above max_gap cancels.
  Exits: ema_rules.simulate_bar (gap through stop fills at the open, stop assumed before target in the same
  candle, upgraded stops count as hit when the candle low reaches them); EMA20 trail on completed candles;
  square-off on the 15:10 candle at the worse of its open and close. Intraday charges + slippage on every fill.
  Walk-forward: min_rr is chosen on the first half (in-sample) by expectancy and then applied unchanged to the
  second half (out-of-sample).

AI News Momentum: real NSE corporate announcements with exchange broadcast timestamps (cached under
  data/backtests/news) and stored daily candles. Screen at publication, confirm on the completed news-session
  candle, buy the next session's open, exit on daily candles (stop first when a candle touches both).
  Revenue for materiality comes from today's stored annual record (FY2026, filed before the window); the
  surprise component is unavailable without historical live prices. Defaults only; nothing is fitted.

AI News Catalyst Investor is not backtested: its 3-24 month horizon exceeds the available news history.

Usage: .venv/bin/python scripts/backtest_signal_baskets.py [--symbols 40] [--start 2026-09-01] [--end 2026-10-16]
       [--news-start 2026-07-01] [--news-end 2026-09-30]"""
import argparse,collections,json,math,re,sqlite3,statistics,sys,time
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT))
from marketdesk.execution import charges,intraday_charges
from marketdesk.signal_baskets import ema_rules,news as newslib,news_rules
from marketdesk.signal_baskets.indicators import ema_series,round_tick
from marketdesk.signal_baskets.runner import UpstoxCandles,valid_candles,news_session

IST=timezone(timedelta(hours=5,minutes=30));OUT=ROOT/'data/backtests'

def ro(path):db=sqlite3.connect(f'file:{path}?mode=ro',uri=True);db.row_factory=sqlite3.Row;return db

def metrics(closed,daily_equity,capital,fees,utilization=None):
    wins=[t['pnl'] for t in closed if t['pnl']>0];losses=[t['pnl'] for t in closed if t['pnl']<=0]
    series=[capital]+[e for _,e in sorted(daily_equity.items())];rets=[b/a-1 for a,b in zip(series,series[1:]) if a>0]
    peak=capital;mdd=0.
    for e in series:peak=max(peak,e);mdd=max(mdd,1-e/peak)
    return dict(trades=len(closed),win_rate_pct=round(len(wins)/len(closed)*100,1) if closed else None,
        average_win=round(statistics.mean(wins),2) if wins else None,average_loss=round(statistics.mean(losses),2) if losses else None,
        profit_factor=round(sum(wins)/-sum(losses),2) if losses and sum(losses)<0 else None,expectancy=round(sum(t['pnl'] for t in closed)/len(closed),2) if closed else None,
        net_pnl=round(series[-1]-capital,2),net_return_pct=round((series[-1]/capital-1)*100,3),max_drawdown_pct=round(mdd*100,2),
        sharpe=round(statistics.mean(rets)/statistics.stdev(rets)*math.sqrt(252),2) if len(rets)>=30 and statistics.stdev(rets)>0 else None,
        sharpe_note=None if len(rets)>=30 else f'{len(rets)} daily returns: too few for a meaningful Sharpe ratio',
        average_holding=round(statistics.mean(t['held'] for t in closed),2) if closed else None,costs=round(fees,2),capital_utilization_pct=utilization)

# ---- EMA + price action --------------------------------------------------------------------------

def ema_data(db,symbols,start,end,tf=5):
    reader=UpstoxCandles();folder=OUT/'ema5';folder.mkdir(parents=True,exist_ok=True);data={}
    keys=dict(db.execute('SELECT symbol,instrument_key FROM instruments'));clock=datetime.now(timezone.utc)
    for s in symbols:
        f=folder/f'{s}_{start}_{end}.json'
        if f.exists():data[s]=json.loads(f.read_text());continue
        rows=[];a=date.fromisoformat(start)
        while a<=date.fromisoformat(end):
            b=min(a+timedelta(days=27),date.fromisoformat(end))
            try:rows+=valid_candles(reader.history(keys[s],tf,a.isoformat(),b.isoformat()),tf,clock)
            except Exception as e:print('skip',s,e,file=sys.stderr);rows=None;break
            a=b+timedelta(days=1)
        if rows is None:continue
        bars=[dict(ts=r[0],open=r[1],high=r[2],low=r[3],close=r[4],volume=r[5]) for r in sorted(set(rows))]
        f.write_text(json.dumps(bars));data[s]=bars
    return data

def ema_backtest(data,cfg,capital=100_000,start=None,end=None):
    """Chronological multi-symbol simulation with the live portfolio limits. Trades only on days in [start, end]."""
    p=ema_rules.merged(cfg);tf=p['timeframe_minutes'];slip=p['slippage_bps']/1e4;tick=p['tick_size']
    timeline=collections.defaultdict(list)
    for s,bars in data.items():
        for i,b in enumerate(bars):timeline[b['ts']].append((s,i))
    cash=capital;positions={};pending={};closed=[];fees=0.;daily={};util=[];cross={}
    for s,bars in data.items():
        c=[b['close'] for b in bars];f=ema_series(c,p['ema_fast']);sl=ema_series(c,p['ema_slow'])
        cross[s]={i for i in range(1,len(bars)) if None not in (f[i-1],sl[i-1],f[i],sl[i]) and f[i-1]<=sl[i-1] and f[i]>sl[i]}
    def equity(marks):return cash+sum(st['qty']*marks.get(s,st['entry']) for s,st in positions.items())
    marks={};entries=collections.Counter();signals=collections.Counter()
    for ts in sorted(timeline):
        day=ts[:10];hm=ts[11:16];active=(start is None or day>=start) and (end is None or day<=end)
        for s,i in timeline[ts]:
            bars=data[s];bar=bars[i];marks[s]=bar['close']
            if s in positions:
                st=positions[s]
                if hm>='15:10' or st['day']!=day:
                    px=min(bar['open'],bar['close'])*(1-slip) if st['day']==day else bar['open']*(1-slip)
                    exits=[('CLOCK_EXIT',st['qty'],px)];st['qty']=0
                else:
                    exits,_=ema_rules.simulate_bar(st,bar,p,slip)
                    if st['qty']>0:ema_rules.on_bar(st,bars[max(0,i-60):i+1],p)
                for kind,q,px in exits:
                    fee=intraday_charges(q*px,'SELL')['total'];fees+=fee;cash+=q*px-fee;st['pnl']+=q*(px-st['entry'])-fee
                if st['qty']<=0:
                    closed.append(dict(symbol=s,pnl=round(st['pnl'],2),held=st['bars'],kind=exits[-1][0] if exits else '?',day=day));del positions[s]
                else:st['bars']+=1
            if s in pending:
                o=pending[s];o['age']+=1
                if o['age']>p['signal_expiry_candles'] or hm>=p['entry_cutoff'] or day!=o['day']:del pending[s]
                elif bar['open']>o['trigger']*(1+p['max_gap_pct']/100):del pending[s]
                elif bar['high']>=o['trigger']+tick and bar['volume']>0:
                    px=max(bar['open'],o['trigger'])*(1+slip)
                    if px<=o['limit'] and len(positions)<p['max_positions'] and entries[day]<p['max_entries_per_day']:
                        eq=equity(marks);heat=sum(max(0,st['entry']-st['stop'])*st['qty'] for st in positions.values())
                        sz=ema_rules.size(eq,cash,px,o['signal']['initial_stop'],p,heat,lambda v:intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total'])
                        if sz['quantity']>0:
                            q=sz['quantity'];fee=intraday_charges(q*px,'BUY')['total'];fees+=fee;cash-=q*px+fee
                            st=ema_rules.arm(o['signal'],px,q,p,(intraday_charges(q*px,'BUY')['total']+intraday_charges(q*px,'SELL')['total'])/q)
                            st.update(day=day,pnl=-fee,bars=0);positions[s]=st;entries[day]+=1;del pending[s]
                            exits,_=([('STOP_LOSS',q,st['stop']*(1-slip))],None) if bar['low']<=st['stop'] else ([],None)   # adverse: stopped in the fill candle
                            for kind,qq,px2 in exits:
                                fee=intraday_charges(qq*px2,'SELL')['total'];fees+=fee;cash+=qq*px2-fee;st['pnl']+=qq*(px2-st['entry'])-fee
                                closed.append(dict(symbol=s,pnl=round(st['pnl'],2),held=0,kind=kind,day=day));del positions[s]
                elif ema_rules.still_valid(bars[max(0,i-400):i+1],p):del pending[s]
            if active and i in cross[s] and s not in positions and s not in pending and p['entry_start']<=hm<p['entry_cutoff']:
                ev=ema_rules.evaluate(bars[max(0,i-400):i+1],p)
                if ev:
                    signals[ev['status']]+=1
                    if ev['status']=='SIGNAL_DETECTED':pending[s]=dict(trigger=ev['entry_trigger'],limit=ev['limit_price'],signal=ev,age=0,day=day)
                    else:
                        for r in ev['reasons']:signals['why: '+re.sub(r'[-+]?[0-9][0-9.,]*','#',r)]+=1
        if ts[11:16]=='15:25':
            daily[day]=equity(marks);util.append(0.)
    return dict(metrics=metrics(closed,daily,capital,fees),signals=dict(signals),exits=dict(collections.Counter(t['kind'] for t in closed)),
        trades=closed[-200:])

# ---- News momentum -------------------------------------------------------------------------------

def news_rows(start,end):
    folder=OUT/'news';folder.mkdir(parents=True,exist_ok=True);provider=newslib.NseAnnouncements(min_interval=2.);rows=[]
    a=date.fromisoformat(start)
    while a<=date.fromisoformat(end):
        b=min((a.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1),date.fromisoformat(end))
        f=folder/f'{a}_{b}.json'
        if not f.exists():f.write_text(json.dumps(provider.fetch(a,b)))
        rows+=json.loads(f.read_text());a=b+timedelta(days=1)
    return rows

def news_backtest(db,rows,cfg,capital=100_000,split=None):
    p=news_rules.merged(news_rules.MOMENTUM_DEFAULTS,cfg);slip=p['slippage_bps']/1e4
    inst={r['symbol']:dict(r) for r in db.execute('SELECT symbol,isin,sector FROM instruments')}
    funds={r['symbol']:json.loads(r['details']).get('summary') or {} for r in db.execute('SELECT symbol,details FROM fundamentals')}
    candles=collections.defaultdict(list)
    for r in db.execute('SELECT symbol,ts,open,high,low,close,volume FROM candles ORDER BY symbol,ts'):candles[r['symbol']].append(dict(r))
    bench=[(r['day'],r['close']) for r in db.execute('SELECT day,close FROM claude_benchmark ORDER BY day')]
    def before(s,day):return [b for b in candles.get(s,[]) if b['ts'][:10]<day]
    items=[];seen={};prior=collections.defaultdict(list);stats=collections.Counter()
    for raw in sorted(rows,key=lambda r:r.get('sort_date') or ''):
        it=newslib.normalize(raw,datetime.now(timezone.utc))
        if not it:continue
        s=it['symbol']
        if s not in inst or inst[s]['isin']!=it['isin']:stats['unverified or outside universe']+=1;continue
        k=it['dedup_key']
        if k in seen and (datetime.fromisoformat(it['published_at'])-seen[k]).days<=3:stats['duplicate']+=1;continue
        seen[k]=datetime.fromisoformat(it['published_at'])
        cat,_,_=newslib.classify(it['subject'],it['headline'])
        if cat in ('routine','other'):stats['routine']+=1;continue
        pub=datetime.fromisoformat(it['published_at']);sess=news_session(it['published_at'])
        a=newslib.analyse(it,revenue=funds.get(s,{}).get('revenue'),prior=prior[s],bars=before(s,sess)[-30:],price=None,now=pub,verification='verified')
        prior[s].append(dict(category=a['category'],amount_crore=a['amount_crore']))
        status,reasons=news_rules.momentum_screen(a,p);stats['screen '+status]+=1
        if status=='WATCHLIST':items.append((sess,s,a,it))
    # Confirm on the completed news-session candle, enter on the next session's open.
    orders=collections.defaultdict(list);neg=collections.defaultdict(list)
    for r in rows:
        it=newslib.normalize(r,datetime.now(timezone.utc))
        if it and it['symbol'] in inst:
            c,d,flags=newslib.classify(it['subject'],it['headline'])
            if d<0:neg[it['symbol']].append(news_session(it['published_at']))
    for sess,s,a,it in items:
        bars=[b for b in candles.get(s,[]) if b['ts'][:10]>=sess]
        if len(bars)<2:stats['no confirmation candle']+=1;continue
        conf=bars[0];hist=before(s,conf['ts'][:10])+[conf]
        ctx=news_rules.daily_context(hist,[c for d,c in bench if d<=conf['ts'][:10]])
        status,reasons,plan=news_rules.momentum_confirm(a,ctx,hist,p)
        stats['confirm '+status]+=1
        for r in reasons:stats['why: '+re.sub(r'[-+]?[0-9][0-9.,]*','#',r)[:70]]+=1
        if status=='BUY_CANDIDATE':orders[bars[1]['ts'][:10]].append(dict(symbol=s,plan=plan,score=a['impact_score'],headline=it['headline'][:120],sector=inst[s]['sector']))
    days=sorted({b['ts'][:10] for s in candles for b in candles[s][-200:]})
    cash=capital;pos={};closed=[];fees=0.;daily={};util=[]
    index={s:{b['ts'][:10]:i for i,b in enumerate(v)} for s,v in candles.items()}
    for day in days:
        # exits first, on this day's candle (stop before target when both are touched)
        for s in list(pos):
            st=pos[s];i=index[s].get(day)
            if i is None:continue
            b=candles[s][i];px=None;kind=None
            if st.get('exit_next'):px,kind=b['open']*(1-slip),st['exit_next']
            elif b['open']<=st['stop']:px,kind=b['open']*(1-slip),'STOP_LOSS (gap)'
            elif b['low']<=st['stop']:px,kind=st['stop']*(1-slip),'STOP_LOSS'
            elif b['high']>=st['target']:px,kind=st['target'],'TARGET'
            if px is None:
                st['held']+=1;ctx=news_rules.daily_context(candles[s][:i+1])
                best=max(st['best_close'],b['close']);st['best_close']=best;st['stop']=max(st['stop'],best-p['trail_atr_mult']*st['atr'])
                if ctx and b['close']<ctx['ema20']:st['exit_next']='TREND_FAILURE'
                elif any(st['entry_session']<d<=day for d in neg[s]):st['exit_next']='NEWS_INVALIDATION'
                elif st['held']>=p['max_hold_sessions']:st['exit_next']='TIME_EXIT'
                elif st['held']>=p['follow_through_sessions'] and best<st['entry']+(st['entry']-st['initial_stop']):st['exit_next']='NO_FOLLOW_THROUGH'
                continue
            fee=charges(st['qty']*px,'SELL',True)['total'];fees+=fee;cash+=st['qty']*px-fee
            closed.append(dict(symbol=s,pnl=round(st['qty']*px-fee-st['cost'],2),held=st['held'],kind=kind,day=day,entry_day=st['entry_session']));del pos[s]
        for o in sorted(orders.get(day,[]),key=lambda x:-x['score'])[:p['max_entries_per_day']]:
            s=o['symbol'];i=index[s].get(day)
            if i is None or s in pos or len(pos)>=p['max_positions'] or sum(1 for v in pos.values() if v['sector']==o['sector'])>=p['max_per_sector']:continue
            b=candles[s][i];plan=o['plan']
            if abs(b['open']/plan['reference']-1)*100>p['max_gap_pct']:continue
            px=b['open']*(1+slip);stop=px-p['stop_atr_mult']*plan['atr'];target=px+p['target_atr_mult']*plan['atr']
            if plan.get('resistance') and plan['resistance']<target:target=plan['resistance']
            if (target-px)/(px-stop)<p['min_rr']:continue
            eq=cash+sum(v['qty']*v['entry'] for v in pos.values())
            q=min(math.floor(eq*p['risk_pct']/100/(px-stop)),math.floor(min(cash,eq*p['max_position_pct']/100)/(px*1.003)))
            if q<1:continue
            fee=charges(q*px,'BUY')['total'];fees+=fee;cash-=q*px+fee
            pos[s]=dict(entry=px,qty=q,cost=q*px+fee,stop=stop,initial_stop=stop,target=target,atr=plan['atr'],best_close=px,held=0,entry_session=day,sector=o['sector'])
            if b['low']<=stop:   # adverse assumption: a stop touched on the entry day is hit after the open fill
                px2=stop*(1-slip);fee=charges(q*px2,'SELL',True)['total'];fees+=fee;cash+=q*px2-fee
                closed.append(dict(symbol=s,pnl=round(q*px2-fee-pos[s]['cost'],2),held=0,kind='STOP_LOSS',day=day,entry_day=day));del pos[s]
        marks=sum(v['qty']*candles[s][index[s][day]]['close'] if day in index[s] else v['qty']*v['entry'] for s,v in pos.items())
        if day>=min(orders,default=day):daily[day]=cash+marks;util.append(marks/(cash+marks) if cash+marks else 0)
    result=dict(metrics=metrics(closed,daily,capital,fees,round(statistics.mean(util)*100,1) if util else None),funnel=dict(stats),
        exits=dict(collections.Counter(t['kind'] for t in closed)),trades=closed)
    if split:
        result['in_sample']=dict(period=f'< {split}',trades=len([t for t in closed if t['entry_day']<split]),pnl=round(sum(t['pnl'] for t in closed if t['entry_day']<split),2))
        result['out_of_sample']=dict(period=f'>= {split}',trades=len([t for t in closed if t['entry_day']>=split]),pnl=round(sum(t['pnl'] for t in closed if t['entry_day']>=split),2))
    b=[c for d,c in bench if d>=min(daily,default='9') and d<=max(daily,default='0')]
    result['benchmark_nifty500_return_pct']=round((b[-1]/b[0]-1)*100,3) if len(b)>1 else None
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--db',default=str(ROOT/'data/marketdesk.sqlite3'));ap.add_argument('--symbols',type=int,default=40)
    ap.add_argument('--start',default='2026-09-01');ap.add_argument('--end',default='2026-10-16')
    ap.add_argument('--news-start',default='2026-07-01');ap.add_argument('--news-end',default='2026-09-30')
    ap.add_argument('--skip-ema',action='store_true');ap.add_argument('--skip-news',action='store_true')
    a=ap.parse_args();db=ro(a.db);report=dict(created_at=datetime.now(timezone.utc).isoformat(),paper_only=True)
    if not a.skip_ema:
        members=set(json.loads((db.execute("SELECT value FROM state WHERE key='nifty500_membership'").fetchone() or ['{}'])[0]).get('members',{}))
        ranked=[r[0] for r in db.execute('''SELECT symbol FROM candles WHERE substr(ts,1,10)<? AND substr(ts,1,10)>=? GROUP BY symbol HAVING MIN(close)>=50 ORDER BY AVG(close*volume) DESC''',
            (a.start,(date.fromisoformat(a.start)-timedelta(days=35)).isoformat())) if not members or r[0] in members][:a.symbols]
        data=ema_data(db,ranked,a.start,a.end)
        days=sorted({b['ts'][:10] for v in data.values() for b in v});mid=days[len(days)//2]
        warm=days[3] if len(days)>3 else days[0]
        variants={}
        for rr in (1.5,1.75,2.0):variants[str(rr)]=ema_backtest(data,{'min_rr':rr},start=warm,end=(date.fromisoformat(mid)-timedelta(days=1)).isoformat())
        best=max(variants,key=lambda k:(variants[k]['metrics']['expectancy'] if variants[k]['metrics']['expectancy'] is not None else -1e9))
        report['ema']=dict(universe=ranked,sessions=[warm,days[-1]],split=mid,
            spec_defaults_full=ema_backtest(data,{},start=warm),
            in_sample={k:dict(metrics=v['metrics'],signals=v['signals'],exits=v['exits']) for k,v in variants.items()},chosen_min_rr=float(best),
            out_of_sample=ema_backtest(data,{'min_rr':float(best)},start=mid),
            full_min_rr_1_5=ema_backtest(data,{'min_rr':1.5},start=warm))
        for k in ('spec_defaults_full','out_of_sample','full_min_rr_1_5'):report['ema'][k]['trades']=report['ema'][k]['trades'][-60:]
    if not a.skip_news:
        rows=news_rows(a.news_start,a.news_end)
        split=(date.fromisoformat(a.news_start)+(date.fromisoformat(a.news_end)-date.fromisoformat(a.news_start))*2/3).isoformat()
        report['news_momentum']=dict(window=[a.news_start,a.news_end],announcements=len(rows),**news_backtest(db,rows,{},split=split))
    report['news_catalyst']=dict(status='not backtested',reason='The 3-24 month holding period exceeds the announcement history used here; a result would not be meaningful.')
    out=OUT/'signal-baskets-latest.json';out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,indent=1,default=str))
    def brief(m):return {k:m[k] for k in ('trades','win_rate_pct','profit_factor','expectancy','net_return_pct','max_drawdown_pct','costs')}
    if 'ema' in report:
        e=report['ema'];print('EMA spec defaults:',brief(e['spec_defaults_full']['metrics']),e['spec_defaults_full']['signals'])
        for k,v in e['in_sample'].items():print(f'EMA in-sample min_rr={k}:',brief(v['metrics']))
        print('EMA chosen',e['chosen_min_rr'],'out-of-sample:',brief(e['out_of_sample']['metrics']),e['out_of_sample']['exits'])
    if 'news_momentum' in report:
        n=report['news_momentum'];print('News momentum:',brief(n['metrics']),n['exits'],'benchmark',n['benchmark_nifty500_return_pct']);print(n['funnel'])
    print('saved',out)

if __name__=='__main__':main()
