#!/usr/bin/env python3
"""Print the per-variant comparison table. Usage: python3 report.py [--detail]"""
import argparse

from bot.config import ROOT
from bot.report import render

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true", help="list open/pending/closed positions per variant")
    print(render(ROOT, ap.parse_args().detail))
