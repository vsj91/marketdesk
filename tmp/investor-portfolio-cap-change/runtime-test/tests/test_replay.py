import copy
import json
import tempfile
import unittest
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from marketdesk.replay import load_snapshot, replay, render_report
from marketdesk.storage import init,connect


def fixture():
    days=[];day=date(2026,10,9)
    while len(days)<265:
        if day.weekday()<5:days.append(day.isoformat())
        day-=timedelta(days=1)
    days.reverse()
    rows=[]
    for i,d in enumerate(days):
        close=90 if i<200 else 100
        volume=100_000 if i<250 else 200_000
        row={'symbol':'TEST','ts':d+'T00:00:00+05:30','open':close,'high':close+1,'low':close-1,'close':close,'volume':volume}
        if i>=260:
            row.update(open=109 if i==261 else 102,close=103,high=110,low=101,volume=300_000)
        rows.append(row)
    fund={'status':'COMPLETE','latest':{'period':'Mar 2026','statement_type':'consolidated','units_in':'crore','profit_before_tax':100,'finance_costs':10,'equity':1000,'total_borrowings':100,'total_lease_liabilities':10,'cash_and_cash_equivalents':50},'summary':{'revenue_period':'Mar 2026','net_profit_period':'Mar 2026','statement_type':'consolidated','units':'crore','frequency':'yearly','revenue_growth_yoy':10,'net_profit_growth_yoy':20}}
    stamp='2026-10-01T00:00:00+00:00'
    return {'symbols':['TEST'],'histories':{'TEST':rows},'sessions':days,'funds':{'TEST':(fund,stamp)},
            'evidence':{'TEST':{k:stamp for k in ('income_statement','balance_sheet','cash_flow')}},'bad':set(),'input_sha256':'fixture'}


