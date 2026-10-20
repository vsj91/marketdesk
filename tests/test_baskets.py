import tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.autotrading import AutoTrading,IST
from marketdesk.basket_rules import stats,ranking,rebalance_exit,daily_exit,stop_exit,rsi
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
        for name in ('lowvol','high52','momentum12','smart_beta','risk_parity','stat_arb','regime','news_momentum','news_catalyst'):
            self.assertEqual(cfg[name]['max_round_trip_fee_pct'],1.5,name)
        self.assertEqual(cfg['ema_price_action']['max_round_trip_fee_pct'],1.)
    def test_retired_basket_cannot_be_enabled_and_never_trades(self):
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');self.assertTrue(cfg['reversion']['retired']);self.assertFalse(cfg['reversion']['enabled'])
            self.assertTrue(cfg['high52']['retired']);self.assertFalse(cfg['high52']['enabled'])
            cfg['reversion']['enabled']=True;set_state(db,'auto_config',cfg)   # even a hand-edited flag is ignored
        with self.assertRaisesRegex(ValueError,'Retired'):self.engine.configure('reversion',True)
        self.engine.cycle(self.clock,{})
        self.assertEqual(self.engine.snapshot()['status']['reversion']['state'],'paused')
        self.engine.configure('reversion',False)                               # pausing stays allowed
    def test_paused_basket_never_trades(self):
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');cfg['momentum']['enabled']=False;set_state(db,'auto_config',cfg)
        self.engine.cycle(self.clock,{})
        self.assertEqual(self.holdings(),[])

if __name__=='__main__':unittest.main()
