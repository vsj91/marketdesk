import math,random,tempfile,unittest
from datetime import date,timedelta
from pathlib import Path
from marketdesk.quant import calc,strategies,analytics
from marketdesk.quant.engine import BasketEngine
from marketdesk.quant.execution import ExecutionSimulator,drift_orders

SECTORS=['IT','Bank','Pharma','Auto','FMCG']

def days(n,start=date(2025,1,1)):
    out=[];d=start
    while len(out)<n:
        if d.weekday()<5:out.append(d.isoformat())
        d+=timedelta(days=1)
    return out

def walk(seed,n,drift=.0005,vol=.015,start=100.,volume=1e6):
    rng=random.Random(seed);px=start;out=[]
    for d in days(n):
        o=px;px=max(1.,px*math.exp(drift+vol*rng.gauss(0,1)))
        out.append(dict(day=d,open=o,high=max(o,px)*1.005,low=min(o,px)*.995,close=px,volume=volume*(1+.2*rng.random())))
    return out

def market(n=320,symbols=40):
    bars={f'S{i:02d}':walk(i,n,drift=.0002+.00005*(i%7),vol=.008+.002*(i%9)) for i in range(symbols)}
    bench=[b['close'] for b in walk(999,n,drift=.0004,vol=.009)]
    funds={s:dict(ebit=100+5*i,ev=1000+40*(i%11),market_cap=900,equity=500,net_profit=90+3*(i%10),ocf=100,total_debt=100+20*(i%5),
        dividend_yield=None,financial=False) for i,s in enumerate(bars)}
    return dict(day=bars['S00'][-1]['day'],bars=bars,fundamentals=funds,sectors={s:SECTORS[i%5] for i,s in enumerate(bars)},
        size={s:('Large cap' if i<20 else 'Mid cap') for i,s in enumerate(bars)},benchmark=bench)

def at(snap,k):
    """Snapshot truncated to the first k sessions."""
    bars={s:r[:k] for s,r in snap['bars'].items()}
    return dict(snap,day=bars['S00'][-1]['day'],bars=bars,benchmark=snap['benchmark'][:k])

