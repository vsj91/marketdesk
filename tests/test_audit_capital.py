import tempfile, unittest
from pathlib import Path
from marketdesk.storage import init,connect
from marketdesk.paper import init_paper,portfolios

class CapitalDisplayTests(unittest.TestCase):
 def test_existing_capital_is_returned_without_reseeding(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'test.db';init(p);init_paper(p)
   with connect(p) as db:db.execute('UPDATE portfolios SET initial_cash=100000,cash=100000')
   init_paper(p)
   self.assertEqual([x['initial_cash'] for x in portfolios(p)],[100000]*13)
   with connect(p) as db:self.assertEqual(db.execute('SELECT SUM(cash) FROM portfolios').fetchone()[0],1300000)
 def test_ui_uses_ledger_field(self):
  root=Path(__file__).resolve().parents[1]
  self.assertNotIn('Each starts with ₹10,00,000',(root/'web/index.html').read_text())
  self.assertIn("fmt(p.initial_cash)",(root/'web/app.js').read_text())
