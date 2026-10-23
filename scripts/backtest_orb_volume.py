#!/usr/bin/env python3
"""Backtest the ORB + volume paper basket on the local cached 5-minute NSE candles.

Usage: python3 scripts/backtest_orb_volume.py [--years 2022 2023 ...] [--params '{}']
The shared simulator applies completed-candle entries, next-bar fills, stop/target management,
Upstox intraday fees, monthly fee and daily-loss limits. The archive is a partial universe and
contains no historical order-book depth, so the 0.3% live chase/depth check cannot be reproduced.
"""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
from backtest_experimental_intraday import main
from marketdesk.signal_baskets.names import ORB_VOLUME_BREAKOUT

if __name__=='__main__':
    main(ORB_VOLUME_BREAKOUT)
