"""Ledger schema for the quant paper baskets.

Lives in its own SQLite file (default data/quant_baskets.sqlite3), separate from the live
MarketDesk portfolios, so a replay or a bad config can never touch the Trader/Investor books.

qb_baskets         one row per basket definition: strategy, JSON config, starting capital, cash,
                   schedule state (last rebalance day, last regime)
qb_positions       current holdings: shares, total cost incl. buy charges, average fill price,
                   entry day, highest close since entry (trailing stops), strategy meta and latest score
qb_orders          paper orders queued on a signal day and filled on the next session's typical price;
                   status pending -> filled | partial | cancelled
qb_fills           every simulated fill with reference price, slippage, impact, charges and realized P&L
qb_nav             one daily NAV snapshot per basket: NAV, cash, invested value, benchmark close,
                   regime, turnover and charges for the day
qb_weights         daily constituent weights: shares, close, actual weight, target weight, factor/Z score
qb_rebalance_log   the audit trail: ADD / EXIT / TRIM / RAISE / HOLD / FILL / CANCEL / REGIME / NOTE
                   with weights before/after and a human-readable reason
qb_runs            one row per (basket, session) the cron handler completed; makes reruns idempotent

A KV store maps 1:1: key `basket:{name}` for the definition, `nav:{name}:{day}`, `weights:{name}:{day}`,
`log:{name}:{day}:{seq}`, `pos:{name}:{symbol}`, `order:{id}` with the same fields as JSON values."""

SCHEMA='''
CREATE TABLE IF NOT EXISTS qb_baskets(
  name TEXT PRIMARY KEY, strategy TEXT NOT NULL, config TEXT NOT NULL, capital REAL NOT NULL, cash REAL NOT NULL,
  created_day TEXT NOT NULL, last_rebalance TEXT, state TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS qb_positions(
  basket TEXT NOT NULL REFERENCES qb_baskets(name), symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
  cost REAL NOT NULL, entry_price REAL NOT NULL, entry_day TEXT NOT NULL, high_close REAL,
  meta TEXT NOT NULL DEFAULT '{}', score TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(basket,symbol));
CREATE TABLE IF NOT EXISTS qb_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, basket TEXT NOT NULL REFERENCES qb_baskets(name), signal_day TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty INTEGER NOT NULL CHECK(qty>0),
  kind TEXT NOT NULL, reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending', fill_day TEXT);
CREATE INDEX IF NOT EXISTS qb_orders_pending ON qb_orders(basket,status);
CREATE TABLE IF NOT EXISTS qb_fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES qb_orders(id), basket TEXT NOT NULL,
  day TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, price REAL NOT NULL,
  reference_price REAL NOT NULL, slippage_bps REAL NOT NULL, impact_bps REAL NOT NULL, fee REAL NOT NULL,
  realized_pnl REAL);
CREATE TABLE IF NOT EXISTS qb_nav(
  basket TEXT NOT NULL, day TEXT NOT NULL, nav REAL NOT NULL, cash REAL NOT NULL, invested REAL NOT NULL,
  benchmark REAL, regime TEXT, turnover REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0, PRIMARY KEY(basket,day));
CREATE TABLE IF NOT EXISTS qb_weights(
  basket TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, qty INTEGER NOT NULL, close REAL NOT NULL,
  weight REAL NOT NULL, target_weight REAL, score TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(basket,day,symbol));
CREATE TABLE IF NOT EXISTS qb_rebalance_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, basket TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT, action TEXT NOT NULL,
  weight_before REAL, weight_after REAL, reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS qb_log_day ON qb_rebalance_log(basket,day);
CREATE TABLE IF NOT EXISTS qb_runs(
  basket TEXT NOT NULL, day TEXT NOT NULL, completed_at TEXT NOT NULL, summary TEXT NOT NULL, PRIMARY KEY(basket,day));
'''
