import copy,unittest,tempfile
from pathlib import Path
from datetime import datetime,timezone,timedelta
from marketdesk.price_reconciliation import verify,digest,confirmed,HISTORY,ACTIONS,IDENTITY,FIELDS
from marketdesk.storage import init,connect,set_state
from marketdesk.quality import inspect_history

class ReconciliationTests(unittest.TestCase):
 def setUp(self):
  self.clock=datetime(2026,10,15,tzinfo=timezone.utc)
  self.rows=[dict(ts='2026-10-13T00:00:00+05:30',open=100,high=110,low=95,close=100,volume=1000),
             dict(ts='2026-10-14T00:00:00+05:30',open=70,high=75,low=65,close=68,volume=2000)]
  data={'identity':{'symbol':'TEST','isin':'ISIN','observed_at':self.clock.isoformat(),'membership_sha256':'0'*64},'actions':[],'history':{'data':[
   {'CH_SYMBOL':'TEST','CH_SERIES':'EQ','mTIMESTAMP':datetime.fromisoformat(r['ts']).strftime('%d-%b-%Y'),
    'CH_PREVIOUS_CLS_PRICE':100,**{v:r[k] for k,v in FIELDS.items()}} for r in self.rows]}}
  self.proof=dict(symbol='TEST',isin='ISIN',observed_at=self.clock.isoformat(),sessions=[r['ts'][:10] for r in self.rows],
                  sources={'history':HISTORY,'actions':ACTIONS,'identity':IDENTITY},evidence=data,sha256=digest(data))
 def test_verified_market_move_does_not_waive_short_history(self):
  day=verify(self.proof,'TEST','ISIN',self.rows,self.clock)
  reasons=inspect_history(self.rows,[r['ts'][:10] for r in self.rows],self.clock,{day})
  self.assertIn('Fewer than 200 completed daily candles',reasons)
  self.assertFalse(any('discontinuity' in r for r in reasons))
  self.assertTrue(any('discontinuity' in r for r in inspect_history(self.rows,[],self.clock)))
 def test_evidence_mismatch_expiry_and_corporate_actions_fail_closed(self):
  for change in ('action','identity','candle','previous','duplicate','missing','hash','expired','source'):
   p=copy.deepcopy(self.proof);data=p['evidence']
   if change=='action':data['actions']=[{'subject':'Demerger'}]
   elif change=='identity':data['identity']['isin']='OTHER'
   elif change=='candle':data['history']['data'][1]['CH_TOT_TRADED_QTY']=1999
   elif change=='previous':data['history']['data'][1]['CH_PREVIOUS_CLS_PRICE']=70
   elif change=='duplicate':data['history']['data'].append(data['history']['data'][0])
   elif change=='missing':data['history']['data'].pop()
   elif change=='hash':p['sha256']='bad'
   elif change=='expired':p['observed_at']=(self.clock-timedelta(days=1)).isoformat()
   elif change=='source':p['sources']['history']='https://example.com'
   if change!='hash':p['sha256']=digest(data)
   with self.subTest(change=change),self.assertRaises((ValueError,KeyError)):verify(p,'TEST','ISIN',self.rows,self.clock)
 def test_saved_proof_is_rechecked_when_local_prices_change(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'db';init(path)
   with connect(path) as db:
    db.execute('insert into instruments values(?,?,?,?,?)',('TEST','key','ISIN','name','sector'))
    set_state(db,'price_reconciliation_TEST',{'proofs':{'2026-10-14':self.proof}})
    self.assertEqual(confirmed(db,'TEST',self.rows,self.clock),{'2026-10-14'})
    self.rows[1]['close']=67
    self.assertEqual(confirmed(db,'TEST',self.rows,self.clock),set())
