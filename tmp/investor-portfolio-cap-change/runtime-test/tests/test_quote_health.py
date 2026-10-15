import tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch
import requests
from marketdesk.storage import init,connect,set_state,get_state,store_quote
from marketdesk.paper import init_paper
from marketdesk.quote_health import fresh,stale_holdings,stale_message,refresh_held_quotes


class HeldQuoteTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'test.db'
  init(self.path);init_paper(self.path);self.clock=datetime(2026,10,15,6,tzinfo=timezone.utc)
  self.old=self.clock-timedelta(seconds=60)
  with connect(self.path) as db:
   db.execute("INSERT INTO instruments VALUES('TEST','NSE_EQ|TEST','TEST','Test','Test')")
   for p in ('trader','investor'):db.execute('INSERT INTO positions VALUES(?,?,?,?)',(p,'TEST',10,1000))
   db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?)',('TEST',100,99,int(self.old.timestamp()*1000),self.old.isoformat(),'test'))
   set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
  self.raw=dict(symbol='TEST',instrument_token='NSE_EQ|TEST',timestamp=self.clock.isoformat(),
      last_trade_time=str(int((self.clock-timedelta(seconds=2)).timestamp()*1000)),last_price=101,prev_close_price=99)
  token=patch('marketdesk.quote_health.load_token',return_value='fixture');token.start();self.addCleanup(token.stop)
 def response(self,raw=None):
  return Mock(status_code=200,json=Mock(return_value={'status':'success','data':{'NSE_EQ:TEST':self.raw if raw is None else raw}}))
 def quote(self):
  with connect(self.path) as db:return dict(db.execute('SELECT * FROM quotes').fetchone())
 def refresh(self,raw=None):
  with patch('marketdesk.quote_health.requests.get',return_value=self.response(raw)):
   return refresh_held_quotes(self.path,'trader',self.clock)
 def test_recovers_genuine_recent_trade_for_both_without_changing_holdings(self):
  before=self.quote()
  with patch('marketdesk.quote_health.requests.get',return_value=self.response()) as request:
   report=refresh_held_quotes(self.path,'trader',self.clock)
   self.assertIsNone(refresh_held_quotes(self.path,'investor',self.clock))
  request.assert_called_once()
  self.assertEqual(report['updated'],['TEST']);self.assertEqual(report['remaining'],[])
  q=self.quote();self.assertTrue(fresh(q,self.clock));self.assertEqual(q['trade_time'],int(self.raw['last_trade_time']))
  self.assertEqual(q['source'],'upstox_rest_quote_v3');self.assertGreater(q['trade_time'],before['trade_time'])
  with connect(self.path) as db:
   self.assertEqual([r['quantity'] for r in db.execute('SELECT * FROM positions')],[10,10])
   self.assertEqual(db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],0)
 def test_new_http_observation_never_makes_old_trade_fresh(self):
  old=self.quote();raw={**self.raw,'last_trade_time':str(old['trade_time'])}
  report=self.refresh(raw)
  self.assertEqual(self.quote(),old);self.assertEqual(report['updated'],[])
  self.assertEqual(report['remaining'][0]['symbol'],'TEST')
  self.assertIn('30 seconds',report['unresolved']['TEST'])
 def test_rejects_identity_invalid_and_stale_provider_data(self):
  old=self.quote()
  for edit in ({'symbol':'OTHER'},{'instrument_token':'NSE_EQ|OTHER'},
      {'timestamp':self.old.isoformat()},{'timestamp':(self.clock+timedelta(seconds=1)).isoformat()},
      {'last_price':'nan'},{'prev_close_price':None},
      {'last_trade_time':str(int((self.clock+timedelta(seconds=1)).timestamp()*1000))}):
   with self.subTest(edit=edit):
    self.assertEqual(self.refresh({**self.raw,**edit})['updated'],[])
    self.assertEqual(self.quote(),old)
 def test_provider_failure_keeps_existing_quotes_and_names_blocker(self):
  old=self.quote()
  with patch('marketdesk.quote_health.requests.get',side_effect=requests.Timeout()):
   report=refresh_held_quotes(self.path,'investor',self.clock)
  self.assertEqual(report['status'],'unavailable');self.assertEqual(self.quote(),old)
  with connect(self.path) as db:
   self.assertIn('TEST (last trade 60s old)',stale_message(db,'investor',self.clock))
   self.assertEqual(get_state(db,'held_quote_refresh_investor')['status'],'unavailable')
 def test_no_requests_when_closed_disconnected_or_no_holdings(self):
  for status,market in (('connected','CLOSED'),('disconnected','NORMAL_OPEN')):
   with connect(self.path) as db:set_state(db,'stream',{'status':status,'market_status':{'NSE_EQ':market}})
   with patch('marketdesk.quote_health.requests.get') as request:
    self.assertIsNone(refresh_held_quotes(self.path,'trader',self.clock));request.assert_not_called()
  with connect(self.path) as db:
   set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
   db.execute('DELETE FROM positions')
  with patch('marketdesk.quote_health.requests.get') as request:
   self.assertIsNone(refresh_held_quotes(self.path,'trader',self.clock));request.assert_not_called()
 def test_does_not_overwrite_newer_stream_quote_arriving_during_request(self):
  def updated(*args,**kwargs):
   with connect(self.path) as db:store_quote(db,'TEST',dict(ltp=103,ltt=int(self.clock.timestamp()*1000),cp=99),self.clock.isoformat())
   return self.response()
  with patch('marketdesk.quote_health.requests.get',side_effect=updated):
   report=refresh_held_quotes(self.path,'trader',self.clock)
  self.assertEqual(report['updated'],[]);self.assertEqual(self.quote()['price'],103)
  self.assertEqual(self.quote()['source'],'upstox_websocket_v3')
 def test_market_close_during_request_prevents_merge(self):
  old=self.quote()
  def closed(*args,**kwargs):
   with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'CLOSED'}})
   return self.response()
  with patch('marketdesk.quote_health.requests.get',side_effect=closed):refresh_held_quotes(self.path,'trader',self.clock)
  self.assertEqual(self.quote(),old)
 def test_identity_change_during_request_prevents_merge(self):
  old=self.quote()
  def changed(*args,**kwargs):
   with connect(self.path) as db:db.execute("UPDATE instruments SET instrument_key='NSE_EQ|REPLACED' WHERE symbol='TEST'")
   return self.response()
  with patch('marketdesk.quote_health.requests.get',side_effect=changed):refresh_held_quotes(self.path,'trader',self.clock)
  self.assertEqual(self.quote(),old)
 def test_missing_quote_identifies_portfolio_symbol(self):
  with connect(self.path) as db:
   db.execute('DELETE FROM quotes')
   self.assertEqual(stale_holdings(db,'investor',self.clock),[dict(symbol='TEST',instrument_key='NSE_EQ|TEST',reason='no quote')])

if __name__=='__main__':unittest.main()
