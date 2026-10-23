#!/usr/bin/env python3
"""2022–2026 historical candle proxy backtest for liquidity-sweep-reversal/v1."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from backtest_experimental_intraday import main
from marketdesk.signal_baskets.names import LIQUIDITY_SWEEP
if __name__=='__main__':main(LIQUIDITY_SWEEP)
