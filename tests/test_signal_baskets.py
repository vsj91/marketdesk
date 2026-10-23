import json,math,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.autotrading import AutoTrading,IST
from marketdesk.paper import init_paper
from marketdesk.storage import init,connect,set_state,get_state
from marketdesk.signal_baskets import ema_rules,news,news_rules
from marketdesk.signal_baskets.indicators import ema_series,rsi_series,atr_series,round_tick,swing_points
from marketdesk.signal_baskets.runner import SignalRunner,news_session
from marketdesk.signal_baskets.config import update as update_config
from marketdesk.signal_baskets.report import snapshot as report
from support import seed_quality,fixture_book

# ---- fixtures -------------------------------------------------------------------------------------

def series(closes,start=datetime(2026,10,13,9,15),wick=.0012,vol=10000):
    bars=[];t=start
    for c in closes:
        o=bars[-1]['close'] if bars else c
        bars.append(dict(ts=t.isoformat()+'+05:30',open=round(o,2),high=round(max(o,c)*(1+wick),2),low=round(min(o,c)*(1-wick),2),close=round(c,2),volume=vol))
        t+=timedelta(minutes=5)
        if (t.hour,t.minute)>=(15,30):
            t=(t+timedelta(days=1)).replace(hour=9,minute=15)
            while t.weekday()>=5:t+=timedelta(days=1)
    return bars

def pullback(dip=8,dip_pct=.01,rise=.0008,wiggle=.0006):
    """Uptrend, pullback, recovery: the 9/21 EMA crossover lands on bar 235 (16 Oct 2026, 10:05 IST)."""
    c=[];p=900.
    for i in range(220):p*=1.0005;c.append(p*(1+wiggle*(-1)**i))
    for i in range(dip):p*=(1-dip_pct/dip);c.append(p)
    for i in range(40):p*=1+rise;c.append(p)
    return series(c)

VALID={'structure_lookback':10,'stop_method':'atr','min_rr':1.9}   # a setup whose RR (1.98) clears 1.9
BARS=pullback();TRIGGER=235

def daily(closes,end=datetime(2026,10,16).date(),volume=200000,last_volume=None):
    days=[];d=end
    while len(days)<len(closes):
        if d.weekday()<5:days.append(d)
        d-=timedelta(days=1)
    days.reverse();out=[]
    for i,(d,c) in enumerate(zip(days,closes)):
        o=closes[i-1] if i else c
        out.append(dict(ts=d.isoformat()+'T00:00:00+05:30',open=o,high=max(o,c)*1.01,low=min(o,c)*.99,close=c,volume=last_volume if last_volume and i==len(closes)-1 else volume))
    return out

def analysis(**kw):
    base=dict(verification='verified',direction=1,label='Positive',category='order_win',momentum_eligible=True,catalyst_eligible=True,governance=False,
        age_hours=1.,impact_score=75.,confidence=.8,components={'novelty':100},materiality_ratio=.2,run_up_pct=2.)
    base.update(kw);return base

# ---- pure indicator and EMA rule tests ------------------------------------------------------------

class IndicatorTests(unittest.TestCase):
    def test_wilder_and_ema_kernels(self):
        self.assertEqual(ema_series([1,2,3],3)[-1],2.)
        self.assertIsNone(ema_series([1,2],3)[-1])
        self.assertEqual(rsi_series(list(range(1,20)))[-1],100.)
        bars=[dict(high=11,low=9,close=10)]*20
        self.assertAlmostEqual(atr_series(bars)[-1],2.)
        self.assertEqual(round_tick(100.01,.05,'up'),100.05);self.assertEqual(round_tick(100.04,.05,'down'),100.)

    def test_swing_points_never_use_unfinished_bars(self):
        bars=[dict(high=h,low=h-1) for h in (5,4,3,2,3,4,5)]
        _,lows=swing_points(bars,k=3)
        self.assertEqual([p['index'] for p in lows],[3])
        _,lows=swing_points(bars[:6],k=3)           # only two bars after the low: not yet confirmed
        self.assertEqual(lows,[])

