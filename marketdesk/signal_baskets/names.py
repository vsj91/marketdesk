"""Signal basket names shared with the paper ledger, the auto-trading loop and the order audit.
No imports, so paper.py, autotrading.py and order_audit.py can depend on it without import cycles."""

NEWS_MOMENTUM='news_momentum'
NEWS_CATALYST='news_catalyst'
EMA_PRICE_ACTION='ema_price_action'
VINAY_INTRADAY='vinay_intraday'
SIGNAL_BASKETS=(NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION,VINAY_INTRADAY)
# Baskets that trade MIS-style and square off the same session (intraday charges, shared 5-minute candles).
INTRADAY_BASKETS=(EMA_PRICE_ACTION,VINAY_INTRADAY)
TITLES={NEWS_MOMENTUM:'AI News Momentum Trader',NEWS_CATALYST:'AI News Catalyst Investor',EMA_PRICE_ACTION:'EMA + Price Action Trader',VINAY_INTRADAY:'Vinay Intraday Trader'}
STRATEGY_VERSIONS={NEWS_MOMENTUM:'news-momentum/v1',NEWS_CATALYST:'news-catalyst/v1',EMA_PRICE_ACTION:'ema-price-action/v1',VINAY_INTRADAY:'vinay-intraday/v1'}
SIGNAL_CAPITAL=100_000