class CalcTests(unittest.TestCase):
    def test_zscores_are_standardized_winsorized_and_skip_missing(self):
        z=calc.zscores({'a':1,'b':2,'c':3,'d':None,'e':float('nan')})
        self.assertEqual(set(z),{'a','b','c'});self.assertAlmostEqual(sum(z.values()),0)
        self.assertEqual(calc.zscores({str(i):(1000 if i==0 else 0) for i in range(30)})['0'],3.)
    def test_inverse_vol_weights(self):
        w=calc.inverse_vol_weights({'a':.1,'b':.2,'c':.4,'d':0})
        self.assertAlmostEqual(sum(w.values()),1);self.assertNotIn('d',w)
        self.assertAlmostEqual(w['a']/w['b'],2);self.assertAlmostEqual(w['a'],(1/.1)/(1/.1+1/.2+1/.4))
    def test_atr_and_atr_position_size(self):
        bars=[dict(high=11,low=9,close=10)]*20
        self.assertAlmostEqual(calc.atr(bars),2)
        self.assertEqual(calc.atr_shares(1_000_000,2),2500)    # 1% of 10L / (2 x 2)
        self.assertEqual(calc.atr_shares(1_000_000,0),0)
    def test_momentum_skips_latest_month(self):
        closes=[100.]*232+[150.]*20+[150.]+[120.]*20
        self.assertAlmostEqual(calc.momentum_12_1(closes),150/100-1)
        # The latest month does not move the score: same 12-1 path, different final month.
        self.assertAlmostEqual(calc.momentum_12_1(closes[:-21]+[60.]*21),calc.momentum_12_1(closes[:-21]+[300.]*21))
    def test_rolling_z(self):
        self.assertLess(calc.rolling_z([100.]*19+[90.]+[100.]*0),-3)
        self.assertIsNone(calc.rolling_z([100.]*20))
    def test_regime_states(self):
        up=[100*1.001**i for i in range(260)];down=[100*.999**i for i in range(260)]
        self.assertEqual(calc.regime(down)['regime'],'bear')
        calm=list(up);calm[-30:-10]=[calm[-31]*(1+.03*(-1)**i) for i in range(20)]   # vol spike that has since faded
        self.assertEqual(calc.regime(calm)['regime'],'bull')
        shock=up[:-5]+[up[-6]*(1+.04*(-1)**i) for i in range(5)]
        self.assertEqual(calc.regime(shock)['regime'],'choppy')
        self.assertIsNone(calc.regime(up[:100]))
    def test_regime_switch_needs_confirmation(self):
        up=[100*1.001**i for i in range(260)]
        dip=up[:-1]+[up[-1]*.8]                                  # one close below the 200-DMA
        self.assertEqual(calc.confirmed_regime(dip,'bull',3)['regime'],'bull')
        self.assertEqual(calc.confirmed_regime(dip,'bull',3)['raw'],'bear')
        self.assertEqual(calc.confirmed_regime(up[:-3]+[up[-1]*.8]*3,'bull',3)['regime'],'bear')
        self.assertEqual(calc.confirmed_regime(dip,None,3)['regime'],'bear')
    def test_sector_cap_pick_and_weight_cap(self):
        ranked=[f'x{i}' for i in range(20)];sectors={k:('A' if i<10 else 'B') for i,k in enumerate(ranked)}
        picks=calc.pick_with_sector_cap(ranked,sectors,10,.30)
        self.assertEqual(sum(sectors[p]=='A' for p in picks),3);self.assertEqual(len(picks),6)
        w=calc.cap_weights({'a':.5,'b':.3,'c':.1,'d':.1},{'a':'X','b':'X','c':'Y','d':'Z'},.5)
        self.assertAlmostEqual(w['a']+w['b'],.5);self.assertAlmostEqual(sum(w.values()),1)

class ExecutionTests(unittest.TestCase):
    bar=dict(open=100,high=110,low=90,close=104,volume=1_000_000)
    def test_typical_price_and_liquidity_tiers(self):
        sim=ExecutionSimulator(impact_coefficient=0)
        buy=sim.fill('BUY',100,self.bar,'Large cap');sell=sim.fill('SELL',100,self.bar,'Small cap')
        self.assertAlmostEqual(buy['reference_price'],(110+90+104)/3,4)
        self.assertAlmostEqual(buy['price'],buy['reference_price']*1.0005,3)
        self.assertAlmostEqual(sell['price'],sell['reference_price']*(1-.0025),3)
        self.assertGreater(buy['fee'],20);self.assertLess(buy['cash_delta'],-buy['value'])
    def test_impact_grows_with_participation_and_volume_caps_fill(self):
        sim=ExecutionSimulator()
        small=sim.fill('BUY',100,self.bar,'Large cap',adv=1e6,daily_vol=.02);big=sim.fill('BUY',90_000,self.bar,'Large cap',adv=1e6,daily_vol=.02)
        self.assertGreater(big['impact_bps'],small['impact_bps'])
        capped=sim.fill('SELL',500_000,self.bar,'Mid cap');self.assertEqual(capped['quantity'],100_000);self.assertTrue(capped['partial'])
        self.assertIsNone(sim.fill('BUY',10,dict(self.bar,volume=0)))
    def test_drift_band(self):
        orders=drift_orders({'a':dict(weight=.11),'b':dict(weight=.20),'c':dict(weight=.05),'d':dict(keep=True)},
            {'a':100,'b':100,'d':5},{'a':100.,'b':100.,'c':100.,'d':100.},100_000)
        kinds={s:k for s,_,_,_,k in orders}
        self.assertNotIn('a',kinds)                  # 10% -> 11%: inside the 2% band
        self.assertEqual(kinds['b'],'raise');self.assertEqual(kinds['c'],'add');self.assertNotIn('d',kinds)

