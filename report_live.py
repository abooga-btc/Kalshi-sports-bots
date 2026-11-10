#!/usr/bin/env python3
"""Live trading report. Read-only: one balance + positions call (no orders). Usage: python3 report_live.py [--offline]"""
import argparse
import os

from bot.config import ROOT, load_config
from bot.envfile import load_dotenv
from bot.report_live import render

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="don't call Kalshi; files only")
    a = ap.parse_args()
    bal = pos = err = None
    if not a.offline:
        try:
            from bot.kalshi import KalshiClient, load_private_key_from_env
            load_dotenv(ROOT / ".env")
            cfg = load_config()
            k = KalshiClient(cfg.kalshi_base_url, cfg.kalshi_trade_base_url,
                             (os.environ.get("KALSHI_API_KEY_ID") or "").strip(), load_private_key_from_env())
            bal = k.balance_dollars()
            pos = k.positions()
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:150]}"
    print(render(ROOT, bal, pos, err))
