from .working_universe import symbol_filter
import os,threading,json,ssl,uuid,time
import socket as network
from urllib.parse import urlsplit
import requests,certifi,websocket
from google.protobuf.json_format import MessageToDict
from upstox_client.feeder.proto import MarketDataFeedV3_pb2
from .storage import connect,set_state,store_quote,now,ROOT

def load_token():
    p=ROOT/'.env'
    if p.exists():
        for line in p.read_text().splitlines():
            if line.startswith('UPSTOX_TOKEN='):return line.split('=',1)[1].strip()
    return os.getenv('UPSTOX_TOKEN','').strip() or os.getenv('UPSTOX_ACCESS_TOKEN','').strip()

def connect_feed_socket(url,stop=None):
    """Try every DNS address, including after IPv6 timeouts, then verify TLS.

    websocket-client aborts its address loop on ETIMEDOUT. Python's standard
    connector continues to the next address without altering system networking.
    The returned socket is already TLS-wrapped because WebSocketApp skips TLS
    setup for a supplied socket.
    """
    parsed=urlsplit(url)
    if parsed.scheme!='wss' or not (parsed.hostname or '').endswith('.upstox.com') or parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Unexpected authorized feed URL')
    if stop and stop.is_set():raise InterruptedError('Feed stopped')
    raw=network.create_connection((parsed.hostname,443),timeout=3)
    try:
        if stop and stop.is_set():raise InterruptedError('Feed stopped')
        raw.settimeout(20)
        context=ssl.create_default_context(cafile=certifi.where())
        secure=context.wrap_socket(raw,server_hostname=parsed.hostname)
    except BaseException:
        raw.close()
        raise
    if stop and stop.is_set():
        secure.close()
        raise InterruptedError('Feed stopped')
    return secure

