import unittest,tempfile,json,uuid,csv
from pathlib import Path
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
from marketdesk.storage import init,connect,set_state,store_quote,now
from marketdesk.paper import init_paper,trade,portfolios
from marketdesk.importer import candle_row,summary_metrics,import_exports
from marketdesk.provider import extract_fundamental_details,fetch_historical_candles
from marketdesk import provider
from support import seed_quality,fixture_book

class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'test.sqlite';init(self.path);init_paper(self.path)
        self.clock=datetime.now(timezone.utc)
        with connect(self.path) as db:
            db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',('TEST','NSE_EQ|INE000A01010','INE000A01010','Test','Test'))
            set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'NORMAL_OPEN'}})
            store_quote(db,'TEST',{'ltp':100,'ltt':int(self.clock.timestamp()*1000),'cp':99},self.clock.isoformat())
        seed_quality(self.path,'TEST',self.clock)
        self.depth=patch('marketdesk.paper.fetch_book',side_effect=fixture_book);self.depth.start();self.addCleanup(self.depth.stop)
    def tearDown(self):self.tmp.cleanup()
    def order(self,side='BUY',qty=10,request=None,portfolio='trader'):
        return trade(self.path,{'portfolio':portfolio,'symbol':'TEST','side':side,'quantity':qty,'request_id':request or str(uuid.uuid4())},self.clock)
    def test_buy_sell_accounting_and_separate_portfolios(self):
        buy=self.order();sell=self.order('SELL',4)
        accounts={p['name']:p for p in portfolios(self.path)}
        self.assertEqual(accounts['trader']['cash'],round(1000000-buy['price']*10-buy['fee']+sell['price']*4-sell['fee'],2))
        self.assertEqual(accounts['trader']['positions'][0]['quantity'],6)
        self.assertEqual(accounts['investor']['cash'],1000000)
        self.assertEqual(accounts['investor']['positions'],[])
        self.assertAlmostEqual(accounts['trader']['pnl'],accounts['trader']['cash']+600-1000000)
    def test_duplicate_submission_is_idempotent(self):
        rid=str(uuid.uuid4());a=self.order(request=rid);b=self.order(request=rid)
        self.assertEqual(a,b)
        with self.assertRaises(ValueError):self.order(qty=11,request=rid)
        self.assertEqual(len(portfolios(self.path)[0]['trades']),1)
    def test_no_oversell_or_overspend(self):
        with self.assertRaises(ValueError):self.order('SELL',1)
        with self.assertRaises(ValueError):self.order(qty=10001)
        self.assertEqual(portfolios(self.path)[0]['cash'],1000000)
    def test_stale_closed_disconnected_and_future_quotes_block(self):
        with connect(self.path) as db:db.execute('UPDATE quotes SET trade_time=trade_time-31000')
        with self.assertRaisesRegex(ValueError,'30 seconds'):self.order()
        with connect(self.path) as db:set_state(db,'stream',{'status':'connected','market_status':{'NSE_EQ':'CLOSED'}})
        with self.assertRaisesRegex(ValueError,'open NSE'):self.order()
    def test_out_of_order_feed_cannot_regress(self):
        with connect(self.path) as db:
            self.assertFalse(store_quote(db,'TEST',{'ltp':90,'ltt':int(self.clock.timestamp()*1000)-1}))
            self.assertFalse(store_quote(db,'TEST',{'ltp':90,'ltt':int(self.clock.timestamp()*1000)+120000}))
            self.assertEqual(db.execute('SELECT price FROM quotes').fetchone()[0],100)
    def test_bad_quantity(self):
        for value in [True,0,-1,1.5,'10']:
            with self.assertRaises(ValueError):self.order(qty=value)

class DataTests(unittest.TestCase):
    def test_bad_ohlc_rejected(self):
        for values in [[10,9,8,10,1],[10,11,8,10,-1],[10,11,8,'nan',1]]:
            with self.assertRaises(ValueError):candle_row(['2026-01-01T00:00:00+05:30',*values])
        with self.assertRaises(ValueError):candle_row(['2026-01-01',10,11,8,10,1])
    def test_missing_debt_not_total_liabilities(self):
        p={'status':'success','data':{'type':'consolidated','time_period':'yearly','units_in':'crore','history':[{'period':'Mar 2026','total_asset':100,'total_liability':40}]}}
        result=extract_fundamental_details({'balance_sheet':p})['latest']
        self.assertEqual(result['equity'],60)
        self.assertIsNone(result['total_borrowings']);self.assertIsNone(result['total_lease_liabilities'])
    def test_period_and_unit_mismatch_not_combined(self):
        def payload(label,value,units):return {'status':'success','data':{'type':'consolidated','units_in':units,'full_statement':[{'particular':label,'history':[{'period':'Mar 2026','value':value}]}]}}
        d=extract_fundamental_details({'income_statement':payload('Profit Before Tax',10,'crore'),'balance_sheet':payload('Total Equity',100,'lakh')})
        self.assertEqual(len(d['annual']),2)
        self.assertFalse(any(x['profit_before_tax'] is not None and x['equity'] is not None for x in d['annual']))
    def test_growth_requires_same_month_previous_year(self):
        p={'income_statement':{'data':{'income_statement':[{'category':'revenue','history':[{'period':'Mar 2026','value':200},{'period':'Dec 2025','value':100}]}]}}}
        self.assertIsNone(summary_metrics(p)['revenue_growth_yoy'])
    def test_http_200_error_not_successful_empty_candles(self):
        response=type('Response',(),{'status_code':200,'json':lambda self:{'status':'error'}})()
        with patch.object(provider,'UPSTOX_AUTH_ERROR',None),patch.object(provider,'UPSTOX_TOKEN','valid'),patch.object(provider,'throttled_requests_get',return_value=response):
            result=fetch_historical_candles('NSE_EQ|INE000A01010','2026-01-01','2026-01-02')
        self.assertEqual(result['status'],'api_error')
    def test_import_is_idempotent_and_rejects_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);db=root/'db.sqlite';(root/'RAW_CANDLES/csv').mkdir(parents=True);(root/'RAW_CANDLES/json').mkdir();(root/'RAW_FUNDAMENTALS').mkdir()
            (root/'VALID_STOCK_INPUT.csv').write_text('symbol,instrument_key\nMARUTI,NSE_EQ|INE585B01010\n')
            (root/'RAW_CANDLES/csv/MARUTI.csv').write_text('timestamp,open,high,low,close,volume\n2026-01-01T00:00:00+05:30,10,11,9,10,100\n')
            meta=root/'RAW_CANDLES/json/MARUTI.json';meta.write_text(json.dumps({'symbol':'MARUTI','instrument_key':'NSE_EQ|INE585B01010'}))
            import_exports(root,db);import_exports(root,db)
            with connect(db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],1)
            meta.write_text(json.dumps({'symbol':'MARUTI','instrument_key':'NSE_EQ|INE999A01010'}))
            report=import_exports(root,db)
            self.assertEqual(report['counts']['identity_mismatches'],1)
            with connect(db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM candles').fetchone()[0],0)

if __name__=='__main__':unittest.main()