class EmaRuleTests(unittest.TestCase):
    def test_valid_bullish_crossover(self):
        ev=ema_rules.evaluate(BARS[:TRIGGER+1],VALID)
        self.assertEqual(ev['status'],'SIGNAL_DETECTED',ev['reasons'])
        self.assertTrue(ev['ema9']>ev['ema21'] and ev['close']>ev['ema200'] and 55<=ev['rsi14']<=70)
        self.assertEqual(ev['entry_trigger'],round_tick(BARS[TRIGGER]['high']*1.0005,.05,'up'))
        self.assertGreater(ev['limit_price'],ev['entry_trigger'])
        self.assertAlmostEqual(ev['risk_reward'],(ev['initial_target']-ev['entry_trigger'])/(ev['entry_trigger']-ev['initial_stop']))

    def test_no_crossover_returns_nothing(self):
        self.assertIsNone(ema_rules.evaluate(BARS[:TRIGGER],VALID))
        self.assertIsNone(ema_rules.evaluate(BARS[:100],VALID))          # not enough bars for EMA200

    def test_price_below_ema200(self):
        ev=ema_rules.evaluate(BARS[:TRIGGER+1],{**VALID,'ema_trend':20})
        bars=[dict(b,close=b['close']) for b in BARS[:TRIGGER+1]]
        ind=ema_rules.indicators(bars,ema_rules.merged(VALID))
        with patch('marketdesk.signal_baskets.ema_rules.indicators',return_value={**ind,'trend':[x and x*2 for x in ind['trend']]}):
            ev=ema_rules.evaluate(bars,VALID)
        self.assertTrue(any('not above EMA200' in r for r in ev['reasons']));self.assertEqual(ev['status'],'REJECTED')

    def test_rsi_band(self):
        low=ema_rules.evaluate(BARS[:TRIGGER+1],{**VALID,'rsi_min':65})
        self.assertTrue(any('below 65' in r for r in low['reasons']))
        high=ema_rules.evaluate(BARS[:TRIGGER+1],{**VALID,'rsi_max':56})
        self.assertTrue(any('above 56' in r for r in high['reasons']))

    def test_missing_atr_rejects(self):
        ind=ema_rules.indicators(BARS[:TRIGGER+1],ema_rules.merged(VALID));ind['atr'][-1]=None
        with patch('marketdesk.signal_baskets.ema_rules.indicators',return_value=ind):
            ev=ema_rules.evaluate(BARS[:TRIGGER+1],VALID)
        self.assertEqual(ev['status'],'REJECTED');self.assertIn('ATR14 unavailable or invalid',ev['reasons'])

    def test_resistance_too_close(self):
        ev=ema_rules.evaluate(BARS[:TRIGGER+1],{**VALID,'structure_lookback':150})   # the pre-pullback high is overhead
        self.assertEqual(ev['status'],'REJECTED')
        self.assertTrue(any('resistance too close' in r for r in ev['reasons']),ev['reasons'])

    def test_risk_reward_below_minimum(self):
        # Hybrid stop >= 1.5 ATR and target <= 3 ATR cap RR at 2.0; tick rounding pushes it just below,
        # so the spec's 2.0 minimum rejects even the best case and the default minimum is 1.8.
        atr={'structure_lookback':10,'stop_method':'atr'}
        self.assertEqual(ema_rules.DEFAULTS['min_rr'],1.8)
        self.assertEqual(ema_rules.evaluate(BARS[:TRIGGER+1],atr)['status'],'SIGNAL_DETECTED')
        ev=ema_rules.evaluate(BARS[:TRIGGER+1],{**atr,'min_rr':2.})
        self.assertEqual(ev['status'],'REJECTED');self.assertLess(ev['risk_reward'],2.)
        ev=ema_rules.evaluate(BARS[:TRIGGER+1],{'structure_lookback':10})   # hybrid: the wider structural stop binds
        self.assertEqual(ev['status'],'REJECTED');self.assertTrue(any(r.startswith('Risk-reward') for r in ev['reasons']))

    def test_position_size_is_min_of_risk_capital_and_heat(self):
        s=ema_rules.size(100000,100000,1000,990,{})
        self.assertEqual(s['binding'],'capital');self.assertEqual(s['quantity'],s['by_capital'])
        s=ema_rules.size(100000,100000,100,90,{'max_position_pct':50})
        self.assertEqual(s['binding'],'risk_per_trade');self.assertLessEqual(s['quantity']*(10+.1),1000)
        self.assertEqual(ema_rules.size(100000,100000,100,90,{},open_risk=3000)['quantity'],0)   # portfolio heat used up

    def state(self,qty=10):
        sig=dict(side='long',atr14=2.,initial_stop=96.,initial_target=106.)
        return ema_rules.arm(sig,100.,qty,{},cost_per_share=.1)

    def test_breakeven_partial_and_trailing(self):
        st=self.state()
        self.assertEqual(st['be_trigger'],104.)          # later of 1.5 ATR (103) and 1R (104)
        self.assertEqual(st['partial_trigger'],105.)
        intent,notes=ema_rules.on_price(st,104.,{})
        self.assertIsNone(intent);self.assertEqual(st['stage'],'BREAKEVEN_ACTIVE');self.assertEqual(st['stop'],100.1)
        intent,_=ema_rules.on_price(st,105.,{});self.assertEqual((intent['kind'],intent['quantity']),('PARTIAL',5))
        ema_rules.apply_fill(st,intent,5,105.,{})
        self.assertEqual(st['qty'],5);self.assertFalse(st['target_active']);self.assertEqual(st['stage'],'EMA_TRAILING_ACTIVE')
        bars=[dict(close=110.,high=110.,low=109.)]*25
        ema_rules.on_bar(st,bars,{});self.assertEqual(st['stop'],109.9)
        ema_rules.on_bar(st,[dict(close=101.,high=101,low=100)]*25,{});self.assertEqual(st['stop'],109.9)   # never moves back
        intent,_=ema_rules.on_price(st,109.,{});self.assertEqual(intent['kind'],'TRAILING_STOP')

    def test_partial_never_exceeds_position_and_small_positions_skip_it(self):
        st=self.state(qty=1);ema_rules.on_price(st,104.,{});intent,notes=ema_rules.on_price(st,105.,{})
        self.assertIsNone(intent);self.assertTrue(st['trail_active']);self.assertEqual(st['qty'],1)
        st=self.state(qty=3);ema_rules.on_price(st,104.,{});intent,_=ema_rules.on_price(st,105.,{})
        self.assertEqual(intent['quantity'],1)
        ema_rules.apply_fill(st,intent,99,105.,{});self.assertEqual(st['qty'],0)   # an over-reported fill is capped at the position

    def test_stop_and_target(self):
        st=self.state();intent,_=ema_rules.on_price(st,95.,{});self.assertEqual(intent['kind'],'STOP_LOSS')
        st=self.state();st['target']=103.5;intent,_=ema_rules.on_price(st,103.6,{});self.assertEqual(intent['kind'],'TARGET')

    def test_gap_through_stop_fills_at_open(self):
        st=self.state();exits,_=ema_rules.simulate_bar(st,dict(open=94.,high=95.,low=93.,close=94.5),{},0.)
        self.assertEqual(exits,[('STOP_LOSS',10,94.)])

    def test_simultaneous_stop_and_target_assumes_stop(self):
        st=self.state();st['target']=103.
        exits,_=ema_rules.simulate_bar(st,dict(open=100.,high=103.5,low=95.5,close=100.),{},0.)
        self.assertEqual(exits[0][0],'STOP_LOSS');self.assertEqual(st['qty'],0)

    def test_intrabar_breakeven_then_return_counts_as_hit(self):
        st=self.state();exits,_=ema_rules.simulate_bar(st,dict(open=100.,high=104.2,low=99.,close=99.5),{},0.)
        self.assertEqual(exits[0][0],'BREAKEVEN_STOP');self.assertEqual(exits[0][2],100.1)

    def test_short_rules_are_research_only(self):
        from marketdesk.signal_baskets.engine import validate_params
        with self.assertRaisesRegex(ValueError,'research-only|not configurable'):validate_params('ema_price_action',{'allow_short':True})
        mirrored=[dict(open=2000-b['open'],high=2000-b['low'],low=2000-b['high'],close=2000-b['close'],volume=b['volume'],ts=b['ts']) for b in BARS[:TRIGGER+1]]
        ev=ema_rules.evaluate(mirrored,{**VALID,'short_rsi_min':30,'short_rsi_max':45},side='short')
        self.assertIsNotNone(ev);self.assertEqual(ev['side'],'short')
        self.assertLess(ev['entry_trigger'],mirrored[-1]['low']);self.assertGreater(ev['initial_stop'],ev['entry_trigger'])

