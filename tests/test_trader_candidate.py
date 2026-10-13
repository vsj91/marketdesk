import copy,unittest
from marketdesk.trader_candidate import ConfirmedBreakout,NAME
from marketdesk.intraday_replay import simulate
from test_intraday import sample,at

class CandidateTests(unittest.TestCase):
    def policy(self):
        p=ConfirmedBreakout();p.start_day(0,['A']);return p
    def bar(self,price=101,high=None):
        return {'A':{'open':price,'high':high or price,'low':price,'close':price,'volume':100}}
    def observe(self,p,n,price=101,high=None):p.observe(n,self.bar(price,high),{'A':{'high20':100}})
    def confirmed(self,p):
        for n in range(15):self.observe(p,n,101,102)
        for n in range(15,18):self.observe(p,n,103)
    def test_waits_for_all_three_completed_minutes(self):
        p=self.policy()
        for n in range(15):self.observe(p,n,101,102)
        for n in range(15,17):self.observe(p,n,103);self.assertFalse(p.permits('A',103))
        self.observe(p,17,103);self.assertTrue(p.permits('A',103))
        self.assertFalse(p.permits('A',102))
    def test_missing_opening_minute_blocks_entire_session(self):
        p=self.policy()
        for n in range(15):p.observe(n,{} if n==5 else self.bar(101,102),{'A':{'high20':100}})
        for n in range(15,25):self.observe(p,n,103)
        self.assertFalse(p.permits('A',103))
    def test_gap_resets_confirmation(self):
        p=self.policy();self.confirmed(p);p.observe(18,{}, {'A':{'high20':100}})
        self.observe(p,19,103);self.observe(p,20,103);self.assertFalse(p.permits('A',103))
        self.observe(p,21,103);self.assertTrue(p.permits('A',103))
    def test_cooldown_blocks_three_following_sessions(self):
        p=self.policy();self.confirmed(p);p.exited('A',-1)
        for day in range(1,4):p.start_day(day,['A']);self.confirmed(p);self.assertFalse(p.permits('A',103))
        p.start_day(4,['A']);self.confirmed(p);self.assertTrue(p.permits('A',103))
    def test_profit_does_not_cool_down(self):
        p=self.policy();self.confirmed(p);p.exited('A',1);self.assertTrue(p.permits('A',103))
    def test_candidate_first_fill_is_0933_and_uses_previous_minutes(self):
        plan,rows=sample()
        for stamp,values in rows.items():
            if stamp>=at('09:30'):values['TEST'].update(open=104,high=104,low=104,close=104)
        a=simulate(plan,rows,trader_variant=NAME)
        self.assertEqual(a['trades'][0]['timestamp'],at('09:33'))
        other=copy.deepcopy(rows);other[at('09:33')]['TEST'].update(high=150,low=80,close=140)
        b=simulate(plan,other,trader_variant=NAME)
        self.assertEqual(a['trades'][0],b['trades'][0])
    def test_gap_back_below_confirmation_rejects_fill(self):
        plan,rows=sample()
        for stamp,values in rows.items():
            if stamp>=at('09:30'):values['TEST'].update(open=104,high=104,low=104,close=104)
        rows[at('09:33')]['TEST'].update(open=102,low=102)
        a=simulate(plan,rows,trader_variant=NAME)
        self.assertEqual(a['trades'][0]['timestamp'],at('09:34'))
        self.assertEqual(a['events']['confirmation_changed_before_fill'],1)

class BreadthTests(unittest.TestCase):
    def signals(self,bullish=500,total=500):
        return {str(i):{'close':110 if i<bullish else 80,'dma50':100,'dma200':90} for i in range(total)}
    def test_requires_large_universe_and_strict_majority(self):
        from marketdesk.trader_regime import breadth
        self.assertFalse(breadth(self.signals(499,499))['permits_buys'])
        self.assertFalse(breadth(self.signals(250))['permits_buys'])
        self.assertTrue(breadth(self.signals(251))['permits_buys'])
        self.assertFalse(breadth({})['permits_buys'])
    def test_weak_market_blocks_buys_but_preserves_exits(self):
        from marketdesk.trader_regime import NAME as REGIME
        plan,rows=sample();day=plan['days'][0]
        plan['frames'][day]['signals'].update(self.signals())
        plan['days'].append('2026-10-06');plan['frames']['2026-10-06']=copy.deepcopy(plan['frames'][day])
        plan['frames']['2026-10-06']['signals'].update(self.signals(0))
        for stamp,values in list(rows.items()):
            new_stamp=stamp.replace(day,'2026-10-06');rows[new_stamp]=copy.deepcopy(values)
            rows[new_stamp]['TEST'].update(ts=new_stamp,open=98,high=98,low=98,close=98)
        r=simulate(plan,rows,trader_variant=REGIME)
        self.assertEqual([t['side'] for t in r['trades']],['BUY','SELL'])
        self.assertEqual(r['trades'][1]['timestamp'],'2026-10-06T09:16:00+05:30')
        self.assertEqual(r['events']['breadth_blocked_sessions'],1)
    def test_invalid_values_are_not_counted_as_breadth_evidence(self):
        from marketdesk.trader_regime import breadth
        s=self.signals();s['0']['dma50']=float('nan');s['1']['close']=True
        self.assertEqual(breadth(s)['stocks'],498)
        self.assertFalse(breadth(s)['permits_buys'])
