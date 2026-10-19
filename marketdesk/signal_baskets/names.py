"""Signal basket names shared with the paper ledger, the auto-trading loop and the order audit.
No imports, so paper.py, autotrading.py and order_audit.py can depend on it without import cycles."""

NEWS_MOMENTUM='news_momentum'
NEWS_CATALYST='news_catalyst'
EMA_PRICE_ACTION='ema_price_action'
SIGNAL_BASKETS=(NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION)
TITLES={NEWS_MOMENTUM:'AI News Momentum Trader',NEWS_CATALYST:'AI News Catalyst Investor',EMA_PRICE_ACTION:'EMA + Price Action Trader'}
STRATEGY_VERSIONS={NEWS_MOMENTUM:'news-momentum/v1',NEWS_CATALYST:'news-catalyst/v1',EMA_PRICE_ACTION:'ema-price-action/v1'}
SIGNAL_CAPITAL=100_000
