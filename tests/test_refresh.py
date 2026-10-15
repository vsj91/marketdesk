import tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from marketdesk import provider
from marketdesk.refresh import Refresh
from marketdesk.storage import init,connect

class RefreshTests(unittest.TestCase):
 def test_missing_token_still_runs_fallback(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);db=root/'test.sqlite3';init(db)
   row={'symbol':'TEST','instrument_key':'NSE_EQ|INE000A01010','isin':'INE000A01010'}
   with connect(db) as cx:cx.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',('TEST',row['instrument_key'],row['isin'],'Test','Test'))
   job=Refresh(db);job.state={'started_at':'test','done':0,'errors':0,'completed_symbols':[]};job.lock.acquire()
   names=['UPSTOX_TOKEN','UPSTOX_AUTH_ERROR','FUNDAMENTAL_DIR','RAW_JSON_DIR','RAW_CSV_DIR']
   before={n:getattr(provider,n) for n in names};headers=dict(provider.headers)
   try:
    with patch('marketdesk.annual_reports.AnnualReportSources') as reports,patch('marketdesk.refresh.ROOT',root),patch('marketdesk.refresh.load_token',return_value=''),patch('marketdesk.refresh.import_exports'),patch.object(job.fallbacks,'fetch',return_value={'nse':{'status':'no_data'},'yahoo':{'status':'no_data'}}) as fallback:
     reports.return_value.fetch.return_value={'status':'unresolved'}
     job.run([row],'fundamentals')
     fallback.assert_called_once_with('TEST','INE000A01010')
     self.assertEqual(job.state['status'],'complete');self.assertEqual(job.state['done'],1)
     self.assertIn('upstox_warning',job.state)
   finally:
    for name,value in before.items():setattr(provider,name,value)
    provider.headers.clear();provider.headers.update(headers)
 def test_failed_endpoint_preserves_good_raw(self):
  with tempfile.TemporaryDirectory() as folder:
   root=Path(folder);stock=root/'TEST';stock.mkdir()
   previous={'symbol':'TEST','isin':'INE000A01010','http_status':200,'payload':{'status':'success','data':{}},'requested_at_utc':'2020-01-01T00:00:00+00:00'}
   for endpoint in ['profile','income_statement','balance_sheet','cash_flow','key_ratios']:(stock/(endpoint+'.json')).write_text(json.dumps(previous))
   with patch.object(provider,'FUNDAMENTAL_DIR',root),patch.object(provider,'UPSTOX_AUTH_ERROR',None),patch.object(provider,'resolve_isin_for_symbol',return_value='INE000A01010'),patch.object(provider,'fetch_fundamental_payload',return_value=(None,503,'https://api.upstox.com','unavailable')),patch.object(provider,'log_upstox_status'):
    provider.fetch_and_save_fundamental_jsons('TEST','NSE_EQ|INE000A01010')
   self.assertEqual(json.loads((stock/'profile.json').read_text()),previous)
   self.assertEqual(json.loads((stock/'profile.last_error.json').read_text())['http_status'],503)

if __name__=='__main__':unittest.main()