# ---- news analysis --------------------------------------------------------------------------------

NOW=datetime(2026,10,16,10,0,tzinfo=IST)
def item(subject='Bagging/Receiving of orders/contracts',headline='ABC Ltd has received an order worth Rs 5,000 crore from NHAI to be executed over 3 years',published=NOW-timedelta(hours=1)):
    return dict(subject=subject,headline=headline,published_at=published.isoformat(),id=1)

class NewsTests(unittest.TestCase):
    def test_normalize_and_dedup(self):
        row={'symbol':'ABC','sm_isin':'INE000A01011','sm_name':'ABC Limited','desc':'Press Release','attchmntText':'ABC Limited has informed the Exchange about a new order',
             'an_dt':'16-Oct-2026 09:05:00','seq_id':'1','attchmntFile':'https://nsearchives.nseindia.com/corporate/x.pdf'}
        a=news.normalize(row,NOW);b=news.normalize(dict(row,seq_id='2',attchmntText='ABC Ltd. has informed the exchange about a new order'),NOW)
        self.assertEqual(a['published_at'],'2026-10-16T09:05:00+05:30');self.assertEqual(a['dedup_key'],b['dedup_key'])
        self.assertIsNone(news.normalize(dict(row,an_dt='bad',sort_date=None),NOW))

    def test_positive_verified_order_with_materiality(self):
        a=news.analyse(item(),revenue=10000,prior=[],bars=[],price=None,now=NOW,verification='verified')
        self.assertEqual(a['category'],'order_win');self.assertEqual(a['label'],'Strongly Positive')
        self.assertAlmostEqual(a['materiality_ratio'],.5);self.assertEqual(a['execution_years'],3)
        self.assertIn('Expected margins not disclosed',a['missing'])

    def test_amounts_never_convert_foreign_currency(self):
        inr,foreign=news.amounts('Order of USD 12 million and Rs. 45 lakh')
        self.assertEqual([round(x['crore'],2) for x in inr],[.45]);self.assertEqual(foreign[0]['currency'],'USD')

    def test_negative_announcement(self):
        a=news.analyse(item('Credit Rating','CRISIL has downgraded the long term rating of ABC Ltd'),revenue=1000,now=NOW,verification='verified')
        self.assertEqual(a['label'],'Negative')
        status,reasons=news_rules.momentum_screen(a,{});self.assertEqual(status,'REJECTED')
        g=news.analyse(item('Change in Auditors','Resignation of statutory auditor'),now=NOW,verification='verified')
        self.assertEqual((g['label'],g['governance']),('Strongly Negative',True))

    def test_duplicate_or_repeat_news_is_not_new(self):
        prior=[dict(category='order_win',amount_crore=5000,published_at=(NOW-timedelta(days=3)).isoformat())]
        a=news.analyse(item(),revenue=10000,prior=prior,now=NOW,verification='verified')
        self.assertEqual(a['components']['novelty'],10)
        self.assertIn('Previously announced information (not new)',news_rules.momentum_screen(a,{})[1])

    def test_stale_news(self):
        a=news.analyse(item(published=NOW-timedelta(hours=60)),revenue=10000,now=NOW,verification='verified')
        self.assertTrue(any(r.startswith('Stale news') for r in news_rules.momentum_screen(a,{})[1]))

    def test_missing_materiality(self):
        a=news.analyse(item(headline='ABC Ltd has received a letter of award from NHAI'),revenue=10000,now=NOW,verification='verified')
        self.assertIsNone(a['materiality_ratio']);self.assertTrue(any('Event value not disclosed' in m for m in a['missing']))
        self.assertIn('Financial materiality unknown (event value or revenue missing)',news_rules.momentum_screen(a,{})[1])

    def test_failed_source_verification(self):
        a=news.analyse(item(),revenue=10000,now=NOW,verification='isin_mismatch')
        self.assertEqual(a['components']['credibility'],30)
        self.assertTrue(any('verification' in r for r in news_rules.momentum_screen(a,{})[1]))

    def test_already_priced_in(self):
        closes=[100+i*.5 for i in range(80)]
        ctx=news_rules.daily_context(daily(closes[:-1]+[closes[-2]*1.14],last_volume=600000),[100+i*.1 for i in range(80)])
        status,reasons,_=news_rules.momentum_confirm(analysis(),ctx,daily(closes),{})
        self.assertEqual(status,'REJECTED');self.assertTrue(any('Already priced in' in r for r in reasons))

    def test_confirmed_candidate(self):
        closes=[100*(1.004**i) for i in range(80)];closes[-1]=closes[-2]*1.03
        bars=daily(closes,volume=600000,last_volume=1500000);ctx=news_rules.daily_context(bars,[100+i*.05 for i in range(80)])
        status,reasons,plan=news_rules.momentum_confirm(analysis(),ctx,bars,{})
        self.assertEqual(status,'BUY_CANDIDATE',reasons);self.assertGreaterEqual(plan['risk_reward'],2)
        self.assertEqual(news_rules.momentum_confirm(analysis(),ctx,bars,{},sector_count=2)[0],'REJECTED')   # sector cap

    def test_resistance_between_3_6_and_4_atr_passes_default_rr(self):
        # 2-ATR stop / 4-ATR target cap RR at 2.0: under a 2.0 minimum any overhead resistance rejected the setup.
        closes=[100*(1.004**i) for i in range(80)];closes[-1]=closes[-2]*1.03
        bars=daily(closes,volume=600000,last_volume=1500000);ctx=news_rules.daily_context(bars,[100+i*.05 for i in range(80)])
        bars[-30]['high']=ctx['close']+3.8*ctx['atr']
        status,reasons,plan=news_rules.momentum_confirm(analysis(),ctx,bars,{})
        self.assertEqual(status,'BUY_CANDIDATE',reasons);self.assertAlmostEqual(plan['risk_reward'],1.9)
        self.assertEqual(news_rules.momentum_confirm(analysis(),ctx,bars,{'min_rr':2.})[0],'REJECTED')
        bars[-30]['high']=ctx['close']+3.4*ctx['atr']                             # 1.7R: still too close
        self.assertEqual(news_rules.momentum_confirm(analysis(),ctx,bars,{})[0],'REJECTED')

    def test_news_session_is_next_weekday_after_close(self):
        self.assertEqual(news_session('2026-10-16T16:00:00+05:30'),'2026-10-19')
        self.assertEqual(news_session('2026-10-16T09:00:00+05:30'),'2026-10-16')

