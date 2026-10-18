import tempfile,unittest
from pathlib import Path
from marketdesk.storage import init,connect,set_state
from marketdesk.paper import init_paper
from marketdesk.order_audit import init_metadata,annotate,current_identity,save_identity
from marketdesk.strategy_performance import attribution

class ProvenanceTests(unittest.TestCase):
 def test_live_and_completed_are_distinct_and_sales_keep_entry(self):
  rows=[]
  for i,mode in enumerate(('completed breakout; signal ','live breakout above 99; completed inputs ')):
   rows.append(dict(id=str(i),request_id='claude-'+str(i),portfolio='trader',symbol='TEST',side='BUY',quantity=1,price=100,fee=1,created_at='2026-10-15T09:00:00+00:00',source='automatic',reason='Claude V2 (Nifty 500 filter off) '+mode+'2026-10-14'))
  rows.append({**rows[0],'side':'SELL','quantity':1,'price':110})
  positions=[dict(symbol='TEST',quantity=1,cost=101,mark=110,mark_time=None,mark_source='daily_close')]
  result=attribution(rows,positions,by_version=True)
  self.assertEqual(len(result['cohorts']),2)
  self.assertEqual(sum(x['realized_net'] for x in result['cohorts']),8)
  self.assertTrue(all(x['buy_fills']==1 for x in result['cohorts']))
 def test_additive_mapping_preserves_ledger_and_is_idempotent(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'test.db';init(p);init_paper(p)
   with connect(p) as db:
    db.execute("INSERT INTO trades(id,request_id,portfolio,symbol,side,quantity,price,fee,quote_time,created_at,source,reason) VALUES('one','claude-one','trader','TEST','BUY',1,100,1,0,'2026-10-15T09:00:00+00:00','automatic','Claude V2 (Nifty 500 filter off) live breakout above 99; completed inputs 2026-10-14')")
    before=[tuple(r) for r in db.execute('SELECT * FROM trades')];init_metadata(db);init_metadata(db)
    self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM trades')])
    t=annotate(db,[dict(db.execute('SELECT * FROM trades').fetchone())])[0]
    self.assertIn('/live/',t['strategy_version_id']);self.assertEqual(db.execute('SELECT COUNT(*) FROM order_strategy').fetchone()[0],1)
 def test_config_version_is_stable_but_changes_with_rule_options(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td)/'test.db';init(p)
   with connect(p) as db:
    cfg={'trader':{'strategy':'claude-original-v2','live_breakout':True}};set_state(db,'auto_config',cfg);a=current_identity(db,'trader',True)
    cfg['trader']['updated_at']='later';set_state(db,'auto_config',cfg);self.assertEqual(a,current_identity(db,'trader',True))
    cfg['trader']['live_breakout']=False;set_state(db,'auto_config',cfg);self.assertNotEqual(a[0],current_identity(db,'trader',True)[0])
