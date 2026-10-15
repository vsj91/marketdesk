import copy
import unittest
from datetime import datetime
from marketdesk.autotrading import IST
from marketdesk.replay import replay_entry, signal_from, replay
from marketdesk.trader_review import analyze
from test_replay import fixture


class TraderReviewTests(unittest.TestCase):
    def test_research_cap_rejects_extension_and_gap_without_changing_baseline(self):
        data=fixture();fund=data['funds']['TEST'][0]
        signal=signal_from(data['histories']['TEST'][:-5],fund)
        clock=datetime(2026,10,5,9,20,tzinfo=IST)
        self.assertIsNotNone(replay_entry('trader',signal,102,clock,'no_chase_v1'))
        for close,high,price in [(100,103,103.2),(102,101,103.1)]:
            s=dict(signal,close=close,high20=high)
            self.assertIsNotNone(replay_entry('trader',s,price,clock,'baseline'))
            self.assertIsNone(replay_entry('trader',s,price,clock,'no_chase_v1'))
            self.assertEqual(replay_entry('investor',s,price,clock,'baseline'),replay_entry('investor',s,price,clock,'no_chase_v1'))

    def test_invalid_variant_fails_and_default_remains_baseline(self):
        with self.assertRaises(ValueError):replay(fixture(),trader_variant='unknown')
        self.assertEqual(replay(fixture())['trader_variant'],'baseline')

    def test_diagnostic_exits_reconcile_with_realized_pnl(self):
        data=fixture();report=replay(data);before=copy.deepcopy(report)
        result=analyze(report,data)
        self.assertEqual(report,before)
        self.assertAlmostEqual(sum(t['net_pnl'] for t in result['exits']),report['portfolios']['trader']['realized_pnl'],places=2)
        self.assertEqual(result['wins']+result['losses'],result['exit_fills'])
        self.assertTrue(all(t['first_range_cross'] is not None for t in result['exits']))