class Feed:
    """Verified TLS socket; official SDK protobuf schema only, no brokerage APIs."""
    def __init__(self,path):
        self.path=path;self.socket=None;self.lock=threading.RLock();self.lifecycle=threading.RLock();self.stop_event=threading.Event();self.generation=0
        self.state={'status':'disconnected','market_status':{},'subscriptions':0};self.save()
    def save(self):
        with connect(self.path) as db:set_state(db,'stream',dict(self.state,updated_at=now()))
    def update(self,**values):
        with self.lock:self.state.update(values);self.save()
    def stop(self):
        with self.lifecycle:
            self.generation+=1;self.stop_event.set()
            if self.socket:
                try:self.socket.close()
                except Exception:pass
                self.socket=None
            self.update(status='disconnected',market_status={},subscriptions=0)
    def start(self):
        with self.lifecycle:
            self.stop();token=load_token()
            if not token:raise ValueError('Save an Upstox access token first.')
            with connect(self.path) as db:keys={r['instrument_key']:r['symbol'] for r in db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db,include_holdings=True)+' ORDER BY symbol')}
            if not keys:raise ValueError('Import instruments first.')
            if len(keys)>5000:raise ValueError('LTPC limit is 5,000 instruments; reduce the universe.')
            generation=self.generation;stop=threading.Event();self.stop_event=stop
            self.update(status='connecting',stage='authorizing',subscriptions=len(keys),mode='ltpc',error=None,retry=0)
            threading.Thread(target=self.run,args=(token,keys,generation,stop),daemon=True).start()
    def run(self,token,keys,generation,stop):
        def active():return not stop.is_set() and self.generation==generation
        last_scope=[0.0]
        def sync_scope(ws):
            with connect(self.path) as db:
                current={r['instrument_key']:r['symbol'] for r in db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db,include_holdings=True))}
            for method,names in (('unsub',sorted(set(keys)-set(current))),('sub',sorted(set(current)-set(keys)))):
                if names:ws.send(json.dumps({'guid':str(uuid.uuid4()),'method':method,'data':{'mode':'ltpc','instrumentKeys':names}}).encode(),opcode=websocket.ABNF.OPCODE_BINARY)
            keys.clear();keys.update(current);last_scope[0]=time.monotonic()
            self.update(subscriptions=len(keys))
        def on_open(ws):
            if not active():ws.close();return
            self.update(status='connected',stage='subscribing',error=None,market_status={},connected_at=now(),retry=0)
            with connect(self.path) as db:
                current={r['instrument_key']:r['symbol'] for r in db.execute('SELECT * FROM instruments WHERE '+symbol_filter(db,include_holdings=True))}
            keys.clear();keys.update(current);last_scope[0]=time.monotonic()
            self.update(subscriptions=len(keys))
            names=list(keys)
            for offset in range(0,len(names),500):
                ws.send(json.dumps({'guid':str(uuid.uuid4()),'method':'sub','data':{'mode':'ltpc','instrumentKeys':names[offset:offset+500]}}).encode(),opcode=websocket.ABNF.OPCODE_BINARY)
        def on_message(ws,data):
            if not active():return
            try:
                if time.monotonic()-last_scope[0]>30:sync_scope(ws)
                self.message(MessageToDict(MarketDataFeedV3_pb2.FeedResponse.FromString(data)),keys)
            except Exception:self.update(error='A malformed feed message was rejected.')
        def on_error(ws,error):
            if active():self.update(status='error',market_status={},error='Upstox socket failed ('+type(error).__name__+'). Retrying the connection.')
        for attempt in range(6):
            if not active():return
            secure=None
            try:
                self.update(stage='authorizing')
                response=requests.get('https://api.upstox.com/v3/feed/market-data-feed/authorize',headers={'Authorization':'Bearer '+token,'Accept':'application/json'},timeout=(10,20))
                if not active():return
                if response.status_code in (401,403):
                    self.update(status='auth_failed',error='Upstox rejected the access token or feed access. Save a fresh token.',market_status={});return
                response.raise_for_status();payload=response.json()
                url=(payload.get('data') or {}).get('authorized_redirect_uri','');parsed=urlsplit(url)
                if payload.get('status')!='success' or parsed.scheme!='wss' or not (parsed.hostname or '').endswith('.upstox.com'):raise ValueError('Unexpected authorized feed URL')
                # Authorization URI can contain credentials. Never log or return it.
                websocket.setdefaulttimeout(20)
                self.update(stage='connecting_tcp_tls')
                secure=connect_feed_socket(url,stop)
                if not active():return
                self.update(stage='websocket_handshake',address_family=secure.family.name)
                socket=websocket.WebSocketApp(url,on_open=on_open,on_message=on_message,on_error=on_error,socket=secure)
                with self.lifecycle:
                    if not active():return
                    self.socket=socket
                socket.run_forever(sslopt={'cert_reqs':ssl.CERT_REQUIRED,'check_hostname':True,'ca_certs':certifi.where()},ping_interval=20,ping_timeout=10)
            except Exception as error:
                if active():self.update(status='error',market_status={},error='Upstox connection failed during '+self.state.get('stage','connection')+' ('+type(error).__name__+').')
            finally:
                if secure is not None:secure.close()
            if not active():return
            self.update(status='reconnecting',market_status={},retry=attempt+1)
            if stop.wait(min(5*2**attempt,60)):return
        if active():self.update(status='stopped',market_status={},error='Reconnect attempts exhausted. Check connection and restart the feed.')
    def message(self,message,keys):
        accepted=0
        with connect(self.path) as db:
            for key,value in message.get('feeds',{}).items():
                symbol=keys.get(key)
                if symbol and isinstance(value,dict):accepted+=store_quote(db,symbol,value.get('ltpc') or {})
        extra={'last_message':now(),'stage':'receiving','messages_received':self.state.get('messages_received',0)+1}
        market=message.get('marketInfo',{}).get('segmentStatus')
        if isinstance(market,dict):extra['market_status']=market
        if accepted:extra['last_quote_received']=now()
        self.update(**extra)
