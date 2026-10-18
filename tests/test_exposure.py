import sqlite3,unittest
from datetime import date,timedelta
from marketdesk.exposure import exposure

class ExposureTests(unittest.TestCase):
 def test_normalized_sectors_and_aligned_correlation(self):
  db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.row_factory=sqlite3.Row
  db.executescript('CREATE TABLE instruments(symbol,sector);CREATE TABLE candles(symbol,ts,close);')
  for s,sector in [('A','Bank'),('B','Banks'),('C',None)]:db.execute('INSERT INTO instruments VALUES(?,?)',(s,sector))
  price=100
  for n in range(61):
   price*=1+(n%5-2)*.001
   for s,m in [('A',1),('B',2)]:db.execute('INSERT INTO candles VALUES(?,?,?)',(s,str(date(2026,1,1)+timedelta(days=n)),price*m))
  ps=[dict(symbol='A',cost=90,value=100),dict(symbol='B',cost=180,value=200),dict(symbol='C',cost=10,value=20)]
  result=exposure(db,ps,1000);financial=result['sectors'][0]
  self.assertEqual(financial['sector'],'Financials');self.assertEqual(financial['pct_equity'],30)
  pairs={(p['a'],p['b']):p for p in result['correlations']}
  self.assertAlmostEqual(pairs['A','B']['correlation'],1);self.assertEqual(pairs['A','B']['observations'],60)
  self.assertIsNone(pairs['A','C']['correlation'])
  ps[0]['value']=None;self.assertIsNone(exposure(db,ps,None)['gross_value'])
