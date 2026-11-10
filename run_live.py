#!/usr/bin/env python3
"""LIVE runner (REAL ORDERS) for F_hold_to_settle + D_no_contrarian. Requires --live. Stop: touch data/live/KILL"""
import sys

from bot.live import main

if __name__ == "__main__":
    sys.exit(main())
