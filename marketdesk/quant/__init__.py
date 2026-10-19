"""Quant paper basket engine: factor, risk-parity, stat-arb, trend and regime baskets on a paper ledger.

calc         pure math (z-scores, inverse-vol weights, ATR sizing, regime detection)
strategies   the five basket rules: snapshot + book -> target plan
execution    ExecutionSimulator (typical-price fills, slippage, impact, charges) and the drift band
analytics    Sharpe, Sortino, max drawdown, Ulcer index
schema       ledger tables
engine       BasketEngine: daily pipeline and JSON telemetry
data         read-only snapshots from the MarketDesk database
Paper only: nothing in this package places or routes a broker order."""
from .engine import BasketEngine
from .execution import ExecutionSimulator
