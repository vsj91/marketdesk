"""Signal baskets: AI News Momentum Trader, AI News Catalyst Investor and EMA + Price Action Trader.

names        basket names/titles (import-free, shared with paper.py and autotrading.py)
indicators   EMA, Wilder RSI/ATR, tick rounding, confirmed swing structure (pure)
ema_rules    EMA + price action signal, sizing and exit state machine (pure, shared with the backtest)
news         NSE announcement collection, normalization, dedup and rule-based impact analysis
news_rules   news momentum and catalyst investor decisions (pure, shared with the backtest)
schema       additive tables in the main database
engine       live 15-second execution through paper.trade (depth-limited IOC simulation)
runner       background data collection and signal generation
report       read-only payload for the Paper baskets screen
Paper only: nothing in this package places or routes a broker order."""
