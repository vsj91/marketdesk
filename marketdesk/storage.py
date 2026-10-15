import json, sqlite3, math
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / 'data' / 'marketdesk.sqlite3'
def now(): return datetime.now(timezone.utc).isoformat()
def number(v):
    if v is None or isinstance(v, bool): return None
    try:
        x=float(str(v).replace(',',''))
        return x if math.isfinite(x) else None
    except (ValueError,TypeError): return None

def connect(path=DEFAULT_DB):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=30)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    return db

def init(path=DEFAULT_DB):
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS instruments(symbol TEXT PRIMARY KEY, instrument_key TEXT UNIQUE NOT NULL, isin TEXT, name TEXT, sector TEXT);
        CREATE TABLE IF NOT EXISTS candles(symbol TEXT NOT NULL REFERENCES instruments(symbol), ts TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, source TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(symbol,ts));
        CREATE TABLE IF NOT EXISTS fundamentals(symbol TEXT PRIMARY KEY REFERENCES instruments(symbol), details TEXT NOT NULL, observed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payloads(symbol TEXT NOT NULL REFERENCES instruments(symbol), endpoint TEXT NOT NULL, payload TEXT NOT NULL, http_status INTEGER, error TEXT, observed_at TEXT NOT NULL, source_url TEXT, PRIMARY KEY(symbol,endpoint));
        CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY REFERENCES instruments(symbol), price REAL NOT NULL, previous_close REAL, trade_time INTEGER NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS issues(symbol TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(symbol,kind,detail));
        CREATE TABLE IF NOT EXISTS quality(symbol TEXT PRIMARY KEY,report TEXT NOT NULL,checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY REFERENCES instruments(symbol));
        CREATE INDEX IF NOT EXISTS candles_symbol ON candles(symbol,ts);
        ''')

def set_state(db,key,value):
    db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value,allow_nan=False)))
def get_state(db,key,default=None):
    r=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else default

def store_quote(db,symbol,ltpc,received=None,*,source='upstox_websocket_v3'):
    price=number(ltpc.get('ltp')); stamp=number(ltpc.get('ltt')); cp=number(ltpc.get('cp'))
    current=datetime.now(timezone.utc).timestamp()*1000
    if price is None or price<=0 or stamp is None or stamp<=0 or stamp>current+60000:return False
    result=db.execute('''INSERT INTO quotes VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
      price=excluded.price,previous_close=excluded.previous_close,trade_time=excluded.trade_time,
      received_at=excluded.received_at,source=excluded.source WHERE excluded.trade_time>quotes.trade_time''',
      (symbol,price,cp if cp and cp>0 else None,int(stamp),received or now(),source))
    return result.rowcount>0
