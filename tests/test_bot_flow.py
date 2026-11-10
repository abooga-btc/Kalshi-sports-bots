"""Offline end-to-end flow with fake Kalshi/Odds clients: entry -> limits -> take-profit -> settle."""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from bot.config import Config
from bot.kalshi import parse_market
from bot.main import Bot
from bot.odds import consensus_from_event
from tests.test_logic import kmarket, odds_event

START = datetime.now(timezone.utc) + timedelta(hours=5)   # pregame


class FakeKalshi:
    def __init__(self, quotes):
        self.quotes = quotes   # ticker -> (sub, bid, ask, status, result)

    def open_markets(self, series):
        out = []
        for t, (sub, bid, ask, status, result) in self.quotes.items():
            if t.startswith(series) and status == "active":
                out.append(parse_market(kmarket(t, sub, bid, ask, start=START + timedelta(hours=3)), series))
        return out

    def orderbook(self, ticker, depth=10):
        return {"yes": [(0.30, 100.0)], "no": [(0.50, 100.0)]}

    def trades(self, ticker, since_ts, limit=1000):
        return []

    def market(self, ticker):
        sub, bid, ask, status, result = self.quotes[ticker]
        m = kmarket(ticker, sub, bid, ask, status=status)
        m["result"] = result
        return parse_market(m, ticker.split("-")[0])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = Config(state_file=f"{d}/s.json", trade_log_csv=f"{d}/t.csv", event_log_jsonl=f"{d}/e.jsonl",
                          series=["KXNBAGAME"], sports=["basketball_nba"])
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "test"}):
            self.bot = Bot(self.cfg)
        T = "KXNBAGAME-26NOV01TORLAL"
        self.k = FakeKalshi({f"{T}-TOR": ("Toronto", .38, .40, "active", ""),
                             f"{T}-LAL": ("Los Angeles L", .60, .62, "active", "")})
        self.bot.kalshi = self.k
        self.bot.news = None          # no network in unit tests
        g = consensus_from_event(odds_event("Los Angeles Lakers", "Toronto Raptors",
                                            {"a": (2.2, 1.8), "b": (2.25, 1.75), "c": (2.2, 1.8)}, start=START),
                                 "basketball_nba")
        self.bot.odds_cache = {"basketball_nba": (time.time(), [g])}
        self.bot.refresh_odds = lambda need, force=False: None
        self.T = T

    def tearDown(self):
        self.tmp.cleanup()

    def test_entry_tp_and_no_duplicates(self):
        report, _ = self.bot.step()
        pos = self.bot.ledger.positions
        self.assertEqual(list(pos), [f"{self.T}-TOR"])            # fair ~.45 vs ask .40 -> buy TOR only
        self.assertLessEqual(pos[f"{self.T}-TOR"]["entry_cost"], 10.0)
        self.bot.step()
        self.assertEqual(len(self.bot.ledger.positions), 1)       # no duplicate entry
        self.k.quotes[f"{self.T}-TOR"] = ("Toronto", .52, .53, "active", "")   # bid up ~30% (>15% after fees)
        self.bot.step()
        self.assertEqual(self.bot.ledger.positions, {})
        closed = self.bot.ledger.state["closed"][0]
        self.assertIn("take_profit", closed["reason"])
        self.assertGreater(closed["pnl"], 0)
        with open(self.cfg.trade_log_csv) as f:
            csv = f.read()
        self.assertIn("BUY_YES", csv); self.assertIn("SELL_YES", csv)

    def test_volume_gate_blocks_thin_game(self):
        orig = self.k.open_markets
        def thin(series):
            out = orig(series)
            for m in out:
                m.volume, m.volume_24h = 100.0, 50.0
            return out
        self.k.open_markets = thin
        report, _ = self.bot.step()
        self.assertEqual(self.bot.ledger.positions, {})
        self.assertIn("volume gate", report[0]["markets"][0]["decision"])

    def test_reentry_cooldown(self):
        self.bot.step()
        self.k.quotes[f"{self.T}-TOR"] = ("Toronto", .52, .53, "active", "")
        self.bot.step()                                            # take profit
        self.k.quotes[f"{self.T}-TOR"] = ("Toronto", .38, .40, "active", "")
        report, _ = self.bot.step()                                # edge again, but cooldown
        self.assertEqual(self.bot.ledger.positions, {})
        self.assertTrue(any("exited this game" in m["decision"] for m in report[0]["markets"]))

    def test_daily_limit_and_settlement(self):
        self.bot.cfg.max_daily_spend_usd = 5.0
        self.bot.step()
        self.assertEqual(self.bot.ledger.positions, {})           # $10 trade > $5 daily cap
        self.bot.cfg.max_daily_spend_usd = 50.0
        self.bot.step()
        self.assertEqual(len(self.bot.ledger.positions), 1)
        self.k.quotes[f"{self.T}-TOR"] = ("Toronto", 0, 0, "finalized", "no")
        self.k.quotes[f"{self.T}-LAL"] = ("Los Angeles L", 0, 0, "finalized", "yes")
        self.bot.step()
        self.assertEqual(self.bot.ledger.positions, {})
        self.assertLess(self.bot.ledger.state["realized_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
