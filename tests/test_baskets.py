import math
import tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.autotrading import AutoTrading,IST
from marketdesk.basket_rules import stats,ranking,exit_ranking,rebalance_exit,daily_exit,stop_exit,rsi,clenow_score
from marketdesk.paper import init_paper
from marketdesk.storage import init,connect,set_state,get_state
from support import seed_quality,fixture_book

def candles(growth,count=200,end=datetime(2026,10,9).date()):
    days=[];day=end
    while len(days)<count:
        if day.weekday()<5:days.append(day)
        day-=timedelta(days=1)
    return [dict(ts=d.isoformat()+'T00:00:00+05:30',open=100*(1+growth)**n,high=100*(1+growth)**n*1.01,low=100*(1+growth)**n*.99,close=100*(1+growth)**n,volume=200000) for n,d in enumerate(reversed(days))]

class BasketRuleTests(unittest.TestCase):
    def test_clenow_scores_steady_climbs_and_buys_only_in_a_rising_market(self):
        self.assertAlmostEqual(clenow_score([100*1.002**n for n in range(90)]),1.002**250-1)   # a straight line: R squared is 1
        self.assertIsNone(clenow_score([100.]*89))
        days=[r['ts'][:10] for r in candles(0,count=260)]
        rising={d:1000+i for i,d in enumerate(days)};falling={d:2000-i for i,d in enumerate(days)}
        def jumpy(growth):
            rows=candles(growth,count=260)
            for i,r in enumerate(rows):
                for k in ('open','high','low','close'):r[k]*=1.04 if i%2 else .96       # same trend, 8% swings
            return rows
        sig=lambda bench:{'STEADY':stats(candles(.002,count=260),bench=bench),'JUMPY':stats(jumpy(.002),bench=bench),'FALL':stats(candles(-.002,count=260),bench=bench)}
        up=sig(rising)
        self.assertGreater(up['STEADY']['clenow'],up['JUMPY']['clenow']);self.assertTrue(up['STEADY']['index_up'])
        self.assertEqual(ranking('clenow',up),['STEADY','JUMPY'])                 # falling stock excluded
        self.assertEqual(ranking('clenow',sig(falling)),[])                      # Nifty 500 below its 200-day average
        self.assertEqual(ranking('clenow',sig({d:v for d,v in rising.items() if d!=days[-1]})),[])   # index close missing: no buys
        gap=candles(.002,count=260);gap[-30]=dict(gap[-30],close=gap[-30]['close']*1.2)
        self.assertNotIn('GAP',ranking('clenow',{**up,'GAP':stats(gap,bench=rising)}))   # 15%+ one-day move in 90 sessions
        self.assertIsNone(stop_exit('clenow',100.,1.))                            # no per-stock stop, as published
        self.assertIsNone(rebalance_exit('clenow','STEADY',up['STEADY'],exit_ranking('clenow',up)))
        self.assertIn('100-day',rebalance_exit('clenow','STEADY',{**up['STEADY'],'close':up['STEADY']['dma100']*.99},['STEADY']))
        self.assertIn('top 100',rebalance_exit('clenow','STEADY',up['STEADY'],[f'X{i}' for i in range(100)]+['STEADY']))
        self.assertEqual(exit_ranking('clenow',sig(falling))[0],'STEADY')         # exits keep ranking in a falling market
    def test_rankings_follow_each_basket_definition(self):
        signals={'FAST':stats(candles(.004)),'SLOW':stats(candles(.001)),'FALL':stats(candles(-.002))}
        self.assertEqual(ranking('momentum',signals),['FAST','SLOW'])  # falling stock fails absolute momentum
        self.assertNotIn('FALL',ranking('lowvol',signals))               # below its 200-day average
        self.assertEqual(rsi([1,2,3,4]),100.)
        self.assertIsNone(stats(candles(.002,count=100)))                # too little history
        self.assertEqual(ranking('high52',signals),[])                    # 200 sessions: no 52-week high yet
        year={'TOP':stats(candles(.002,count=260)),'FALL':stats(candles(-.002,count=260))}
        dip=candles(.002,count=260);dip[-1]=dict(dip[-1],close=dip[-1]['close']*.97)
        year['DIP']=stats(dip)
        self.assertEqual(ranking('high52',year),['TOP','DIP'])           # nearest the high first; falling stock excluded
        self.assertEqual(ranking('momentum12',signals),[])                # 200 sessions: no 12-1 return yet
        strong={'FAST':stats(candles(.004,count=260)),'SLOW':stats(candles(.001,count=260)),'FALL':stats(candles(-.002,count=260))}
        self.assertEqual(ranking('momentum12',strong),['FAST','SLOW'])
        early=candles(.002,count=260)
        for r in early[20:]:
            for k in ('open','high','low','close'):r[k]*=1.3                    # unverified jump 8 months ago
        self.assertIsNone(stats(early)['momentum12'])                     # outside the 6-month check, inside the year
        self.assertIn('12-1',rebalance_exit('momentum12','A',{**strong['FAST'],'momentum12':-.1},['A']))
        self.assertIsNone(rebalance_exit('momentum12','FAST',strong['FAST'],['FAST']))

    def test_research_basket_rankings(self):
        gapped=candles(.002,count=200);last=gapped[-1];prior=gapped[-2]['close']
        gapped[-1]=dict(last,open=prior*1.05,high=prior*1.08,low=prior*1.04,close=prior*1.07,volume=200000*4)
        quiet=stats(candles(.002,count=200));g=stats(gapped)
        self.assertEqual(ranking('gap_drift',{'GAP':g,'QUIET':quiet}),['GAP'])
        self.assertEqual(ranking('gap_drift',{'GAP':dict(g,close=g['open']*.99)}),[])   # faded below its open
        self.assertIn('Held 60',daily_exit('gap_drift',g,60));self.assertIsNone(daily_exit('gap_drift',g,59))
        self.assertIsNone(daily_exit('momentum',g,500))
        night,day=candles(.002,count=200),candles(.002,count=200)
        for i in range(1,200):
            night[i]['open']=night[i-1]['close']*1.002;day[i]['open']=day[i-1]['close']*.999
        self.assertEqual(ranking('overnight',{'NIGHT':stats(night),'DAY':stats(day)}),['NIGHT'])
        fast,slow=stats(candles(.004)),stats(candles(.001))
        days=[r['ts'][:10] for r in candles(0,count=260)];index=[100.];bench={}
        for i in range(1,260):index.append(index[-1]*(1.004 if i%2 else .998))   # Nifty 500 with alternating moves
        bench=dict(zip(days,index))
        def follow(extra):                                                  # beta 1 to the index plus a daily edge
            rows=candles(0,count=260);c=100.
            for i,r in enumerate(rows):
                if i:c*=index[i]/index[i-1]*math.exp(extra)
                r.update(open=c,high=c*1.01,low=c*.99,close=c)
            return rows
        lead,lag=stats(follow(.002),bench=bench),stats(follow(-.001),bench=bench)
        self.assertAlmostEqual(lead['alpha'],.002,places=6);self.assertAlmostEqual(lag['alpha'],-.001,places=6)
        self.assertIsNone(stats(follow(.002))['alpha'])                    # no benchmark, no alpha
        self.assertEqual(ranking('alpha',{'LEAD':lead,'LAG':lag}),['LEAD'])
        self.assertIn('alpha',rebalance_exit('alpha','LEAD',dict(lead,alpha=-.001),['LEAD']))
        sigs={**{f'A{i}':dict(fast,sector='Alpha') for i in range(5)},**{f'B{i}':dict(slow,sector='Beta') for i in range(5)},
              **{f'C{i}':dict(slow,sector='Gamma') for i in range(4)},'D0':dict(fast,sector='Delta')}
        self.assertEqual(set(ranking('sector_rotation',sigs)),{f'A{i}' for i in range(5)}|{f'B{i}' for i in range(5)})  # sectors under 5 stocks skipped

    def test_only_unverified_large_moves_break_history(self):
        rows=candles(.002)
        for r in rows[170:]:
            for k in ('open','high','low','close'):r[k]*=1.3                   # a genuine 30% results-day jump
        self.assertIsNone(stats(rows))                                         # unverified: possible corporate action
        s=stats(rows,{rows[170]['ts'][:10]})                                   # NSE-verified ordinary move
        self.assertIsNotNone(s);self.assertEqual(ranking('momentum',{'JUMP':s}),['JUMP'])

    def test_exit_rules(self):
        s=stats(candles(.002))
        self.assertIsNone(rebalance_exit('momentum','A',s,['A']))
        self.assertIn('top 25',rebalance_exit('momentum','A',s,['X']*30+['A']))
        self.assertEqual(stop_exit('momentum',100,79),'20% loss from average cost')
        self.assertIsNone(stop_exit('momentum',100,81))
        self.assertIn('Held 10',daily_exit('reversion',{**s,'close':1,'dma5':2},10))
        self.assertIsNone(daily_exit('momentum',s,50))

class BasketEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'b.sqlite3';init(self.path);init_paper(self.path)
        self.clock=datetime(2026,10,12,10,0,tzinfo=IST).astimezone(timezone.utc)
        self.engine=AutoTrading(self.path)
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config')
            for name in cfg:cfg[name]['enabled']=False
            cfg['momentum']={'enabled':True,'buy_universe':'all','full_session_buying':True}
            set_state(db,'auto_config',cfg);set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            for n in range(12):
                symbol=f'S{n:02d}';rows=candles(.0005+.0003*n)
                db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(symbol,'NSE_EQ|'+symbol,symbol,symbol,'Test'))
                db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[(symbol,r['ts'],r['open'],r['high'],r['low'],r['close'],r['volume'],'fixture',self.clock.isoformat()) for r in rows])
                db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(symbol,rows[-1]['close'],rows[-2]['close'],int(self.clock.timestamp()*1000),self.clock.isoformat(),'test'))
        for n in range(12):seed_quality(self.path,f'S{n:02d}',self.clock)
        self.depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.depth.start();self.addCleanup(self.depth.stop)
    def tearDown(self):self.temp.cleanup()
    def holdings(self):
        with connect(self.path) as db:return sorted(r[0] for r in db.execute("SELECT symbol FROM positions WHERE portfolio='momentum' AND quantity>0"))

    def test_buys_the_ten_strongest_then_stops_out_a_loser(self):
        self.engine.cycle(self.clock,{})
        self.assertEqual(self.holdings(),[f'S{n:02d}' for n in range(2,12)])
        status=self.engine.snapshot()['status']['momentum']
        self.assertEqual(status['position_count'],10)
        with connect(self.path) as db:
            cost=db.execute("SELECT cost/quantity FROM positions WHERE portfolio='momentum' AND symbol='S05'").fetchone()[0]
            db.execute("UPDATE quotes SET price=? WHERE symbol='S05'",(cost*.75,))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM basket_positions WHERE portfolio='momentum'").fetchone()[0],10)
        self.engine.cycle(self.clock+timedelta(seconds=5),{})
        self.assertNotIn('S05',self.holdings())
        with connect(self.path) as db:
            sale=db.execute("SELECT reason FROM trades WHERE portfolio='momentum' AND side='SELL'").fetchone()[0]
            self.assertFalse(db.execute("SELECT 1 FROM basket_positions WHERE portfolio='momentum' AND symbol='S05'").fetchone())
        self.assertEqual(sale,'20% loss from average cost')
        self.assertNotIn('S05',self.holdings())  # no same-session re-entry

    def test_monthly_review_keeps_strong_holdings_and_marks_weak_ones(self):
        self.engine.cycle(self.clock,{})
        with connect(self.path) as db:
            db.execute("UPDATE basket_positions SET reviewed_month='2026-09' WHERE portfolio='momentum'")
            db.execute("DELETE FROM candles WHERE symbol='S03' AND ts>'2026-09-01'")  # S03 history now stale
        self.engine.baskets.loaded=0
        self.engine.cycle(self.clock+timedelta(seconds=5),{})
        with connect(self.path) as db:
            rows=dict(db.execute("SELECT symbol,reviewed_month FROM basket_positions WHERE portfolio='momentum'").fetchall())
            sold=[r[0] for r in db.execute("SELECT reason FROM trades WHERE portfolio='momentum' AND side='SELL' AND symbol='S03'")]
        self.assertEqual(rows['S11'],'2026-10')
        self.assertEqual(sold,['No valid completed history at monthly review'])
    def test_new_baskets_cap_round_trip_fees_at_one_and_a_half_percent(self):
        with connect(self.path) as db:cfg=get_state(db,'auto_config')
        for name in ('lowvol','high52','momentum12','alpha','clenow','overnight','gap_drift','sector_rotation','smart_beta','risk_parity','stat_arb','regime','news_momentum','news_catalyst'):
            self.assertEqual(cfg[name]['max_round_trip_fee_pct'],1.5,name)
        self.assertEqual(cfg['ema_price_action']['max_round_trip_fee_pct'],1.)
    def test_retired_basket_cannot_be_enabled_and_never_trades(self):
        with patch.dict('marketdesk.autotrading.RETIRED',{'reversion':'Retired: test'}):
            with connect(self.path) as db:
                cfg=get_state(db,'auto_config');cfg['reversion']['enabled']=True;set_state(db,'auto_config',cfg)   # even a hand-edited flag is ignored
            with self.assertRaisesRegex(ValueError,'Retired'):self.engine.configure('reversion',True)
            self.engine.cycle(self.clock,{})
            self.assertEqual(self.engine.snapshot()['status']['reversion']['state'],'paused')
            self.engine.configure('reversion',False)                               # pausing stays allowed
    def test_comparison_month_lifts_exits_only_and_retired_once(self):
        from marketdesk.baskets import init as basket_init
        with connect(self.path) as db:
            db.execute("DELETE FROM state WHERE key='comparison_month'")
            cfg=get_state(db,'auto_config');cfg['lowvol'].update(exits_only=True,exits_only_reason='failed');cfg['high52'].update(enabled=False,retired=True,retired_reason='old')
            cfg['momentum'].update(enabled=True,exits_only=False);set_state(db,'auto_config',cfg)
        basket_init(self.path)
        with connect(self.path) as db:cfg=get_state(db,'auto_config')
        for name in ('lowvol','high52'):
            self.assertTrue(cfg[name]['enabled']);self.assertFalse(cfg[name]['exits_only']);self.assertTrue(cfg[name]['comparison_only']);self.assertFalse(cfg[name]['retired'])
        self.assertEqual(cfg['lowvol']['previous']['exits_only_reason'],'failed');self.assertNotIn('comparison_only',cfg['momentum'])   # untouched
        with connect(self.path) as db:cfg['lowvol']['exits_only']=True;set_state(db,'auto_config',cfg)   # the owner switches it back
        basket_init(self.path)
        with connect(self.path) as db:self.assertTrue(get_state(db,'auto_config')['lowvol']['exits_only'])  # applied once only
    def test_research_basket_trades_the_backtest_ranking_exit_rule_and_entry_stop(self):
        latest=candles(0)[-1]['ts'][:10]
        def research(exit_now=()):
            return {'turtle':dict(as_of=latest,order=['S03','S07','S01'],keep_order=[],keep=30,monthly=False,max_hold=None,n=10,exit_now=set(exit_now),
                stop=None,stop_frame={'2026-09-30':{'S03':.10,'S07':.05},latest:{'S03':.10,'S07':.05}})}   # S01: no measurable stop, never bought
        held=lambda:dict(connect(self.path).execute("SELECT symbol,cost/quantity FROM positions WHERE portfolio='turtle' AND quantity>0").fetchall())
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');cfg['momentum']['enabled']=False;cfg['turtle']={'enabled':True,'buy_universe':'all','full_session_buying':True};set_state(db,'auto_config',cfg)
        with patch('marketdesk.research_live.signals',return_value=research()):self.engine.cycle(self.clock,{})
        self.assertEqual(sorted(held()),['S03','S07'])
        with connect(self.path) as db:
            db.execute("UPDATE basket_positions SET entry_session='2026-10-01' WHERE portfolio='turtle'")                 # bought before the signal session
            db.execute("UPDATE quotes SET price=? WHERE symbol='S07'",(held()['S07']*.94,))                              # 6% down: through its 5% stop
        self.engine.baskets.research={};self.engine.baskets.loaded=0
        with patch('marketdesk.research_live.signals',return_value=research(exit_now={'S03'})):self.engine.cycle(self.clock+timedelta(seconds=5),{})
        with connect(self.path) as db:reasons=dict(db.execute("SELECT symbol,reason FROM trades WHERE portfolio='turtle' AND side='SELL'").fetchall())
        self.assertEqual(held(),{});self.assertEqual(reasons,{'S03':'Exit rule met on the completed session','S07':'5% stop from average cost'})
    def test_paused_basket_never_trades(self):
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');cfg['momentum']['enabled']=False;set_state(db,'auto_config',cfg)
        self.engine.cycle(self.clock,{})
        self.assertEqual(self.holdings(),[])

if __name__=='__main__':unittest.main()