class AnalyticsTests(unittest.TestCase):
    def test_drawdown_ulcer_and_ratios(self):
        p=analytics.performance([100,110,99,121],[100,101,102,103],risk_free=0)
        self.assertAlmostEqual(p['max_drawdown_pct'],10.);self.assertAlmostEqual(p['cumulative_return_pct'],21.)
        self.assertAlmostEqual(p['excess_return_pct'],18.)
        self.assertAlmostEqual(p['ulcer_index'],math.sqrt(100/4),3)
        self.assertGreater(p['sharpe'],0);self.assertGreater(p['sortino'],p['sharpe'])
        self.assertIsNone(analytics.performance([100])['sharpe'])

class StrategyTests(unittest.TestCase):
    def test_multifactor_picks_top_n_inside_sector_cap(self):
        snap=market();plan=strategies.multifactor(snap,{},strategies.config('multifactor',dict(roe_min=.1)))
        picks=plan['targets'];self.assertTrue(0<len(picks)<=15)
        per=[sum(snap['sectors'][s]==sec for s in picks) for sec in SECTORS];self.assertLessEqual(max(per),4)
        self.assertTrue(all(abs(t['weight']-1/15)<1e-12 for t in picks.values()))
        self.assertIn('composite',next(iter(picks.values()))['score'])
    def test_verified_large_move_keeps_the_stock(self):
        snap=market();bars=snap['bars']['S03']
        for b in bars[-30:]:
            for k in ('open','high','low','close'):b[k]*=1.3
        self.assertNotIn('S03',strategies.features(dict(snap)))
        self.assertIn('S03',strategies.features(dict(snap,confirmed={'S03':{bars[-30]['day']}})))

    def test_multifactor_without_fundamentals_holds_cash(self):
        snap=dict(market(),fundamentals={})
        plan=strategies.multifactor(snap,{},strategies.config('multifactor'));self.assertEqual(plan['targets'],{});self.assertTrue(plan['meta']['note'])
    def test_risk_parity_weights_are_inverse_vol(self):
        snap=market();plan=strategies.risk_parity(snap,{},strategies.config('risk_parity',dict(sector_cap=1.)))
        t=plan['targets'];self.assertAlmostEqual(sum(x['weight'] for x in t.values()),1)
        a,b=sorted(t,key=lambda s:t[s]['score']['vol60'])[::len(t)-1]
        self.assertGreater(t[a]['weight'],t[b]['weight'])
        self.assertAlmostEqual(t[a]['weight']*t[a]['score']['vol60'],t[b]['weight']*t[b]['score']['vol60'],3)
    def test_stat_arb_entry_exit_and_stop(self):
        snap=market();cfg=strategies.config('stat_arb')
        rows=snap['bars']['S01'];base=[100*1.002**i for i in range(len(rows))]
        for r,c in zip(rows,base):r.update(close=c,open=c,high=c*1.01,low=c*.99)
        rows[-1].update(close=base[-2]*.95,low=base[-2]*.94)          # one-day 5% dislocation inside an uptrend
        plan=strategies.stat_arb(snap,{},cfg)
        self.assertIn('S01',plan['targets']);self.assertIn('Z-score reached',plan['targets']['S01']['reason'])
        rows[-1].update(close=base[-1]*1.01)
        plan=strategies.stat_arb({k:v for k,v in snap.items() if k!='_features'},{'S01':dict(qty=10)},cfg)
        self.assertIn('Reverted',plan['exits']['S01'])
        rows[-1].update(close=base[-2]*.80,low=base[-2]*.79)
        plan=strategies.stat_arb({k:v for k,v in snap.items() if k!='_features'},{'S01':dict(qty=10)},cfg)
        self.assertIn('Stop',plan['exits']['S01'])
    def test_trend_breakout_sizing_and_trailing_stop(self):
        snap=market();rows=snap['bars']['S02']
        for i,r in enumerate(rows):c=100+.2*i;r.update(close=c,open=c,high=c+1,low=c-1,volume=1e6)
        rows[-1].update(close=rows[-2]['close']+5,high=rows[-2]['close']+6,volume=3e6)
        plan=strategies.trend(snap,{},strategies.config('trend'),1_000_000)
        t=plan['targets']['S02'];atr=t['score']['atr14']
        self.assertEqual(t['shares'],min(math.floor(10_000/(2*atr)),math.floor(200_000/rows[-1]['close'])))
        high=rows[-1]['close']+10*atr
        plan=strategies.trend({k:v for k,v in snap.items() if k!='_features'},{'S02':dict(qty=100,high_close=high,meta={})},strategies.config('trend'),1_000_000)
        self.assertIn('Trailing stop',plan['exits']['S02'])
    def test_regime_allocations_follow_the_index(self):
        snap=market();snap['benchmark']=[100*.999**i for i in range(320)]
        plan=strategies.regime(snap,{},strategies.config('regime',dict(roe_min=.1)))
        self.assertEqual(plan['meta']['regime'],'bear');self.assertAlmostEqual(sum(t['weight'] for t in plan['targets'].values()),1)
        cash=strategies.regime({k:v for k,v in snap.items() if k!='_features'},{},strategies.config('regime',dict(bear_defensive='cash')))
        self.assertAlmostEqual(sum(t['weight'] for t in cash['targets'].values()),.4)

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.engine=BasketEngine(Path(self.temp.name)/'ledger.db');self.snap=market()
    def test_signal_today_fills_tomorrow_at_typical_price_and_reruns_are_noops(self):
        e=self.engine;k=300;e.create('rp','risk_parity',1_000_000,dict(sector_cap=1.),day='2000-01-01')
        first=e.run_basket('rp',at(self.snap,k))
        self.assertTrue(first['rebalanced']);self.assertEqual(first['fills'],0);self.assertGreater(first['orders_queued'],0)
        self.assertEqual(first['nav'],1_000_000)
        self.assertEqual(e.run_basket('rp',at(self.snap,k)),dict(skipped='already ran'))
        nxt=at(self.snap,k+1);second=e.run_basket('rp',nxt)
        self.assertEqual(second['fills'],first['orders_queued']);self.assertFalse(second['rebalanced'])
        p=e.payload('rp');h=p['holdings'][0];bar=nxt['bars'][h['ticker']][-1]
        self.assertGreater(h['entry_price'],calc.typical_price(bar)*.9999)          # buys pay up from the typical price
        self.assertLess(p['nav'],1_000_000);self.assertGreater(p['nav'],950_000)    # friction, not a free lunch
        self.assertGreater(p['performance']['total_charges'],0)
        self.assertTrue(any(r['action']=='FILL' for r in p['rebalance_log']))
        self.assertTrue(any(r['reason'].startswith('Added ') for r in p['rebalance_log']))
        for key in ('cumulative_return_pct','benchmark_return_pct','annualized_volatility_pct','sharpe','sortino','max_drawdown_pct','ulcer_index'):
            self.assertIn(key,p['performance'])
        self.assertAlmostEqual(sum(x['weight_pct'] for x in p['holdings'])+100*p['cash']/p['nav'],100,1)
    def test_every_strategy_runs_a_month_without_negative_cash(self):
        for name in strategies.STRATEGIES:self.engine.create(name,name,1_000_000,dict(roe_min=.1) if name in ('multifactor','regime') else None,day='2000-01-01')
        for k in range(290,320):self.engine.run_day(at(self.snap,k))
        for name in strategies.STRATEGIES:
            p=self.engine.payload(name)
            self.assertGreaterEqual(p['cash'],0,name);self.assertEqual(p['performance']['sessions'],30,name)
            self.assertTrue(p['paper_only'])
    def test_out_of_order_and_unknown_strategy_are_rejected(self):
        e=self.engine;e.create('mf','multifactor',day='2000-01-01')
        e.run_basket('mf',at(self.snap,310))
        self.assertIn('out of order',e.run_basket('mf',at(self.snap,300))['skipped'])
        with self.assertRaises(ValueError):e.create('x','martingale')
        self.assertFalse(e.create('mf','trend'))

