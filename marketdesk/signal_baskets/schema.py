"""Tables for the three signal baskets, inside the main MarketDesk database so that a fill, its position
state and its state-machine transition commit in the same transaction as the paper trade.

Additive only (CREATE ... IF NOT EXISTS): existing tables, portfolios and ledgers are never altered or reset.

sb_news            every collected announcement: source id (unique), identity, subject, summary, filing URL,
                   publication / dissemination / retrieval timestamps, dedup key, duplicate_of, verification
sb_news_analysis   one rule-based analysis per news item: category, label, impact score, confidence, components
sb_signals         every basket decision (BUY_CANDIDATE, WATCHLIST, REJECTED, EXIT_REDUCE, SIGNAL_DETECTED,
                   ENTRY_PENDING, ENTRY_FILLED, EXPIRED, CANCELLED) with trigger, stop, target, RR, quantity,
                   score, explanation, expiry and rejection reason; (portfolio, signal_key) is unique, so a
                   scheduler retry can never create a second decision for the same event
sb_positions       per-basket open position state: stop, target, stage, entry ATR, original quantity, best price
sb_transitions     append-only state-machine log; `dedup` is unique so repeated scans log a transition once
sb_equity          equity/cash snapshots (5-minute buckets) for drawdown, utilization and the chart
sb_intraday        cached completed intraday candles for the EMA basket's universe (retention-pruned)
sb_live_bars       5-minute candles built from the live feed for every streamed stock (Vinay Intraday; retention-pruned)
sb_live_volume     last volume-traded-today snapshot per stock, for booking each live candle's volume"""
from ..storage import connect,get_state,set_state

SCHEMA='''
CREATE TABLE IF NOT EXISTS sb_news(
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_id TEXT NOT NULL, symbol TEXT NOT NULL, isin TEXT,
  company TEXT, subject TEXT, headline TEXT, url TEXT, industry TEXT, published_at TEXT NOT NULL, disseminated_at TEXT,
  retrieved_at TEXT NOT NULL, event_at TEXT, dedup_key TEXT NOT NULL, duplicate_of INTEGER, verification TEXT NOT NULL,
  attachment_text TEXT, UNIQUE(source,source_id));
CREATE INDEX IF NOT EXISTS sb_news_symbol ON sb_news(symbol,published_at);
CREATE INDEX IF NOT EXISTS sb_news_dedup ON sb_news(dedup_key);
CREATE TABLE IF NOT EXISTS sb_news_analysis(
  news_id INTEGER PRIMARY KEY REFERENCES sb_news(id), version TEXT NOT NULL, analysed_at TEXT NOT NULL, category TEXT NOT NULL,
  label TEXT NOT NULL, impact_score REAL NOT NULL, confidence REAL NOT NULL, details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sb_news_analysis_category ON sb_news_analysis(category,label);
CREATE TABLE IF NOT EXISTS sb_signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT NOT NULL, strategy_version TEXT NOT NULL, signal_key TEXT NOT NULL,
  symbol TEXT NOT NULL, isin TEXT, created_at TEXT NOT NULL, session TEXT NOT NULL, side TEXT NOT NULL DEFAULT 'long',
  status TEXT NOT NULL, entry_trigger REAL, limit_price REAL, initial_stop REAL, initial_target REAL, risk_reward REAL,
  quantity INTEGER, score REAL, explanation TEXT NOT NULL DEFAULT '', valid_from TEXT, expires_at TEXT, rejection_reason TEXT,
  news_id INTEGER, details TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, UNIQUE(portfolio,signal_key));
CREATE INDEX IF NOT EXISTS sb_signals_open ON sb_signals(portfolio,status);
CREATE INDEX IF NOT EXISTS sb_signals_recent ON sb_signals(portfolio,created_at);
CREATE TABLE IF NOT EXISTS sb_positions(
  portfolio TEXT NOT NULL, symbol TEXT NOT NULL, signal_id INTEGER, strategy_version TEXT NOT NULL, entry_session TEXT NOT NULL,
  entry_at TEXT NOT NULL, stage TEXT NOT NULL, state TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(portfolio,symbol));
CREATE TABLE IF NOT EXISTS sb_transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, portfolio TEXT NOT NULL, symbol TEXT, signal_id INTEGER, from_stage TEXT, to_stage TEXT NOT NULL,
  reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, dedup TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS sb_transitions_recent ON sb_transitions(portfolio,id);
CREATE TABLE IF NOT EXISTS sb_equity(portfolio TEXT NOT NULL, ts TEXT NOT NULL, equity REAL NOT NULL, cash REAL NOT NULL, invested REAL NOT NULL, PRIMARY KEY(portfolio,ts));
CREATE TABLE IF NOT EXISTS sb_intraday(
  symbol TEXT NOT NULL, interval INTEGER NOT NULL, ts TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY(symbol,interval,ts));
CREATE TABLE IF NOT EXISTS sb_live_bars(
  symbol TEXT NOT NULL, interval INTEGER NOT NULL, ts TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL, PRIMARY KEY(symbol,interval,ts));
CREATE TABLE IF NOT EXISTS sb_live_volume(symbol TEXT PRIMARY KEY, day TEXT NOT NULL, volume REAL NOT NULL);
'''

def init(path):
    from .names import SIGNAL_BASKETS,INTRADAY_BASKETS
    with connect(path) as db:
        db.executescript(SCHEMA)
        cfg=get_state(db,'auto_config',{})
        for name in SIGNAL_BASKETS:
            if name not in cfg:
                # Intraday baskets trade MIS-style and square off themselves, so their fee check uses intraday charges.
                cfg[name]={'enabled':True,'full_session_buying':True,'max_round_trip_fee_pct':1. if name in INTRADAY_BASKETS else 1.5,'params':{}}
        set_state(db,'auto_config',cfg)
