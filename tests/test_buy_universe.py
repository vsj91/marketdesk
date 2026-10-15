import copy
import csv
import io
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_autotrading as legacy_tests
import test_claude_trader as claude_tests
from marketdesk.buy_universe import (SOURCE, KEY, allowed_symbols, membership,
    parse_constituents, refresh_membership, universe_status, MembershipRefresh)
from marketdesk.paper import trade
from marketdesk.storage import init, connect, get_state, set_state
from support import fixture_book


def members(included=('TEST',)):
    names=list(included)+['MEMBER'+str(n) for n in range(500-len(included))]
    return {name:'INE'+str(n).zfill(9) for n,name in enumerate(names)}


def csv_bytes(values):
    out=io.StringIO();writer=csv.writer(out)
    writer.writerow(['Company Name','Industry','Symbol','Series','ISIN Code'])
    for s,i in values.items():writer.writerow([s,'Test',s,'EQ',i])
    return out.getvalue().encode()


def seed(db, clock, included=('TEST',)):
    values=members(included)
    for s,i in values.items():db.execute('UPDATE instruments SET isin=? WHERE symbol=?',(i,s))
    set_state(db,KEY,dict(source=SOURCE,fetched_at=clock.isoformat(),members=values))


@contextmanager
def fixture(kind):
    f=claude_tests.ClaudeTests() if kind=='claude' else legacy_tests.AutoTradingTests()
    f.setUp()
    try:
        portfolio='trader' if kind=='claude' else kind
        if kind!='claude':f.enable(portfolio)
        with connect(f.path) as db:
            cfg=get_state(db,'auto_config');cfg[portfolio]['buy_universe']='nifty500';set_state(db,'auto_config',cfg)
            seed(db,f.clock)
        def order(side='BUY'):
            guard=f.c.guard(f.signal,f.clock) if kind=='claude' else f.engine.guard(portfolio,f.signal,f.clock)
            return trade(f.path,dict(portfolio=portfolio,symbol='TEST',side=side,quantity=1,request_id=str(uuid.uuid4())),f.clock,auto_guard=guard)
        yield f,portfolio,order
    finally:
        f.doCleanups()
        if kind!='claude':f.tearDown()


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.db';init(self.path)
        self.clock=datetime.now(timezone.utc)
        with connect(self.path) as db:
            db.execute("INSERT INTO instruments VALUES('TEST','NSE_EQ|TEST','INE000000000','Test','Test')")
            set_state(db,'auto_config',{'trader':{'buy_universe':'nifty500'}});seed(db,self.clock)

    def test_csv_schema_count_duplicates_and_corporate_action_extra(self):
        values=members();self.assertEqual(parse_constituents(csv_bytes(values)),values)
        self.assertEqual(parse_constituents(csv_bytes({**values,'DUMMYHEG':'DUM545A01024'})),values)
        values['EXTRA']='INE999999999';self.assertEqual(len(parse_constituents(csv_bytes(values))),501)
        self.assertEqual(len(parse_constituents(csv_bytes(values).replace(b',EQ,',b',BE,',1))),501)
        self.assertEqual(len(parse_constituents(csv_bytes(values).replace(b',EQ,',b',RR,',1))),501)
        for raw in (b'<html>unavailable</html>',csv_bytes({'TEST':'INE000000000'}),
                    csv_bytes(values)+b'Test,Test,TEST,EQ,INE000000000\n',
                    csv_bytes(values).replace(b',EQ,',b',ZZ,',1)):
            with self.subTest(raw=raw[:30]),self.assertRaises(ValueError):parse_constituents(raw)
        values['EXTRA']='INE000000000'
        with self.assertRaises(ValueError):parse_constituents(csv_bytes(values))

    def test_exact_identity_and_invalid_stale_future_membership(self):
        with connect(self.path) as db:
            self.assertEqual(allowed_symbols(db,'trader',self.clock),{'TEST'})
            db.execute("UPDATE instruments SET isin='INE999999999'")
            self.assertEqual(allowed_symbols(db,'trader',self.clock),set())
            original=get_state(db,KEY)
            for changes in ({'fetched_at':(self.clock-timedelta(days=1)).isoformat()},
                            {'fetched_at':(self.clock+timedelta(seconds=1)).isoformat()},
                            {'source':'https://example.com'}, {'members':{}}, {'fetched_at':'bad'}):
                set_state(db,KEY,{**original,**changes})
                with self.assertRaisesRegex(ValueError,'new buys wait'):membership(db,self.clock)
            set_state(db,KEY,original)
            self.assertIsNone(allowed_symbols(db,'investor',self.clock))

    def test_failed_refresh_preserves_list_success_replaces_atomically(self):
        with connect(self.path) as db:original=get_state(db,KEY)
        response=MagicMock();response.__enter__.return_value=response
        response.iter_content.return_value=[b'not a list']
        with patch('marketdesk.buy_universe.requests.get',return_value=response):
            self.assertFalse(refresh_membership(self.path))
        with connect(self.path) as db:self.assertEqual(get_state(db,KEY),original)
        response.iter_content.return_value=[csv_bytes(members(('NEW',)))]
        with patch('marketdesk.buy_universe.requests.get',return_value=response):
            self.assertTrue(refresh_membership(self.path))
        with connect(self.path) as db:
            updated=get_state(db,KEY);self.assertIn('NEW',updated['members']);self.assertEqual(len(updated['sha256']),64)

    def test_refresh_skips_verified_day_and_throttles_missing_list(self):
        job=MembershipRefresh(self.path)
        with patch('marketdesk.buy_universe.threading.Thread') as thread:
            job.start_if_due();thread.assert_not_called()
            with connect(self.path) as db:db.execute('DELETE FROM state WHERE key=?',(KEY,))
            job.start_if_due();thread.return_value.start.assert_called_once()
            thread.return_value.is_alive.return_value=False
            job.start_if_due();thread.return_value.start.assert_called_once()


