import socket,ssl,tempfile,threading,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import Mock,patch
from marketdesk.feed import connect_feed_socket,Feed
from marketdesk.storage import init,connect,get_state
from marketdesk.analytics import listing

class FeedConnectionTests(unittest.TestCase):
    url='wss://feed.upstox.com/feeds?code=private-one-time-code'
    def test_status_save_failure_never_kills_the_feed_thread(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'f.sqlite3';init(path);feed=Feed(path)
            with patch('marketdesk.feed.connect',side_effect=sqlite3.OperationalError('unable to open database file')):
                feed.update(status='error',error='Upstox connection failed')                # would raise inside the reconnect loop
            self.assertEqual(feed.state['status'],'error')
    def test_ipv6_timeout_falls_back_to_ipv4_with_verified_tls(self):
        v6=Mock();v6.connect.side_effect=TimeoutError('unreachable IPv6')
        v4=Mock();secure=Mock();context=Mock();context.wrap_socket.return_value=secure
        addresses=[(socket.AF_INET6,socket.SOCK_STREAM,6,'',('2001:db8::1',443,0,0)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.0.2.1',443))]
        with patch('socket.getaddrinfo',return_value=addresses),patch('socket.socket',side_effect=[v6,v4]),patch('marketdesk.feed.ssl.create_default_context',return_value=context) as tls:
            self.assertIs(connect_feed_socket(self.url),secure)
        v6.close.assert_called_once();v4.connect.assert_called_once_with(('192.0.2.1',443))
        context.wrap_socket.assert_called_once_with(v4,server_hostname='feed.upstox.com')
        self.assertTrue(tls.call_args.kwargs['cafile'])
    def test_bad_certificate_fails_closed(self):
        raw=Mock();context=Mock();context.wrap_socket.side_effect=ssl.SSLCertVerificationError('invalid certificate')
        with patch('marketdesk.feed.network.create_connection',return_value=raw),patch('marketdesk.feed.ssl.create_default_context',return_value=context):
            with self.assertRaises(ssl.SSLCertVerificationError):connect_feed_socket(self.url)
        raw.close.assert_called_once()
    def test_untrusted_authorization_url_is_rejected_without_network(self):
        for url in ['ws://feed.upstox.com/','wss://upstox.com.attacker.test/','wss://feed.upstox.com:444/','wss://user:pass@feed.upstox.com/']:
            with patch('marketdesk.feed.network.create_connection') as connect_socket:
                with self.assertRaises(ValueError):connect_feed_socket(url)
                connect_socket.assert_not_called()
    def test_stop_during_connect_closes_socket(self):
        stop=threading.Event();raw=Mock()
        def connected(*args,**kwargs):stop.set();return raw
        with patch('marketdesk.feed.network.create_connection',side_effect=connected):
            with self.assertRaises(InterruptedError):connect_feed_socket(self.url,stop)
        raw.close.assert_called_once()
    def test_closed_market_snapshot_stays_last_trade(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'feed.sqlite3';init(path)
            with connect(path) as db:db.execute('INSERT INTO instruments VALUES(?,?,?,?,?)',('TEST','NSE_EQ|TEST','TEST','Test','Test'))
            feed=Feed(path);feed.update(status='connected')
            feed.message({'marketInfo':{'segmentStatus':{'NSE_EQ':'CLOSING_END'}}},{'NSE_EQ|TEST':'TEST'})
            stamp=int((datetime.now(timezone.utc)-timedelta(days=2)).timestamp()*1000)
            feed.message({'feeds':{'NSE_EQ|TEST':{'ltpc':{'ltp':100,'ltt':stamp,'cp':99}}}},{'NSE_EQ|TEST':'TEST'})
            rows=listing(path)
            self.assertEqual(rows[0]['quote_status'],'LAST TRADE');self.assertEqual(rows[0]['quote']['price'],100)
            with connect(path) as db:self.assertEqual(get_state(db,'stream')['messages_received'],2)

if __name__=='__main__':unittest.main()
