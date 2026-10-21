import json,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.autotrading import AutoTrading,IST
from marketdesk.paper import init_paper
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.execution import intraday_charges
from marketdesk.signal_baskets import vinay_rules
from marketdesk.signal_baskets.runner import SignalRunner
from marketdesk.signal_baskets.report import snapshot as report
from support import seed_quality,fixture_book

# The original tighter settings: every optional exit on, so the tests cover the whole state machine.
FULL=dict(min_gap_open_pct=-99.,max_vwap_extension_pct=1.5,min_rvol=1.3,min_close_location=.5,min_stop_pct=.6,max_stop_pct=1.2,target_r=2.,
    breakeven_r=1.,trail_start_r=1.5,time_stop_candles=12,vwap_exit=True,risk_pct=1.,max_position_pct=33.,max_positions=3,max_open_risk_pct=3.,max_entries_per_day=6,fee_max_pct=.25)

def cost(v):return intraday_charges(v,'BUY')['total']+intraday_charges(v,'SELL')['total']

def bar(day,hm,o,h,l,c,v):return dict(ts=f'{day}T{hm}:00+05:30',open=o,high=h,low=l,close=c,volume=v)

def session_times():
    t=datetime(2026,1,1,9,15)
    while (t.hour,t.minute)<(15,30):yield t.strftime('%H:%M');t+=timedelta(minutes=5)

def leader(today_volume=20000,closes=None,prior_close=1000.):
    """Four flat sessions at 1000, then 16 Oct 2026: a 1005 open, a 1003-1008 opening range and a steady climb."""
    bars=[]
    for d in ('2026-10-12','2026-10-13','2026-10-14','2026-10-15'):
        for hm in session_times():bars.append(bar(d,hm,1000,1001,999,1000 if d!='2026-10-15' else prior_close,10000))
    for hm,(o,h,l,c) in zip(('09:15','09:20','09:25'),((1005,1008,1003,1006),(1006,1007,1004,1006.5),(1006.5,1007.5,1005,1006))):
        bars.append(bar('2026-10-16',hm,o,h,l,c,today_volume))
    prev=1006
    for hm,c in zip(('09:30','09:35','09:40','09:45','09:50','09:55','10:00','10:05'),closes or [1008.5+i for i in range(8)]):
        bars.append(bar('2026-10-16',hm,prev,max(prev,c)+.5,min(prev,c)-.5,c,today_volume));prev=c
    return bars

class LeaderScanTests(unittest.TestCase):
    def test_strong_mover_above_range_and_vwap_is_a_candidate(self):
        ev=vinay_rules.evaluate(leader(),FULL)
        self.assertEqual(ev['status'],'BUY_CANDIDATE',ev['reasons'])
        self.assertAlmostEqual(ev['day_change_pct'],1.55,places=2);self.assertAlmostEqual(ev['rvol'],2.,places=2)
        self.assertEqual(ev['or_high'],1008);self.assertEqual(ev['stop_pct'],.6)       # quiet ATR: the 0.6% floor binds
        self.assertGreater(ev['score'],3)

    def test_rejections_say_why(self):
        self.assertIn('extended',' '.join(vinay_rules.evaluate(leader(),FULL,prev_close=960.)['reasons']))
        self.assertIn('Relative volume',' '.join(vinay_rules.evaluate(leader(today_volume=11000),FULL)['reasons']))
        weak=vinay_rules.evaluate(leader(closes=[1007,1007.5,1007,1006.5,1007,1007.2,1007.5,1007.8]),FULL)
        self.assertIn('opening range',' '.join(weak['reasons']));self.assertEqual(weak['status'],'REJECTED')
        self.assertIsNone(vinay_rules.evaluate(leader()[:-8],FULL))                          # still inside the opening range

    def test_fee_rules_keep_positions_large_and_respect_the_monthly_budget(self):
        self.assertIsNone(vinay_rules.fee_check(1000,95,cost,{}))                              # ₹95k: ~₹80, 0.08%
        self.assertIn('above 0.1%',vinay_rules.fee_check(1000,30,cost,{}))                     # ₹30k: ~₹57, 0.19%
        self.assertIn('Monthly fee budget',vinay_rules.fee_check(1000,95,cost,{},month_fees=450))

    def test_default_gap_leader_holds_to_the_close_with_a_stop(self):
        bars=leader();bars=[dict(b,open=b['open']+10) if b['ts']=='2026-10-16T09:15:00+05:30' else b for b in bars]   # opened +1.5%
        ev=vinay_rules.evaluate(bars)
        self.assertEqual(ev['status'],'BUY_CANDIDATE',ev['reasons']);self.assertEqual(ev['stop_pct'],2.5)
        self.assertEqual(vinay_rules.evaluate(leader())['reasons'],['Opened +0.50% gapped, below +1%'])
        st=vinay_rules.arm(1000.,90,2.5,{});self.assertFalse(st['target_active'])
        self.assertEqual(vinay_rules.on_price(st,1100,{}),(None,[]))                           # no target: rides to 15:10
        self.assertEqual(vinay_rules.on_price(st,974,{})[0]['kind'],'STOP_LOSS')

