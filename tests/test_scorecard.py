import tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from marketdesk.scorecard import scorecard
from marketdesk.storage import init,connect,set_state
from marketdesk.paper import init_paper

class ScorecardTests(unittest.TestCase):
    def test_fresh_ledger_meets_only_the_breaker_and_never_the_order_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'s.db';init(path);init_paper(path)
            with connect(path) as db:
                sc=scorecard(db,datetime(2026,10,19,tzinfo=timezone.utc))
        self.assertFalse(sc['ready']);self.assertEqual(sc['met'],1)
        self.assertFalse(next(g for g in sc['shared'] if g['key']=='orders')['met'])
        self.assertEqual({k:len(v) for k,v in sc['baskets'].items()},{'trader':4,'investor':4})
    def test_price_gate_needs_enough_clean_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'s.db';init(path);init_paper(path)
            sessions={f'2026-09-{d:02d}':{'compared':497,'close_mismatches':0,'volume_mismatches':0} for d in range(1,21)}
            with connect(path) as db:
                set_state(db,'close_check',{'sessions':sessions});good=scorecard(db)
                sessions['2026-09-05']['close_mismatches']=2;set_state(db,'close_check',{'sessions':sessions});bad=scorecard(db)
        self.assertTrue(next(g for g in good['shared'] if g['key']=='prices')['met'])
        self.assertFalse(next(g for g in bad['shared'] if g['key']=='prices')['met'])
if __name__=='__main__':unittest.main()