# ---- catalyst investor ----------------------------------------------------------------------------

def details(rg=20,pg=25,np_=500,rev=5000,eq=2500,debt=500,pbt=700,fc=50,period='Mar 2026'):
    return dict(summary=dict(revenue=rev,net_profit=np_,revenue_growth_yoy=rg,net_profit_growth_yoy=pg,revenue_period=period),
        latest=dict(equity=eq,total_borrowings=debt,total_lease_liabilities=0,profit_before_tax=pbt,finance_costs=fc,period=period,period_end='2026-03-31'))

class CatalystTests(unittest.TestCase):
    def ctx(self,vol=.25):return dict(close=110,dma200=100,turnover20_cr=20,vol60=vol,high252=115)
    def decide(self,d,cap,cat=None):
        p=news_rules.fundamental_profile(d,False,110,cap,600)
        r=news_rules.opportunity_score(cat or analysis(impact_score=80),p,self.ctx(),{})
        return news_rules.catalyst_decision(cat or analysis(impact_score=80),p,r,self.ctx(),{}),r,p

    def test_strong_catalyst_and_fundamentals_buys(self):
        (status,reasons),r,_=self.decide(details(),cap=10000)       # P/E 20
        self.assertEqual(status,'BUY_CANDIDATE',reasons);self.assertGreaterEqual(r['score'],65)

    def test_strong_catalyst_weak_fundamentals(self):
        (status,reasons),_,_=self.decide(details(rg=-5,pg=-20,np_=100,eq=2500),cap=3000)
        self.assertEqual(status,'REJECTED');self.assertTrue(any('ROE' in x or 'deteriorating' in x for x in reasons))

    def test_strong_fundamentals_expensive_valuation(self):
        (status,reasons),_,_=self.decide(details(),cap=50000)        # P/E 100
        self.assertEqual(status,'REJECTED');self.assertTrue(any(x.startswith('Expensive') for x in reasons))

    def test_debt_reduction_event_is_a_catalyst(self):
        a=news.analyse(item('Updates','ABC Ltd has prepaid term loans of Rs 800 crore and is now debt-free'),revenue=5000,now=NOW,verification='verified')
        self.assertEqual(a['category'],'debt_reduction');self.assertTrue(a['catalyst_eligible']);self.assertGreater(a['direction'],0)

    def test_earnings_deterioration_and_thesis_invalidation(self):
        p=news_rules.fundamental_profile(details(rg=-8,pg=-30,period='Mar 2027'),False,110,10000,600)
        verdict=news_rules.catalyst_review(dict(average_cost=100,entry_period='Mar 2026'),p,None,105,None,120,{})
        self.assertEqual(verdict[0],'THESIS_FAILED')
        self.assertEqual(news_rules.catalyst_review(dict(average_cost=100,entry_period='Mar 2026'),None,None,105,'Strongly Negative auditor resignation',30,{})[0],'GOVERNANCE')
        # negative sentiment alone (no governance flag, thesis intact) never sells
        p=news_rules.fundamental_profile(details(),False,110,10000,600)
        self.assertIsNone(news_rules.catalyst_review(dict(average_cost=100,entry_period='Mar 2026'),p,news_rules.opportunity_score(analysis(),p,self.ctx(),{}),105,None,30,{}))

    def test_missing_fundamentals_are_flagged_not_invented(self):
        p=news_rules.fundamental_profile({},False,110,None,None)
        self.assertEqual(p['critical_gaps'],['revenue','net profit','equity']);self.assertIsNone(p['roe'])
        self.assertIn('promoter holding and pledging (no data source connected)',p['missing'])

