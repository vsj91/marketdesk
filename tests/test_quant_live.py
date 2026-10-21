import json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.autotrading import AutoTrading,IST
from marketdesk.paper import init_paper
from marketdesk.storage import init,connect,set_state,get_state
from support import seed_quality,fixture_book

class QuantLiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'m.sqlite3';init(self.path);init_paper(self.path)
        self.clock=datetime(2026,10,19,10,0,tzinfo=IST).astimezone(timezone.utc)
        self.auto=AutoTrading(self.path);self.engine=self.auto.quant.engine
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config')
            for name in cfg:cfg[name]['enabled']=False
            cfg['stat_arb']={'enabled':True,'buy_universe':'all','full_session_buying':True,'max_round_trip_fee_pct':2.5}
            set_state(db,'auto_config',cfg);set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            for s in ('AAA','BBB'):
                db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',(s,'NSE_EQ|'+s,s,s,'Test'))
                db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',(s,100.,100.,int(self.clock.timestamp()*1000),self.clock.isoformat(),'test'))
        for s in ('AAA','BBB'):seed_quality(self.path,s,self.clock)
        depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);depth.start();self.addCleanup(depth.stop)
        self.engine.create('stat_arb','stat_arb',100_000,day='2026-10-16');self.engine.go_live('stat_arb')

    def queue(self,symbol,side,qty,signal_close=100.):
        with connect(self.engine.path) as db:
            return db.execute('INSERT INTO qb_orders(basket,signal_day,symbol,side,qty,kind,reason,details) VALUES(?,?,?,?,?,?,?,?)',
                ('stat_arb','2026-10-16',symbol,side,qty,'add' if side=='BUY' else 'exit','Z-score reached -2.10',json.dumps(dict(signal_close=signal_close)))).lastrowid
    def trades(self):
        with connect(self.path) as db:return [dict(r) for r in db.execute("SELECT * FROM trades WHERE portfolio='stat_arb' ORDER BY created_at")]
    def order(self,oid):
        with connect(self.engine.path) as db:return dict(db.execute('SELECT * FROM qb_orders WHERE id=?',(oid,)).fetchone())
    def held(self,s):
        with connect(self.path) as db:
            r=db.execute("SELECT quantity FROM positions WHERE portfolio='stat_arb' AND symbol=?",(s,)).fetchone()
        return r[0] if r else 0

    def test_queued_buy_fills_through_paper_trade_once(self):
        oid=self.queue('AAA','BUY',50)
        self.auto.cycle(self.clock,{})
        t=self.trades();self.assertEqual(len(t),1)
        self.assertEqual((t[0]['source'],t[0]['side'],t[0]['quantity']),('automatic','BUY',50))
        self.assertTrue(t[0]['request_id'].startswith(f'quant-stat_arb-{oid}-'))
        self.assertIn('daily signal, live fill',t[0]['reason'])
        self.assertEqual(self.held('AAA'),50);self.assertEqual(self.order(oid)['status'],'filled')
        p=self.engine.payload('stat_arb')
        self.assertEqual([h['ticker'] for h in p['holdings']],['AAA']);self.assertEqual(p['execution'],'live')
        self.assertTrue(any(r['action']=='FILL' and 'live' in r['reason'] for r in p['rebalance_log']))
        self.auto.cycle(self.clock,{})
        self.assertEqual(len(self.trades()),1)                                   # never filled twice
        status=self.auto.snapshot()['status']['stat_arb'];self.assertIn('All queued orders done',status['message'])

    def test_crash_between_ledgers_is_recovered_without_a_second_fill(self):
        oid=self.queue('AAA','BUY',50);self.auto.cycle(self.clock,{})
        with connect(self.engine.path) as db:                                    # main ledger filled, quant bookkeeping lost
            db.execute("UPDATE qb_orders SET status='pending' WHERE id=?",(oid,));db.execute('DELETE FROM qb_fills');db.execute("DELETE FROM qb_rebalance_log WHERE action='FILL'")
        self.auto.cycle(self.clock,{})
        self.assertEqual(len(self.trades()),1);self.assertEqual(self.order(oid)['status'],'filled')
        with connect(self.engine.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM qb_fills').fetchone()[0],1)

    def test_sell_exits_and_books_realized_pnl(self):
        self.queue('AAA','BUY',50);self.auto.cycle(self.clock,{})
        oid=self.queue('AAA','SELL',50);self.auto.cycle(self.clock,{})
        self.assertEqual(self.held('AAA'),0);self.assertEqual(self.order(oid)['status'],'filled')
        with connect(self.engine.path) as db:
            pnl=db.execute("SELECT realized_pnl FROM qb_fills WHERE side='SELL'").fetchone()[0]
            self.assertIsNone(db.execute("SELECT 1 FROM qb_positions WHERE basket='stat_arb'").fetchone())
        self.assertLess(pnl,0)                                                   # a same-price round trip loses the charges

    def test_buys_wait_on_price_gap_pause_and_closed_market(self):
        far=self.queue('AAA','BUY',10,signal_close=80.)                          # live 100 is 25% above the signal
        self.auto.cycle(self.clock,{})
        self.assertEqual(self.trades(),[]);self.assertEqual(self.order(far)['status'],'pending')
        self.assertIn('AAA',self.auto.snapshot()['status']['stat_arb']['message'])
        near=self.queue('BBB','BUY',10)
        with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'CLOSED'}})
        self.auto.cycle(self.clock,{});self.assertEqual(self.trades(),[])
        with connect(self.path) as db:
            set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            cfg=get_state(db,'auto_config');cfg['stat_arb']['enabled']=False;set_state(db,'auto_config',cfg)
        self.auto.cycle(self.clock,{});self.assertEqual(self.trades(),[]);self.assertEqual(self.order(near)['status'],'pending')

    def test_daily_run_mirrors_the_live_portfolio_and_replaces_unfilled_orders(self):
        self.queue('AAA','BUY',50);self.auto.cycle(self.clock,{})
        stale=self.queue('BBB','BUY',10,signal_close=50.)
        bars=lambda p:[dict(day=f'2026-10-{d:02d}',open=p,high=p,low=p,close=p,volume=1e6) for d in (15,16,19)]
        snap=dict(day='2026-10-19',bars={'AAA':bars(100.),'BBB':bars(100.)},fundamentals={},sectors={},size={},benchmark=[])
        with connect(self.engine.path) as db:db.execute("UPDATE qb_baskets SET last_rebalance='2026-10-16'")
        result=self.engine.run_basket('stat_arb',snap)
        with connect(self.path) as db:cash=db.execute("SELECT cash FROM portfolios WHERE name='stat_arb'").fetchone()[0]
        self.assertAlmostEqual(result['cash'],cash,2);self.assertEqual(result['fills'],1)
        self.assertEqual(result['holdings'],1);self.assertAlmostEqual(result['nav'],cash+50*100.,2)
        self.assertEqual(self.order(stale)['status'],'cancelled')                # superseded by the 19 Oct plan

    def test_buy_too_small_for_fee_floor_is_cancelled_once(self):
        oid=self.queue('AAA','BUY',2)                                            # Rs 200: fees far above 2.5% at any fill
        self.auto.cycle(self.clock,{})
        self.assertEqual(self.trades(),[]);self.assertEqual(self.order(oid)['status'],'cancelled')
        self.auto.cycle(self.clock,{})
        with connect(self.path) as db:n=db.execute("SELECT COUNT(*) FROM auto_events WHERE portfolio='stat_arb' AND reason LIKE 'Buy too small%'").fetchone()[0]
        self.assertEqual(n,1)

    def test_executor_error_pauses_only_that_basket(self):
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');cfg['momentum']['enabled']=True;set_state(db,'auto_config',cfg)
        with patch.object(self.auto.quant,'scan',side_effect=RuntimeError('boom')):self.auto.cycle(self.clock,{})
        cfg=self.auto.snapshot()['config']
        self.assertFalse(cfg['stat_arb']['enabled']);self.assertTrue(cfg['momentum']['enabled'])
        self.assertEqual(self.auto.snapshot()['status']['stat_arb']['state'],'error')

if __name__=='__main__':unittest.main()