class ReplayTests(unittest.TestCase):
    def test_calendar_range_keeps_warmup_and_excludes_outside_fills(self):
        data=fixture();data['captured_at']='2026-10-12T00:00:00+00:00'
        result=replay(data,start='2026-10-07',end='2026-10-09',fundamentals_mode='current')
        self.assertEqual(result['sessions'],['2026-10-07','2026-10-08','2026-10-09'])
        self.assertTrue(result['portfolios']['investor']['trades'])
        self.assertEqual(result['coverage'][0]['price_eligible'],1)
        for account in result['portfolios'].values():
            for trade in account['trades']:
                self.assertGreaterEqual(trade['session'],'2026-10-07')
                self.assertLess(trade['indicator_through'],trade['session'])
        self.assertIn('Requested calendar window: 2026-10-07 to 2026-10-09',render_report(result))

    def test_invalid_or_empty_calendar_range_is_rejected(self):
        for start,end in [('2026-10-10','2026-10-09'),('2026-10-10','2026-10-11'),('bad','2026-10-09')]:
            with self.assertRaises(ValueError):replay(fixture(),start=start,end=end)

    def test_current_mode_uses_later_funds_without_changing_historical_default(self):
        data=fixture();data['captured_at']='2026-10-12T00:00:00+00:00'
        fund,_=data['funds']['TEST'];stamp='2026-10-10T00:00:00+00:00'
        data['funds']['TEST']=(fund,stamp)
        data['evidence']['TEST']={k:stamp for k in data['evidence']['TEST']}
        original=copy.deepcopy(data)
        historical=replay(data);current=replay(data,fundamentals_mode='current')
        self.assertFalse(historical['portfolios']['investor']['trades'])
        self.assertTrue(current['portfolios']['investor']['trades'])
        self.assertEqual(data,original)
        self.assertEqual(historical['portfolios']['trader'],current['portfolios']['trader'])
        for t in current['portfolios']['investor']['trades']:
            self.assertEqual(t['fundamentals_mode'],'current')
            self.assertEqual(t['fundamentals_observed_at'],stamp)
            self.assertLess(t['indicator_through'],t['session'])
        self.assertIn('hypothetical paper testcase, not historical performance',render_report(current))

    def test_current_mode_preserves_quality_and_capture_time_gates(self):
        for problem in ('incomplete','conflict','stale','future','evidence'):
            data=fixture();data['captured_at']='2026-10-12T00:00:00+00:00'
            fund,stamp=data['funds']['TEST']
            if problem=='incomplete':fund['latest']['equity']=None
            elif problem=='conflict':fund['source_conflicts']=['mismatch']
            elif problem=='stale':data['funds']['TEST']=(fund,'2026-08-01T00:00:00+00:00')
            elif problem=='future':data['funds']['TEST']=(fund,'2026-10-13T00:00:00+00:00')
            else:data['evidence']['TEST']['income_statement']='2026-08-01T00:00:00+00:00'
            self.assertFalse(replay(data,fundamentals_mode='current')['portfolios']['investor']['trades'],problem)

    def test_current_mode_still_excludes_future_price_bars(self):
        a=fixture();a['captured_at']='2026-10-12T00:00:00+00:00';b=copy.deepcopy(a)
        for r in b['histories']['TEST'][-3:]:r.update(open=800,high=900,low=700,close=850,volume=1)
        before=replay(a,fundamentals_mode='current');after=replay(b,fundamentals_mode='current')
        cutoff=before['sessions'][2]
        select=lambda r:[t for t in r['portfolios']['investor']['trades'] if t['session']<cutoff]
        self.assertTrue(select(before));self.assertEqual(select(before),select(after))

    def test_buys_exits_fees_and_limits(self):
        result=replay(fixture())
        trader=result['portfolios']['trader']
        self.assertEqual([t['side'] for t in trader['trades'][:2]],['BUY','SELL'])
        self.assertEqual(trader['trades'][1]['reason'],'6% profit target')
        self.assertGreater(trader['fees'],0)
        for p,a in result['portfolios'].items():
            self.assertGreaterEqual(a['cash'],0)
            self.assertAlmostEqual(a['net_pnl'],a['realized_pnl']+a['unrealized_pnl'],places=2)
            for t in a['trades']:
                self.assertLess(t['indicator_through'],t['session'])
                if t['side']=='BUY':self.assertLessEqual(t['quantity']*t['price']+t['fee'],100000.01)
        firstday=result['sessions'][0]
        fills=[t for a in result['portfolios'].values() for t in a['trades'] if t['session']==firstday]
        self.assertLessEqual(sum(t['quantity'] for t in fills),1500)
        self.assertTrue(any(t['unfilled']>0 for t in fills))

    def test_future_bars_cannot_change_earlier_fills(self):
        original=fixture();changed=copy.deepcopy(original)
        for row in changed['histories']['TEST'][-3:]:row.update(open=800,high=900,low=700,close=850,volume=1)
        before=replay(original);after=replay(changed)
        cutoff=before['sessions'][2]
        for p in before['portfolios']:
            select=lambda r:[t for t in r['portfolios'][p]['trades'] if t['session']<cutoff]
            self.assertEqual(select(before),select(after))

    def test_same_day_range_and_volume_not_used_to_choose_first_fill(self):
        original=fixture();changed=copy.deepcopy(original)
        changed['histories']['TEST'][-5].update(high=10000,low=1,close=999,volume=1)
        a=replay(original);b=replay(changed)
        for p in a['portfolios']:
            first=lambda r:[t for t in r['portfolios'][p]['trades'] if t['session']==r['sessions'][0]]
            self.assertEqual(first(a),first(b))

    def test_later_fundamentals_block_investor_without_harming_trader(self):
        data=fixture();fund,_=data['funds']['TEST'];data['funds']['TEST']=(fund,'2026-10-10T00:00:00+00:00')
        result=replay(data)
        self.assertTrue(result['portfolios']['trader']['trades'])
        self.assertEqual(result['portfolios']['investor']['trades'],[])
        self.assertIn('UNTESTABLE',result['portfolios']['investor']['status'])
        self.assertTrue(all(c['investor_evidence_eligible']==0 for c in result['coverage']))

    def test_future_underlying_evidence_also_blocks_investor(self):
        data=fixture();data['evidence']['TEST']['balance_sheet']='2026-10-10T00:00:00+00:00'
        self.assertFalse(replay(data)['portfolios']['investor']['trades'])

    def test_source_database_opened_without_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.sqlite3';init(path);data=fixture()
            with connect(path) as db:
                db.execute("INSERT INTO instruments VALUES('TEST','NSE_EQ|TEST','TEST','Test','Test')")
                db.executemany('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',[(r['symbol'],r['ts'],r['open'],r['high'],r['low'],r['close'],r['volume'],'fixture','2026-10-10T00:00:00+00:00') for r in data['histories']['TEST']])
                db.execute("INSERT INTO state VALUES('sentinel','unchanged')")
            with connect(path) as db:before='\n'.join(db.iterdump())
            replay(load_snapshot(path))
            with connect(path) as db:after='\n'.join(db.iterdump())
            self.assertEqual(before,after)
