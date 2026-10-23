"""Signal basket names shared with the paper ledger, the auto-trading loop and the order audit.
No imports, so paper.py, autotrading.py and order_audit.py can depend on it without import cycles."""

NEWS_MOMENTUM='news_momentum'
NEWS_CATALYST='news_catalyst'
EMA_PRICE_ACTION='ema_price_action'
VINAY_INTRADAY='vinay_intraday'
LIQUIDITY_SWEEP='liquidity_sweep_reversal'
ORDER_FLOW_REVERSAL='order_flow_reversal'
VOLATILITY_COMPRESSION='volatility_compression_breakout'
ORB_VOLUME_BREAKOUT='orb_volume_breakout'
SECTOR_RELATIVE_STRENGTH='sector_relative_strength'
NEW_INTRADAY_BASKETS=(LIQUIDITY_SWEEP,ORDER_FLOW_REVERSAL,VOLATILITY_COMPRESSION,ORB_VOLUME_BREAKOUT)
NEW_PAPER_BASKETS=(*NEW_INTRADAY_BASKETS,SECTOR_RELATIVE_STRENGTH)
SIGNAL_BASKETS=(NEWS_MOMENTUM,NEWS_CATALYST,EMA_PRICE_ACTION,VINAY_INTRADAY,*NEW_PAPER_BASKETS)
# Baskets that trade MIS-style and square off the same session (intraday charges, shared 5-minute candles).
INTRADAY_BASKETS=(EMA_PRICE_ACTION,VINAY_INTRADAY,*NEW_INTRADAY_BASKETS)
TITLES={NEWS_MOMENTUM:'Verified News Momentum Trader',NEWS_CATALYST:'Fundamental News Catalyst Investor',EMA_PRICE_ACTION:'EMA + Price Action Trader',VINAY_INTRADAY:'Vinay Intraday Trader',
 LIQUIDITY_SWEEP:'Liquidity Sweep Reversal',ORDER_FLOW_REVERSAL:'Order Flow Reversal',VOLATILITY_COMPRESSION:'Volatility Compression Breakout',
 ORB_VOLUME_BREAKOUT:'ORB Volume Breakout',SECTOR_RELATIVE_STRENGTH:'Sector Relative Strength'}
STRATEGY_VERSIONS={NEWS_MOMENTUM:'news-momentum/v1',NEWS_CATALYST:'news-catalyst/v1',EMA_PRICE_ACTION:'ema-price-action/v1',VINAY_INTRADAY:'vinay-intraday/v1',
 LIQUIDITY_SWEEP:'liquidity-sweep-reversal/v1',ORDER_FLOW_REVERSAL:'order-flow-reversal/v1',VOLATILITY_COMPRESSION:'volatility-compression-breakout/v1',
 ORB_VOLUME_BREAKOUT:'orb-volume-breakout/v1',SECTOR_RELATIVE_STRENGTH:'sector-relative-strength/v1'}
SIGNAL_CAPITAL=100_000
