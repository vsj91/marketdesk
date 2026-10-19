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
        self.assertEqual({k:len(v) for k,v in sc['baskets'].items()},{'trader':4,'investor':4,'momentum':4,'lowvol':4,'smart_beta':4,'risk_parity':4,'stat_arb':4,'regime':4,'news_momentum':4,'news_catalyst':4,'ema_price_action':4})
    def test_price_gate_needs_enough_clean_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'s.db';init(path);init_paper(path)
            sessions={f'2026-09-{d:02d}':{'compared':497,'close_mismatches':0,'volume_mismatches':0} for d in range(1,21)}
            with connect(path) as db:
                set_state(db,'close_check',{'sessions':sessions});good=scorecard(db)
                sessions['2026-09-05']['close_mismatches']=2;set_state(db,'close_check',{'sessions':sessions});bad=scorecard(db)
        self.assertTrue(next(g for g in good['shared'] if g['key']=='prices')['met'])
        self.assertFalse(next(g for g in bad['shared'] if g['key']=='prices')['met'])
    def test_partial_trims_are_not_closed_trades(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'s.db';init(path);init_paper(path)
            rows=[('a','investor','AAA','BUY',10,'2026-10-15T04:00:00+00:00'),('b','investor','AAA','SELL',4,'2026-10-15T05:00:00+00:00'),
                  ('c','investor','BBB','BUY',5,'2026-10-15T04:00:00+00:00'),('d','investor','BBB','SELL',5,'2026-10-16T04:00:00+00:00'),
                  ('e','investor','BBB','BUY',3,'2026-10-17T04:00:00+00:00'),('f','investor','BBB','SELL',3,'2026-10-18T04:00:00+00:00')]
            with connect(path) as db:
                for i,portfolio,symbol,side,qty,at in rows:
                    db.execute('INSERT INTO trades(id,request_id,portfolio,symbol,side,quantity,price,fee,quote_time,created_at) VALUES(?,?,?,?,?,?,100,1,0,?)',(i,'request-'+i+'-0000000000',portfolio,symbol,side,qty,at))
                sc=scorecard(db,datetime(2026,10,19,tzinfo=timezone.utc))
        closed=next(g for g in sc['baskets']['investor'] if g['key']=='closed')
        self.assertIn('2 fully closed',closed['detail'])
        profit=next(g for g in sc['baskets']['investor'] if g['key']=='profit')
        self.assertTrue(profit['detail'].startswith('−₹') or profit['detail'].startswith('₹'))
if __name__=='__main__':unittest.main()
