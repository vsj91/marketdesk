"""Quant basket names shared with the paper ledger and the auto-trading loop. No imports, so paper.py,
autotrading.py and order_audit.py can depend on it without import cycles."""

# Baskets that execute through paper.trade at live quotes during market hours.
LIVE_BASKETS=('smart_beta','risk_parity','stat_arb','regime')
TITLES={'smart_beta':'Multi-factor smart beta','risk_parity':'Risk parity','stat_arb':'Stat-arb mean reversion',
    'trend':'ATR trend following','regime':'Regime switching'}
LIVE_CAPITAL=100_000
