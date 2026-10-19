import tempfile,unittest
from pathlib import Path
from marketdesk.close_check import parse_bhav,compare,run
from marketdesk.storage import init,connect,set_state,get_state

CSV='''SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY
AAA, EQ, 16-Oct-2026, 99, 100, 105, 98, 103, 102.0, 101, 5000
BBB, BE, 16-Oct-2026, 49, 50, 55, 48, 53, 52.0, 51, 800
CCC, N1, 16-Oct-2026, 9, 10, 11, 9, 10, 10.0, 10, 10
'''
class CloseCheckTests(unittest.TestCase):
    def test_parse_keeps_equity_series_only(self):
        rows=parse_bhav(CSV);self.assertEqual(set(rows),{'AAA','BBB'});self.assertEqual(rows['AAA']['close'],102.0)
    def test_compare_flags_close_and_volume_and_missing(self):
        theirs=parse_bhav(CSV)
        ours={'AAA':dict(open=100,high=105,low=98,close=102.0,volume=5000),'BBB':dict(open=50,high=55,low=48,close=55.0,volume=100),'ZZZ':dict(open=1,high=1,low=1,close=1,volume=1)}
        r=compare(ours,theirs)
        self.assertEqual((r['compared'],r['close_mismatches'],r['volume_mismatches'],r['not_in_bhavcopy']),(2,1,1,1))
        self.assertEqual(r['worst'][0]['symbol'],'BBB')
    def test_run_stores_summary_and_reuses_checked_days(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'t.db';init(path)
            with connect(path) as db:
                db.execute("INSERT INTO instruments(symbol,instrument_key,isin,name,sector) VALUES('AAA','k','i','A','x')")
                db.execute("INSERT INTO candles(symbol,ts,open,high,low,close,volume,source,observed_at) VALUES('AAA','2026-10-16T00:00:00+05:30',100,105,98,102,5000,'t','2026-10-17T00:00:00+00:00')")
                set_state(db,'quality_sessions',['2026-10-16'])
            calls=[]
            def fetch(day):calls.append(day);return parse_bhav(CSV)
            first=run(path,sessions=1,fetch=fetch);second=run(path,sessions=1,fetch=fetch)
            self.assertEqual(first['sessions']['2026-10-16']['compared'],1);self.assertEqual(calls,['2026-10-16'])
            self.assertEqual(second['sessions']['2026-10-16']['close_mismatches'],0)
if __name__=='__main__':unittest.main()