class ExitTests(unittest.TestCase):
    def armed(self):return vinay_rules.arm(1000.,20,1.,FULL,cost_per_share=cost(20000)/20,entry_at='2026-10-16T04:40:00+00:00')

    def test_breakeven_after_fees_trail_half_at_target_then_trailing_stop(self):
        st=self.armed();self.assertEqual((st['stop'],st['target']),(990.,1020.))
        self.assertEqual(vinay_rules.on_price(st,1005,FULL),(None,[]))
        _,notes=vinay_rules.on_price(st,1010,FULL);self.assertEqual(st['stage'],'BREAKEVEN_ACTIVE')
        self.assertGreater(st['stop'],1000+cost(20000)/20-.05)                           # a stop-out from here is not a net loss
        vinay_rules.on_price(st,1016,FULL);self.assertTrue(st['trail_active']);self.assertEqual(st['stop'],1006.)
        intent,_=vinay_rules.on_price(st,1020.5,FULL);self.assertEqual((intent['kind'],intent['quantity']),('PARTIAL',10))
        vinay_rules.apply_fill(st,intent,10,1020,FULL);self.assertFalse(st['target_active']);self.assertEqual(st['qty'],10)
        vinay_rules.on_price(st,1030,FULL);self.assertEqual(st['stop'],1020.)                 # the runner keeps at least +2R
        intent,_=vinay_rules.on_price(st,1019,FULL);self.assertEqual(intent['kind'],'TRAILING_STOP')
        self.assertEqual(vinay_rules.apply_fill(st,intent,10,1019,FULL)[-1][0],'CLOSED')

    def test_stop_loss_is_one_r(self):
        intent,_=vinay_rules.on_price(self.armed(),989.9,FULL)
        self.assertEqual((intent['kind'],intent['level']),('STOP_LOSS',990.))

    def test_vwap_break_and_time_stop_exit_at_market(self):
        st=self.armed();bars=leader()
        bars.append(bar('2026-10-16','10:10',1015.5,1015.5,990,991,20000))
        vinay_rules.on_bar(st,bars,FULL);self.assertEqual(st['exit_now'][0],'VWAP_EXIT')
        intent,_=vinay_rules.on_price(st,995,FULL);self.assertEqual((intent['kind'],intent['order']),('VWAP_EXIT','market'))
        st=self.armed();st['entry_at']='2026-10-16T03:59:00+00:00'                         # 09:29 IST: 8 candles held, no +0.5R
        vinay_rules.on_bar(st,leader(),{**FULL,'time_stop_candles':8})
        self.assertEqual(st['exit_now'][0],'TIME_STOP')

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'v.sqlite3';init(self.path);init_paper(self.path)
        self.clock=datetime(2026,10,16,10,10,30,tzinfo=IST).astimezone(timezone.utc)
        self.auto=AutoTrading(self.path)
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config')
            for n in cfg:cfg[n]['enabled']=False
            cfg['vinay_intraday'].update(enabled=True,buy_universe='all',params=dict(FULL))
            set_state(db,'auto_config',cfg);set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            db.execute("INSERT INTO instruments VALUES('ABC','NSE_EQ|INE000A01011','INE000A01011','ABC Ltd','Industrials')")
            db.executemany('INSERT INTO sb_intraday VALUES(?,?,?,?,?,?,?,?)',[('ABC',5,b['ts'],b['open'],b['high'],b['low'],b['close'],b['volume']) for b in leader()])
        seed_quality(self.path,'ABC',self.clock)
        with connect(self.path) as db:db.execute("UPDATE candles SET open=1000,high=1010,low=990,close=1000 WHERE symbol='ABC'")
        self.quote(1015.5)
        self.depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.depth.start();self.addCleanup(self.depth.stop)
        self.runner=SignalRunner(self.path,candles=object())
    def tearDown(self):self.temp.cleanup()
    def quote(self,price,clock=None):
        clock=clock or self.clock
        with connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)',('ABC',price,None,int(clock.timestamp()*1000),clock.isoformat(),'test'))
            db.execute('UPDATE quality SET checked_at=?',(clock.isoformat(),))
            set_state(db,'signal_ema_checkpoint',{'last_success':clock.isoformat()})
    def held(self):
        with connect(self.path) as db:return (db.execute("SELECT quantity FROM positions WHERE portfolio='vinay_intraday' AND symbol='ABC'").fetchone() or [0])[0]
    def state(self):
        with connect(self.path) as db:
            r=db.execute("SELECT state FROM sb_positions WHERE portfolio='vinay_intraday'").fetchone()
            return json.loads(r['state']) if r else None
    def scan(self):
        from marketdesk.signal_baskets.engine import params
        with connect(self.path) as db:p=params('vinay_intraday',get_state(db,'auto_config')['vinay_intraday'])
        return self.runner.vinay_signals(['ABC'],self.clock,p)

    def test_candidate_buys_at_live_price_with_exits_and_one_trade_per_day(self):
        summary=self.scan();self.assertEqual(summary['candidates'],1)
        self.scan()
        with connect(self.path) as db:self.assertEqual(db.execute("SELECT COUNT(*) FROM sb_signals WHERE portfolio='vinay_intraday'").fetchone()[0],1)
        self.auto.cycle(self.clock,{})
        qty=self.held();self.assertGreater(qty,0)
        st=self.state();self.assertEqual(st['stage'],'INITIAL_EXITS_ARMED')
        self.assertLessEqual(qty*st['entry'],100000*.33+1)                                  # 33% of the ₹1 lakh basket at most
        with connect(self.path) as db:
            t=db.execute("SELECT execution,reason FROM trades WHERE portfolio='vinay_intraday'").fetchone()
        self.assertIn('Intraday',json.loads(t['execution'])['charge_basis']);self.assertIn('PAPER',t['reason'])
        later=self.clock+timedelta(minutes=1);self.quote(st['stop']-1,later);self.auto.cycle(later,{})
        self.assertEqual(self.held(),0);self.assertIsNone(self.state())
        self.scan();self.auto.cycle(later+timedelta(seconds=20),{})
        self.assertEqual(self.held(),0)                                                     # never re-bought the same day

    def test_no_chasing_and_square_off(self):
        self.scan();self.quote(1015.5*1.006);self.auto.cycle(self.clock,{})
        self.assertEqual(self.held(),0)                                                     # ran away more than 0.4%
        self.quote(1015.5);self.auto.cycle(self.clock,{});self.assertGreater(self.held(),0)
        close=datetime(2026,10,16,15,10,5,tzinfo=IST).astimezone(timezone.utc);self.quote(1016,close);self.auto.cycle(close,{})
        self.assertEqual(self.held(),0)
        with connect(self.path) as db:self.assertIn('square-off',db.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0])

    def test_daily_loss_limit_stops_new_entries(self):
        with connect(self.path) as db:
            db.execute("INSERT INTO sb_equity VALUES('vinay_intraday','2026-10-16T09:15:00+05:30',100000,100000,0)")
            db.execute("UPDATE portfolios SET cash=97500 WHERE name='vinay_intraday'")
        self.scan();self.auto.cycle(self.clock,{})
        self.assertEqual(self.held(),0)
        self.assertIn('Day loss',self.auto.snapshot()['status']['vinay_intraday']['message'])

    def test_report_lists_the_basket(self):
        self.scan()
        payload=report(self.path,self.clock);b=[x for x in payload['baskets'] if x['name']=='vinay_intraday'][0]
        self.assertEqual(b['title'],'Vinay Intraday Trader');self.assertEqual(b['capital'],100000)
        self.assertEqual(b['signals'][0]['evidence']['or_high'],1008);json.dumps(payload,allow_nan=False)

if __name__=='__main__':unittest.main()
