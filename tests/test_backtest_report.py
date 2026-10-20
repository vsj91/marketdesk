import json,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from marketdesk.storage import init,connect
from marketdesk.paper import init_paper
import marketdesk.backtest_report as br

class BacktestReportTests(unittest.TestCase):
    def test_backtests_sit_beside_live_paper_results(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'t.db';init(p);init_paper(p);summary=Path(d)/'s.json'
            summary.write_text(json.dumps(dict(generated='2026-10-20',baskets={'momentum12':dict(full=dict(cagr=26.4,maxdd=52.2),verdict='pass')})))
            with connect(p) as db:
                db.execute('CREATE TABLE IF NOT EXISTS claude_benchmark(day TEXT PRIMARY KEY, close REAL NOT NULL)')
                db.executemany('INSERT INTO claude_benchmark VALUES(?,?)',[('2026-09-30',100.),('2026-10-01',101.),('2026-10-30',110.)])
                db.execute("INSERT INTO trades(id,request_id,portfolio,symbol,side,quantity,price,fee,quote_time,created_at) VALUES('1','r1','momentum12','AAA','BUY',10,100,20,0,'2026-10-01T04:00:00+00:00')")
                db.execute("UPDATE portfolios SET cash=105000 WHERE name='momentum12'")
            with patch.object(br,'SUMMARY',summary):r=br.report(p,datetime(2026,10,31,6,tzinfo=timezone.utc))
        rows={x['name']:x for x in r['baskets']}
        m=rows['momentum12'];self.assertEqual(m['backtest']['verdict'],'pass')
        self.assertEqual(m['paper']['since'],'2026-10-01');self.assertEqual(m['paper']['days'],30);self.assertEqual(m['paper']['trades'],1)
        self.assertEqual(m['paper']['nifty500_pct'],10.0)                          # from the close before the first trade
        self.assertAlmostEqual(m['paper']['backtest_pace_pct'],round(100*(1.264**(30/365.25)-1),2))
        self.assertIsNone(rows['trader']['paper']['since']);self.assertIsNone(rows['trader']['backtest'])
        self.assertEqual(rows['reversion']['state'],'retired');self.assertEqual(rows['high52']['state'],'retired')
        self.assertIsNone(r['missing'])
        with patch.object(br,'SUMMARY',Path(d)/'absent.json'):
            with tempfile.TemporaryDirectory() as d2:
                q=Path(d2)/'t.db';init(q);init_paper(q);self.assertIn('research_multi_year.py summary',br.report(q)['missing'])
