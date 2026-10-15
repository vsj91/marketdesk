import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from xml.sax.saxutils import escape
from marketdesk.market_caps import workbook_records,save_workbook,classifications,decorate_html,KEY
from marketdesk.storage import init,connect,set_state
from marketdesk.server import App,Server


def workbook(change=None):
    rows=[{'A':'Average Market Capitalization of listed companies during the six months ended 30 June 2026'}, {'C':'ISIN','F':'NSE Symbol'}]
    rows += [dict(A=str(n),B='Company '+str(n),C='INE'+str(n).zfill(9),F='STOCK'+str(n),J=str(1000000/n),K='Large Cap' if n<=100 else 'Mid Cap' if n<=250 else 'Small Cap') for n in range(1,1001)]
    if change:change(rows)
    xml='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    for i,row in enumerate(rows,1):
        xml+='<row>'+''.join(f'<c r="{col}{i}" t="inlineStr"><is><t>{escape(value)}</t></is></c>' for col,value in row.items())+'</row>'
    xml+='</sheetData></worksheet>';out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:z.writestr('xl/worksheets/sheet1.xml',xml)
    return out.getvalue()


class MarketCapTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'data.db';init(self.path)
        with connect(self.path) as db:
            db.execute("INSERT INTO instruments VALUES('RENAMED','NSE_EQ|1','INE000000001','Renamed Company','Test')")
            db.execute("INSERT INTO instruments VALUES('STOCK2','NSE_EQ|2','INE999999999','Not the same identity','Test')")
        self.url='https://portal.amfiindia.com/spages/AverageMarketCapitalization30Jun2026.xlsx'

    def test_official_boundaries_and_isin_join_not_name_or_symbol(self):
        values=workbook_records(workbook(),'2026-06-30')
        for rank,label in ((100,'Large cap'),(101,'Mid cap'),(250,'Mid cap'),(251,'Small cap')):
            self.assertEqual(values['INE'+str(rank).zfill(9)]['label'],label)
        save_workbook(self.path,workbook(),self.url,'2026-06-30')
        result=classifications(self.path)
        self.assertEqual(result['stocks']['RENAMED']['label'],'Large cap')
        self.assertEqual(result['stocks']['STOCK2']['label'],'Unclassified')
        self.assertEqual(sum(result['counts'].values()),2)

    def test_invalid_sources_duplicates_periods_and_categories_rejected(self):
        with self.assertRaises(ValueError):save_workbook(self.path,workbook(),'https://example.com/file.xlsx','2026-06-30')
        with self.assertRaises(ValueError):workbook_records(workbook(),'2025-12-31')
        for change in (lambda r:r[3].update(C=r[2]['C']),lambda r:r[2].update(K='Small Cap'),lambda r:r.pop()):
            with self.assertRaises(ValueError):workbook_records(workbook(change),'2026-06-30')

    def test_failed_import_preserves_previous_verified_snapshot(self):
        save_workbook(self.path,workbook(),self.url,'2026-06-30');before=classifications(self.path)
        with self.assertRaises(ValueError):save_workbook(self.path,workbook(lambda r:r[3].update(C=r[2]['C'])),self.url,'2026-06-30')
        self.assertEqual(classifications(self.path),before)

    def test_api_and_every_html_response_include_shared_labels(self):
        save_workbook(self.path,workbook(),self.url,'2026-06-30')
        server=Server(('127.0.0.1',0),App(self.path,read_only=True));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base='http://127.0.0.1:'+str(server.server_port)
        with urlopen(base+'/api/stock-classifications') as response:
            self.assertEqual(json.load(response)['stocks']['RENAMED']['label'],'Large cap')
        for page in ('/','/replay.html','/fundamental-repair.html','/source-conflicts.html','/trader-comparison.html'):
            with urlopen(base+page) as response:
                html=response.read();self.assertEqual(html.count(b'src="/stock-labels.js"'),1);self.assertIn(b'/stock-labels.css',html)
        html=b'<html><head></head><body></body></html>'
        self.assertEqual(decorate_html(decorate_html(html)),decorate_html(html))