class UniverseExecutionTests(unittest.TestCase):
    def test_member_fills_nonmember_and_mismatched_identity_block_all_engines(self):
        for kind in ('trader','investor','claude'):
            with self.subTest(kind=kind),fixture(kind) as (f,p,order):
                with connect(f.path) as db:
                    db.execute("UPDATE instruments SET isin='INE999999999' WHERE symbol='TEST'")
                with self.assertRaisesRegex(ValueError,'outside the verified Nifty 500'):order()
                with connect(f.path) as db:seed(db,f.clock,('ANOTHER',))
                with self.assertRaisesRegex(ValueError,'outside the verified Nifty 500'):order()
                with connect(f.path) as db:seed(db,f.clock)
                self.assertEqual(order()['quantity'],1)

    def test_removal_during_depth_fetch_blocks_fill_without_mutation(self):
        for kind in ('trader','investor','claude'):
            with self.subTest(kind=kind),fixture(kind) as (f,p,order):
                with connect(f.path) as db:cash=db.execute('SELECT cash FROM portfolios WHERE name=?',(p,)).fetchone()[0]
                def changed(path,symbol):
                    book=fixture_book(path,symbol)
                    with connect(path) as db:seed(db,f.clock,('ANOTHER',))
                    return book
                with patch('marketdesk.paper.fetch_book',side_effect=changed):
                    with self.assertRaisesRegex(ValueError,'outside the verified Nifty 500'):order()
                with connect(f.path) as db:
                    for table in ('trades','positions','depth_used'):
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT cash FROM portfolios WHERE name=?',(p,)).fetchone()[0],cash)
                    if kind=='claude':self.assertEqual(db.execute('SELECT COUNT(*) FROM claude_positions').fetchone()[0],0)

    def test_scan_filters_candidates_without_deleting_research_stocks(self):
        for kind in ('trader','investor','claude'):
            with self.subTest(kind=kind),fixture(kind) as (f,p,order):
                f.add_stock('OUTSIDE')
                if kind=='claude':
                    f.c.histories={'TEST':[f.signal],'OUTSIDE':[{**f.signal,'rank':99}]}
                    f.c.loaded=time.monotonic();f.c.loaded_day='2026-10-13'
                    with patch.object(f.c,'refresh_benchmark'):status=f.c.scan(f.clock)
                else:
                    f.run_cycle();status=f.engine.snapshot()['status'][p]
                self.assertEqual(status['candidates'],1)
                with connect(f.path) as db:
                    self.assertEqual([r[0] for r in db.execute('SELECT symbol FROM trades')],['TEST'])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM instruments').fetchone()[0],2)
                    summary=universe_status(db,p,f.clock)
                    self.assertEqual(summary['matched'],1);self.assertEqual(summary['data_ready'],1)

    def test_existing_nonmember_exits_when_membership_missing(self):
        for kind in ('trader','investor','claude'):
            with self.subTest(kind=kind),fixture(kind) as (f,p,order):
                order()
                with connect(f.path) as db:db.execute('DELETE FROM state WHERE key=?',(KEY,))
                if kind=='claude':
                    f.price(90);f.c.histories={'TEST':[f.signal]};f.c.loaded=time.monotonic();f.c.loaded_day='2026-10-13'
                    with patch.object(f.c,'refresh_benchmark'):f.c.scan(f.clock)
                else:f.set_price('TEST',90);f.run_cycle()
                with connect(f.path) as db:
                    self.assertEqual([r[0] for r in db.execute('SELECT side FROM trades ORDER BY rowid')],['BUY','SELL'])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM instruments').fetchone()[0],1)

    def test_missing_list_blocks_buy_and_paused_manual_buy_keeps_filter(self):
        with fixture('investor') as (f,p,order):
            with connect(f.path) as db:db.execute('DELETE FROM state WHERE key=?',(KEY,))
            with self.assertRaisesRegex(ValueError,'new buys wait'):order()
            f.engine.configure(p,False)
            with connect(f.path) as db:seed(db,f.clock,('ANOTHER',))
            with self.assertRaisesRegex(ValueError,'outside the verified Nifty 500'):
                trade(f.path,dict(portfolio=p,symbol='TEST',side='BUY',quantity=1,request_id=str(uuid.uuid4())),f.clock)
