#!/usr/bin/env python3
"""2022–2026 candle-proxy backtest for order-flow-reversal/v1; no historical order book exists."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from backtest_experimental_intraday import main
from marketdesk.signal_baskets.names import ORDER_FLOW_REVERSAL
if __name__=='__main__':main(ORDER_FLOW_REVERSAL)
