import copy,unittest
from datetime import datetime,timedelta
from marketdesk.autotrading import IST
from marketdesk.intraday_data import validate_bars
from marketdesk.intraday_replay import simulate


def sample():
    day='2026-10-05';signal={'as_of':'2026-10-01T00:00:00+05:30','close':100,'high20':101,'dma50':99,'dma200':90,'volume_ratio':1.5,'turnover20':20_000_000,'average_volume20':1_000_000,'fundamentals':{}}
    plan={'days':[day],'frames':{day:{'signals':{'TEST':signal},'eligible':['TEST']}}}
    rows={}
    for n in range(375):
        stamp=(datetime(2026,10,5,9,15,tzinfo=IST)+timedelta(minutes=n)).isoformat()
        rows[stamp]={'TEST':{'ts':stamp,'open':102,'high':102.5,'low':101.5,'close':102,'volume':100_000}}
    return plan,rows


def at(minute):return '2026-10-05T'+minute+':00+05:30'


class IntradayTests(unittest.TestCase):
    def test_partial_exits_group_into_one_closed_position(self):
        from marketdesk.intraday_replay import closed_positions
        trades=[{'symbol':'A','side':'BUY','timestamp':'entry','quantity':10},
                {'symbol':'A','side':'SELL','timestamp':'partial','quantity':4,'realized_pnl':10,'reason':'target'},
                {'symbol':'A','side':'SELL','timestamp':'exit','quantity':6,'realized_pnl':-20,'reason':'stop'}]
        self.assertEqual(closed_positions(trades[:-1]),[])
        result=closed_positions(trades)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['pnl'],-10)

    def test_timestamp_validation_and_duplicate_conflicts(self):
        b=['2026-10-05T09:15:00+05:30',100,101,99,100,10,0]
        self.assertEqual(len(validate_bars({'status':'success','data':{'candles':[b,b]}},'2026-10-01','2026-10-09')),1)
        for bad in [b[:1]+[100,98,99,100,10],['2026-10-05T09:15:00',100,101,99,100,10],b[:5]+[-1]]:
            with self.assertRaises(ValueError):validate_bars({'status':'success','data':{'candles':[bad]}},'2026-10-01','2026-10-09')
        other=list(b);other[4]=100.5
        with self.assertRaises(ValueError):validate_bars({'status':'success','data':{'candles':[b,other]}},'2026-10-01','2026-10-09')

    def test_signal_close_fills_next_open_at_0920_and_cash_reconciles(self):
        plan,rows=sample();r=simulate(plan,rows);t=r['trades'][0]
        self.assertEqual(t['observed_bar_start'],at('09:19'));self.assertEqual(t['timestamp'],at('09:20'))
        self.assertGreater(t['price'],102);self.assertEqual(t['side'],'BUY')
        self.assertEqual(len(r['trades']),1)
        self.assertAlmostEqual(r['net_pnl'],r['realized_pnl']+r['unrealized_pnl'],places=2)
        self.assertLessEqual(t['quantity']*t['price']+t['fee'],100000.01)

    def test_gap_exit_uses_next_open_not_stop_threshold(self):
        plan,rows=sample();rows[at('09:20')]['TEST'].update(low=97,close=98)
        rows[at('09:21')]['TEST'].update(open=95,low=95)
        r=simulate(plan,rows);sell=r['trades'][1]
        self.assertEqual(sell['side'],'SELL');self.assertEqual(sell['timestamp'],at('09:21'))
        self.assertAlmostEqual(sell['price'],95*.999)
        self.assertEqual(len(r['trades']),2) # no same-session re-entry

    def test_both_thresholds_are_flagged_without_fabricated_threshold_fill(self):
        plan,rows=sample();rows[at('09:20')]['TEST'].update(high=110,low=90,close=102)
        a=simulate(plan,rows);b=simulate(plan,rows,'range_touch')
        self.assertTrue(a['ambiguous_minutes']);self.assertEqual(len(a['trades']),1)
        self.assertEqual(b['trades'][1]['timestamp'],at('09:21'))
        self.assertIn('both stop and target',b['trades'][1]['reason'])
        self.assertAlmostEqual(b['trades'][1]['price'],102*.999)

    def test_fill_capacity_uses_prior_volume_and_cancels_remainder(self):
        plan,rows=sample();rows[at('09:19')]['TEST']['volume']=100
        r=simulate(plan,rows);t=r['trades'][0]
        self.assertEqual(t['quantity'],10);self.assertGreater(t['unfilled'],0)
        self.assertEqual(len(r['trades']),1)

    def test_missing_execution_minute_cancels_instead_of_forward_filling(self):
        plan,rows=sample();del rows[at('09:20')]
        r=simulate(plan,rows)
        self.assertEqual(r['trades'][0]['timestamp'],at('09:22'))
        self.assertGreater(r['events']['cancelled_missing_execution_minute'],0)

    def test_current_bar_high_low_close_cannot_change_its_open_fill(self):
        plan,rows=sample();changed=copy.deepcopy(rows)
        changed[at('09:20')]['TEST'].update(high=150,low=70,close=130,volume=1_000_000)
        a=simulate(plan,rows);b=simulate(plan,changed)
        self.assertEqual(a['trades'][0],b['trades'][0])

    def test_daily_loss_halt_survives_recovery(self):
        plan,rows=sample()
        # A severe post-entry gap causes >2% portfolio loss, then the stock recovers.
        rows[at('09:20')]['TEST'].update(low=76,close=76)
        rows[at('09:21')]['TEST'].update(open=76,low=76)
        r=simulate(plan,rows)
        self.assertTrue(r['daily'][0]['buy_halt']);self.assertEqual(len(r['trades']),2)

    def test_no_entries_at_1500_and_no_overnight_synthetic_fill(self):
        plan,rows=sample()
        for stamp,values in rows.items():
            if stamp<at('14:59'):values['TEST'].update(open=100,high=100,low=100,close=100)
        self.assertEqual(simulate(plan,rows)['trades'],[])

    def test_exits_continue_after_entry_window(self):
        plan,rows=sample();rows[at('15:10')]['TEST'].update(low=97,close=98)
        result=simulate(plan,rows)
        self.assertEqual(result['trades'][1]['timestamp'],at('15:11'))
        self.assertEqual(result['trades'][1]['side'],'SELL')

    def test_partial_exits_charge_dp_once_and_reconcile(self):
        from marketdesk.execution import charges,money
        plan,rows=sample()
        for stamp,values in rows.items():
            if at('09:20')<=stamp<=at('09:22'):
                values['TEST'].update(low=97,close=98,volume=100)
        result=simulate(plan,rows);sells=[t for t in result['trades'] if t['side']=='SELL']
        self.assertEqual([t['quantity'] for t in sells],[10,10,10])
        for i,t in enumerate(sells):
            self.assertEqual(t['fee'],charges(money(t['quantity']*t['price']),'SELL',dp=i==0)['total'])
        self.assertAlmostEqual(result['net_pnl'],result['realized_pnl']+result['unrealized_pnl'],places=2)

    def test_zero_trade_minutes_cannot_fill(self):
        plan,rows=sample();rows[at('09:20')]['TEST']['volume']=0
        result=simulate(plan,rows)
        self.assertEqual(result['trades'][0]['timestamp'],at('09:22'))

    def test_final_minute_exit_is_not_filled_outside_session(self):
        plan,rows=sample();rows[at('15:29')]['TEST'].update(low=97,close=98)
        result=simulate(plan,rows)
        self.assertEqual(len(result['trades']),1)
        self.assertEqual(result['events']['exit_signal_at_close_waits_next_session'],1)

    def test_overnight_gap_waits_for_first_completed_minute(self):
        plan,rows=sample()
        day='2026-10-06';plan['days'].append(day)
        plan['frames'][day]=copy.deepcopy(plan['frames']['2026-10-05'])
        plan['frames'][day]['signals']['TEST']['as_of']='2026-10-05T00:00:00+05:30'
        for stamp,values in list(rows.items()):
            new_stamp=stamp.replace('2026-10-05',day)
            rows[new_stamp]=copy.deepcopy(values)
            rows[new_stamp]['TEST'].update(ts=new_stamp,open=95,high=96,low=94,close=95)
        result=simulate(plan,rows)
        self.assertEqual(result['trades'][1]['timestamp'],day+'T09:16:00+05:30')
        self.assertAlmostEqual(result['trades'][1]['price'],95*.999)

    def test_bar_count_coverage_does_not_treat_zero_volume_as_missing(self):
        from marketdesk.intraday_replay import coverage
        plan,rows=sample();plan['keys']={'TEST':'NSE_EQ|TEST'}
        plan['frames'][plan['days'][0]]['daily']={'TEST':{'open':102,'high':102.5,'low':101.5}}
        rows[at('09:20')]['TEST']['volume']=0
        del rows[at('09:21')]
        result=coverage(plan,rows,{'TEST':{}})
        self.assertEqual(result['gaps'][0]['missing'],1)
        self.assertEqual(result['eligible_zero_volume_minutes'],1)
        self.assertEqual(result['daily_ohl_mismatches'],[])