# ---- live engine integration ----------------------------------------------------------------------

class FakeNews:
    def __init__(self,rows):self.rows=rows;self.calls=0
    def fetch(self,start,end):self.calls+=1;return self.rows
    def attachment_text(self,url):return None

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'s.sqlite3';init(self.path);init_paper(self.path)
        self.clock=datetime(2026,10,16,10,10,30,tzinfo=IST).astimezone(timezone.utc)
        self.auto=AutoTrading(self.path)
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config')
            for n in cfg:cfg[n]['enabled']=False
            for n in ('news_momentum','news_catalyst','ema_price_action'):cfg[n].update(enabled=n=='ema_price_action',buy_universe='all')
            cfg['ema_price_action']['params']=dict(VALID)
            set_state(db,'auto_config',cfg);set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            set_state(db,'signal_ema_checkpoint',{'last_success':self.clock.isoformat()})
            db.execute("INSERT INTO instruments VALUES('ABC','NSE_EQ|INE000A01011','INE000A01011','ABC Ltd','Industrials')")
            db.executemany('INSERT INTO sb_intraday VALUES(?,?,?,?,?,?,?,?)',[('ABC',5,b['ts'],b['open'],b['high'],b['low'],b['close'],b['volume']) for b in BARS[:TRIGGER+1]])
        self.quote(1003.)
        seed_quality(self.path,'ABC',self.clock)
        self.depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.depth.start();self.addCleanup(self.depth.stop)
        self.runner=SignalRunner(self.path,provider=FakeNews([]),candles=object())
    def tearDown(self):self.temp.cleanup()
    def test_tick_survives_when_recording_a_job_error_also_fails(self):
        import sqlite3
        with patch.object(self.runner,'poll_news',side_effect=sqlite3.OperationalError('unable to open database file')),\
             patch.object(self.runner,'status',side_effect=sqlite3.OperationalError('unable to open database file')),patch('sys.stderr'):
            self.runner.tick(self.clock)                                                    # must not raise and kill the runner thread
        self.assertTrue(self.runner.lock.acquire(blocking=False));self.runner.lock.release()
    def quote(self,price,clock=None):
        clock=clock or self.clock
        with connect(self.path) as db:
            db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?,?,?,?)',('ABC',price,None,int(clock.timestamp()*1000),clock.isoformat(),'test'))
            db.execute('UPDATE quality SET checked_at=?',(clock.isoformat(),))
            set_state(db,'signal_ema_checkpoint',{'last_success':clock.isoformat()})
    def signal(self):
        with connect(self.path) as db:return dict(db.execute("SELECT * FROM sb_signals WHERE portfolio='ema_price_action'").fetchone())
    def held(self,name='ema_price_action'):
        with connect(self.path) as db:return (db.execute('SELECT quantity FROM positions WHERE portfolio=? AND symbol=?',(name,'ABC')).fetchone() or [0])[0]
    def state(self):
        with connect(self.path) as db:
            r=db.execute("SELECT * FROM sb_positions WHERE portfolio='ema_price_action'").fetchone()
            return json.loads(r['state']) if r else None
    def arm(self):
        self.runner.ema_signals(['ABC'],self.clock,{**ema_rules.DEFAULTS,**VALID})
        self.assertEqual(self.signal()['status'],'ENTRY_PENDING')

    def test_duplicate_scheduler_execution_creates_one_signal(self):
        self.arm();self.runner.ema_signals(['ABC'],self.clock,{**ema_rules.DEFAULTS,**VALID})
        with connect(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM sb_signals').fetchone()[0],1)

    def test_breakout_not_triggered_below_trigger(self):
        self.arm();self.quote(1001.);self.auto.cycle(self.clock,{})
        self.assertEqual(self.held(),0);self.assertEqual(self.signal()['status'],'ENTRY_PENDING')

    def test_entry_fill_arms_oco_and_retry_does_not_duplicate(self):
        self.arm();self.auto.cycle(self.clock,{})
        qty=self.held();self.assertGreater(qty,0)
        s=self.signal();self.assertEqual(s['status'],'ENTRY_FILLED')
        st=self.state();self.assertEqual(st['stage'],'INITIAL_EXITS_ARMED');self.assertEqual(st['qty'],qty)
        self.assertEqual(st['stop'],s['initial_stop']);self.assertEqual(st['target'],s['initial_target'])
        self.auto.cycle(self.clock+timedelta(seconds=2),{})        # a retry/second scan never buys again
        self.assertEqual(self.held(),qty)
        with connect(self.path) as db:
            t=db.execute("SELECT execution FROM trades WHERE portfolio='ema_price_action'").fetchone()[0]
            self.assertIn('Intraday',json.loads(t)['charge_basis'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM positions WHERE portfolio!='ema_price_action' AND quantity>0").fetchone()[0],0)   # no cross-basket position

    def test_stop_loss_exit(self):
        self.arm();self.auto.cycle(self.clock,{});stop=self.state()['stop']
        later=self.clock+timedelta(minutes=1);self.quote(stop-1,later);self.auto.cycle(later,{})
        self.assertEqual(self.held(),0);self.assertIsNone(self.state())
        with connect(self.path) as db:
            self.assertIn('Stop Loss',db.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0])
            self.assertTrue(db.execute("SELECT 1 FROM sb_transitions WHERE to_stage='CLOSED'").fetchone())

    def test_target_hit(self):
        self.arm();self.auto.cycle(self.clock,{});st=self.state()
        with connect(self.path) as db:
            st['target']=st['be_trigger']-.5;db.execute("UPDATE sb_positions SET state=?",(json.dumps(st),))
        later=self.clock+timedelta(minutes=1);self.quote(st['target']+.3,later);self.auto.cycle(later,{})
        self.assertGreater(self.held(),0)                       # a limit exit never fills below its level (bid is 0.1% under)
        with connect(self.path) as db:self.assertIn('not reachable',db.execute("SELECT reason FROM auto_events WHERE kind='blocked'").fetchone()[0])
        later+=timedelta(seconds=20);self.quote(st['target']*1.002,later);self.auto.cycle(later,{})
        self.assertEqual(self.held(),0)

    def test_breakeven_partial_and_trailing_live(self):
        self.arm();self.auto.cycle(self.clock,{});st=self.state();qty=st['qty']
        with connect(self.path) as db:st['target']=st['partial_trigger']+5;db.execute('UPDATE sb_positions SET state=?',(json.dumps(st),))
        t1=self.clock+timedelta(minutes=1);self.quote(st['be_trigger']+.1,t1);self.auto.cycle(t1,{})
        st2=self.state();self.assertEqual(st2['stage'],'BREAKEVEN_ACTIVE');self.assertGreaterEqual(st2['stop'],st['entry'])
        t2=t1+timedelta(minutes=1);self.quote(st['partial_trigger']*1.002,t2);self.auto.cycle(t2,{})
        st3=self.state();self.assertEqual(st3['stage'],'EMA_TRAILING_ACTIVE');self.assertEqual(self.held(),qty-qty//2)
        self.assertFalse(st3['target_active'])

    def test_clock_square_off_cancels_and_closes(self):
        self.arm();self.auto.cycle(self.clock,{})
        with connect(self.path) as db:
            db.execute("INSERT INTO sb_signals(portfolio,strategy_version,signal_key,symbol,created_at,session,status,updated_at,details) VALUES('ema_price_action','v','x','ABC',?,?,'ENTRY_PENDING',?,'{}')",(self.clock.isoformat(),'2026-10-16',self.clock.isoformat()))
        close=datetime(2026,10,16,15,12,5,tzinfo=IST).astimezone(timezone.utc);self.quote(1003.,close);self.auto.cycle(close,{})
        self.assertEqual(self.held(),0)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM sb_signals WHERE signal_key='x'").fetchone()[0],'CANCELLED')
            self.assertIn('square-off',db.execute("SELECT reason FROM trades WHERE side='SELL'").fetchone()[0])

    def test_square_off_without_price_is_not_claimed(self):
        self.arm();self.auto.cycle(self.clock,{})
        close=datetime(2026,10,16,15,12,5,tzinfo=IST).astimezone(timezone.utc)   # quote is now hours old
        with connect(self.path) as db:set_state(db,'signal_ema_checkpoint',{'last_success':close.isoformat()})
        self.auto.cycle(close,{})
        self.assertGreater(self.held(),0)
        with connect(self.path) as db:self.assertTrue(db.execute("SELECT 1 FROM sb_transitions WHERE to_stage='SQUARE_OFF_BLOCKED'").fetchone())

    def test_market_holiday_or_closed_feed_does_nothing(self):
        self.arm()
        with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'CLOSED'}})
        self.auto.cycle(self.clock,{});self.assertEqual(self.held(),0)
        self.assertEqual(self.auto.snapshot()['status']['ema_price_action']['state'],'waiting')
        self.runner.ema_cycle(self.clock)           # the candle job also stays idle (would fail on the fake reader otherwise)

    def test_pausing_one_basket_leaves_others_and_no_broker_calls(self):
        self.arm();self.auto.configure('news_momentum',False)
        with patch('requests.post') as post:self.auto.cycle(self.clock,{})
        post.assert_not_called();self.assertGreater(self.held(),0)
        self.auto.configure('ema_price_action',False)
        self.assertFalse(self.auto.snapshot()['config']['ema_price_action']['enabled'])

    def test_capital_is_independent_and_fixed_after_trading(self):
        update_config(self.path,dict(portfolio='news_catalyst',capital=250000))
        with connect(self.path) as db:
            caps=dict(db.execute("SELECT name,initial_cash FROM portfolios WHERE name IN ('news_catalyst','news_momentum','ema_price_action')").fetchall())
        self.assertEqual(caps,{'news_catalyst':250000,'news_momentum':100000,'ema_price_action':100000})
        self.arm();self.auto.cycle(self.clock,{})
        with self.assertRaisesRegex(ValueError,'already has paper trades'):update_config(self.path,dict(portfolio='ema_price_action',capital=50000))
        with self.assertRaisesRegex(ValueError,'between'):update_config(self.path,dict(portfolio='ema_price_action',params={'min_rr':3}))

    def test_news_poll_stores_dedupes_and_verifies(self):
        rows=[{'symbol':'ABC','sm_isin':'INE000A01011','sm_name':'ABC','desc':'Bagging/Receiving of orders/contracts','attchmntText':'ABC Ltd has received an order worth Rs 900 crore',
               'an_dt':'16-Oct-2026 09:30:00','seq_id':'11'},
              {'symbol':'ABC','sm_isin':'INE000A01011','sm_name':'ABC','desc':'Bagging/Receiving of orders/contracts','attchmntText':'ABC Ltd. has received an order worth Rs 900 crore',
               'an_dt':'16-Oct-2026 09:31:00','seq_id':'12'},
              {'symbol':'ABC','sm_isin':'INE999Z99999','sm_name':'ABC','desc':'Press Release','attchmntText':'Spoofed order','an_dt':'16-Oct-2026 09:32:00','seq_id':'13'}]
        with connect(self.path) as db:
            cfg=get_state(db,'auto_config');cfg['news_momentum']['enabled']=True;set_state(db,'auto_config',cfg)
        self.runner.provider=FakeNews(rows);self.runner.poll_news(self.clock,force=True);self.runner.poll_news(self.clock,force=True)
        with connect(self.path) as db:
            got=db.execute('SELECT source_id,duplicate_of,verification FROM sb_news ORDER BY source_id').fetchall()
            self.assertEqual([tuple(r) for r in got],[('11',None,'verified'),('12',1,'verified'),('13',None,'isin_mismatch')])
            sig=db.execute("SELECT status FROM sb_signals WHERE portfolio='news_momentum'").fetchall()
        self.assertEqual(len(sig),1)                   # only the verified original gets a decision
        payload=report(self.path,self.clock)
        self.assertEqual([b['name'] for b in payload['baskets']],['news_momentum','news_catalyst','ema_price_action','vinay_intraday','liquidity_sweep_reversal','order_flow_reversal','volatility_compression_breakout','orb_volume_breakout','sector_relative_strength'])
        json.dumps(payload,allow_nan=False)


class OutsideUniverse(unittest.TestCase):
    def test_candidates_outside_the_buy_universe_are_rejected(self):
        from unittest.mock import patch
        from marketdesk.signal_baskets import runner
        with patch.object(runner,'allowed_symbols',return_value={'KPIL'}):
            self.assertEqual(runner.outside_universe(None,'news_catalyst','KPIL',None,'BUY_CANDIDATE',['ok']),('BUY_CANDIDATE',['ok']))
            self.assertEqual(runner.outside_universe(None,'news_catalyst','EPL',None,'BUY_CANDIDATE',['ok']),('REJECTED',['Outside the Nifty 500 buy universe','ok']))
        with patch.object(runner,'allowed_symbols',side_effect=ValueError('membership stale')):
            self.assertEqual(runner.outside_universe(None,'news_momentum','EPL',None,'WATCHLIST',[]),('WATCHLIST',[]))

if __name__=='__main__':unittest.main()