def snapshot_of(market,day):
    from marketdesk.quant.data import snapshot
    return snapshot(market,day)

class RunnerTests(unittest.TestCase):
    def setUp(self):
        from marketdesk.storage import init
        from marketdesk.paper import init_paper
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'m.db';init(self.db);init_paper(self.db)
        snap=market();self.days=[b['day'] for b in snap['bars']['S00']]
        self.market=dict(bars=snap['bars'],days={s:[b['day'] for b in r] for s,r in snap['bars'].items()},sectors=snap['sectors'],size=snap['size'],
            caps={},cap_anchor={},fundamentals={},benchmark=list(zip(self.days,snap['benchmark'])))
    def clock(self,day):
        from datetime import datetime,timezone
        return datetime.fromisoformat(day+'T12:00:00+05:30').astimezone(timezone.utc)
    def runner(self):
        from marketdesk.quant.runner import QuantRunner
        r=QuantRunner(self.db);r.index.refresh_benchmark=lambda clock:None      # no index download in tests
        return r
    def test_forward_only_start_catch_up_and_idempotent(self):
        from unittest import mock
        r=self.runner()
        with mock.patch('marketdesk.quant.runner.load_market',return_value=self.market):
            # Today's session (index 300) is not complete yet: baskets start on the previous one.
            st=r.tick(self.clock(self.days[300]))
            self.assertEqual(st['state'],'ok');self.assertEqual(st['last_session'],self.days[299])
            snap=r.snapshot();self.assertEqual(len(snap['baskets']),4)
            self.assertTrue(all(b['performance']['sessions']==1 for b in snap['baskets']))   # no backfill
            st=r.tick(self.clock(self.days[305]))                                              # app was closed for days
            self.assertEqual(st['last_session'],self.days[304])
            self.assertTrue(all(b['performance']['sessions']==6 for b in r.snapshot()['baskets']))
            self.assertIn('Up to date',r.tick(self.clock(self.days[305]))['message'])
    def test_retired_trend_basket_exits_and_stops_trading(self):
        from unittest import mock
        r=self.runner();e=r.engine
        # Retirement is by basket name; risk parity is used because it reliably holds stocks on synthetic data.
        e.create('trend','risk_parity',1_000_000,dict(sector_cap=1.),day=self.days[290])
        with mock.patch('marketdesk.quant.runner.RETIRED',{}),mock.patch('marketdesk.quant.runner.load_market',return_value=self.market):
            for k in range(291,300):e.run_day(snapshot_of(self.market,self.days[k]))
        held=len(e.payload('trend')['holdings'])
        with mock.patch('marketdesk.quant.runner.load_market',return_value=self.market):
            r.tick(self.clock(self.days[301]));r.tick(self.clock(self.days[303]))
        p=e.payload('trend');log=[x['reason'] for x in p['rebalance_log']]
        self.assertTrue(p['retired']);self.assertTrue(any(x.startswith('Basket retired') for x in log))
        self.assertEqual(p['holdings'],[]);self.assertEqual(p['pending_orders'],[])
        self.assertFalse(any(x['action']=='ADD' and x['day']>p['retired_day'] for x in p['rebalance_log']))
        self.assertGreater(held,0)
        self.assertEqual(sum(x.startswith('Exit ') and x.endswith(': basket retired') for x in log),held)
        self.assertNotIn('trend',[n for n in e.names() if not e.payload(n)['retired']])
        self.assertFalse(e.retire('trend',self.days[303],'again'))
    def test_fresh_ledger_does_not_create_retired_baskets(self):
        from unittest import mock
        r=self.runner()
        with mock.patch('marketdesk.quant.runner.load_market',return_value=self.market):r.tick(self.clock(self.days[300]))
        self.assertNotIn('trend',r.engine.names());self.assertEqual(len(r.engine.names()),4)
    def test_waits_for_running_candle_refresh_and_reports_errors(self):
        from unittest import mock
        from marketdesk.storage import connect,set_state
        r=self.runner()
        with connect(self.db) as db:set_state(db,'refresh',{'status':'running','mode':'candles'})
        self.assertEqual(r.tick(self.clock(self.days[300]))['state'],'waiting');self.assertEqual(r.engine.names(),[])
        # A refresh cut short by a restart is resumed by maintenance; its day is still half refreshed.
        with connect(self.db) as db:set_state(db,'refresh',{'status':'interrupted','mode':'candles'})
        self.assertEqual(r.tick(self.clock(self.days[300]))['state'],'waiting')
        with connect(self.db) as db:set_state(db,'refresh',{'status':'done','mode':'candles'})
        with mock.patch('marketdesk.quant.runner.load_market',side_effect=RuntimeError('boom')),mock.patch('sys.stderr'):
            st=r.tick(self.clock(self.days[300]))
        self.assertEqual(st['state'],'error');self.assertIn('RuntimeError',st['message'])
    def test_remark_heals_a_mid_refresh_mark_only_while_the_book_is_unchanged(self):
        from marketdesk.storage import connect
        e=self.runner().engine;e.create('rp','risk_parity',1_000_000,dict(sector_cap=1.),day=self.days[290])
        for k in range(291,300):e.run_day(snapshot_of(self.market,self.days[k]))
        last=snapshot_of(self.market,self.days[299]);good=e.payload('rp');held=good['holdings'][0]['ticker']
        self.assertEqual(e.remark(last),[])      # already complete: nothing to change
        def stale():
            with connect(e.path) as db:
                db.execute('UPDATE qb_nav SET nav=nav-1000,benchmark=NULL WHERE basket=? AND day=?',('rp',self.days[299]))
                db.execute('UPDATE qb_weights SET close=1 WHERE basket=? AND day=? AND symbol=?',('rp',self.days[299],held))
        stale();missing=dict(last,bars={s:b for s,b in last['bars'].items() if s!=held})
        self.assertEqual(e.remark(missing),[])   # the held close is still missing: keep the record
        self.assertEqual(e.remark(last),['rp']);healed=e.payload('rp')
        self.assertEqual(healed['nav'],good['nav']);self.assertEqual(healed['holdings'],good['holdings'])
        with connect(e.path) as db:self.assertIsNotNone(db.execute('SELECT benchmark FROM qb_nav WHERE basket=? AND day=?',('rp',self.days[299])).fetchone()[0])
        stale()
        with connect(e.path) as db:db.execute('UPDATE qb_baskets SET cash=cash-1 WHERE name=?',('rp',))
        self.assertEqual(e.remark(last),[])      # a later fill changed the book: the day's record stays

class DisasterStopTests(unittest.TestCase):
    def test_stop_exits_only_at_25_percent_below_cost_on_the_current_session(self):
        from marketdesk.quant.strategies import stop_exits,config
        snap=dict(day='2026-10-21',bars={'A':[dict(day='2026-10-21',close=75.)],'B':[dict(day='2026-10-21',close=76.)],'C':[dict(day='2026-10-20',close=10.)]})
        book={s:dict(qty=10,entry_price=100.) for s in 'ABC'}
        self.assertEqual(list(stop_exits(book,snap,config('multifactor'))),['A'])
        self.assertEqual(stop_exits(book,snap,config('stat_arb')),{})              # stat-arb keeps its own Z-score stop
        for name in ('multifactor','risk_parity','regime'):self.assertEqual(config(name)['stop_pct'],.25)

if __name__=='__main__':unittest.main()
