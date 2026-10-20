import tempfile,unittest
from pathlib import Path
from marketdesk import drawdown
from marketdesk.storage import init,connect

class DrawdownTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)/'d.db';init(self.path)
    def step(self,equity,limit=15):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE');return drawdown.update(db,'trader',equity,100000,limit)
    def test_peak_ratchets_halts_at_limit_and_resumes_with_hysteresis(self):
        self.assertFalse(self.step(100000)[0]);self.assertFalse(self.step(110000)[0])
        self.assertFalse(self.step(95000)[0])           # 13.6% below the 110k peak
        halted,info=self.step(93000)                    # 15.5%
        self.assertTrue(halted);self.assertEqual(info['peak'],110000)
        self.assertTrue(self.step(100000)[0])           # 9.1%: still above the 9% resume line
        self.assertFalse(self.step(101000)[0])          # 8.2%: resumes
    def test_start_capital_is_the_floor_for_the_peak_and_limit_is_validated(self):
        _,info=self.step(90000);self.assertEqual(info['peak'],100000)
        self.assertEqual(drawdown.limit_pct({'max_drawdown_pct':'x'}),15.0);self.assertEqual(drawdown.limit_pct({'max_drawdown_pct':10}),10.0)
        self.assertIn('Exits remain active',drawdown.message(info))
    def test_exits_only_blocks_buys_without_equity_and_names_the_reason(self):
        with connect(self.path) as db:
            halted,info=drawdown.gate(db,'trader',None,100000,{'exits_only':True,'exits_only_reason':'Backtest lost money.'})
            self.assertTrue(halted);self.assertIn('Exits only: Backtest lost money.',drawdown.message(info))
            self.assertFalse(drawdown.gate(db,'trader',100000,100000,{})[0])
if __name__=='__main__':unittest.main()
