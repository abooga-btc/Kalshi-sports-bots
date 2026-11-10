"""Offline tests for the multi-variant simulator: fees, variant rules, maker fills, hold-to-settle, CLV, report."""
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot.config import Config
from bot.kalshi import parse_market
from bot.multi import VARIANTS, Engine, Portfolio, evaluate_side, migrate_legacy
from bot.odds import consensus_from_event
from bot.report import render, variant_metrics
from bot.strategy import maker_fee, odds_min_interval, taker_fee
from tests.test_bot_flow import FakeKalshi
from tests.test_logic import kmarket, odds_event

V = {v.name[0]: v for v in VARIANTS}
NO_NEWS = {"total": 0.0, "neg_self": 0.0, "neg_opp": 0.0, "blocked": False}
NO_CONTRA = {"total": 0.0, "book_c": 0.0, "flow_c": 0.0, "overpay_c": 0.0, "drift_c": 0.0}


def side(bid, ask, fair, contra=None, news=None, t="KXNBAGAME-26NOV01TORLAL-TOR"):
    mk = parse_market(kmarket(t, "Toronto", bid, ask), "KXNBAGAME")
    return {"mk": mk, "team": "Toronto Raptors", "fair": fair, "contra": contra or dict(NO_CONTRA),
            "news": news or dict(NO_NEWS)}


class Fees(unittest.TestCase):
    def test_kalshi_formulas(self):
        self.assertEqual(taker_fee(20, 0.50), 0.35)          # 0.07*20*.25 = 0.35
        self.assertEqual(taker_fee(25, 0.40), 0.42)          # 0.07*25*.24 = 0.42
        self.assertEqual(maker_fee(20, 0.50), 0.09)          # 0.0175*20*.25 = 0.0875 -> ceil to 0.09
        self.assertEqual(taker_fee(1, 0.50), 0.02)           # rounding up dominates small orders

    def test_pacing_until_horizon(self):
        now = datetime(2026, 10, 29, 19, 0, tzinfo=timezone.utc)
        until = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
        self.assertGreater(odds_min_interval(495, 20, now, until=until), odds_min_interval(495, 20, now))
        self.assertAlmostEqual(odds_min_interval(495, 20, now, until=until), (until - now).total_seconds() / 475, 0)


class VariantRules(unittest.TestCase):
    cfg = Config()

    def test_raw_vs_fee_aware_edge(self):
        s = side(0.43, 0.44, 0.50)                           # raw edge 6c at the ask
        b, c = evaluate_side(V["B"], self.cfg, s), evaluate_side(V["C"], self.cfg, s)
        self.assertTrue(b["ok"])
        # net = 6 - entry fee/ct - exit fee/ct at fair - half spread = 6 - ~1.73 - ~1.75 - 0.5 ~= 2.0 < 3
        self.assertAlmostEqual(c["net_edge_c"], 6 - c["fee"] / c["contracts"] * 100
                               - taker_fee(c["contracts"], .5) / c["contracts"] * 100 - 0.5, places=2)
        self.assertFalse(c["ok"])
        self.assertTrue(evaluate_side(V["C"], self.cfg, side(0.41, 0.42, 0.50))["ok"])   # 8c raw survives fees

    def test_news_block_applies_to_every_variant(self):
        blocked = dict(NO_NEWS, blocked=True, neg_self=1.0, total=-2.0)
        for v in VARIANTS:
            self.assertFalse(evaluate_side(v, self.cfg, side(0.30, 0.31, 0.50, news=blocked))["ok"], v.name)

    def test_contrarian_only_counts_in_A_and_E(self):
        strong = dict(NO_CONTRA, total=4.0, flow_c=2.5, overpay_c=1.5)
        s = side(0.46, 0.47, 0.50, contra=strong)            # 3c raw edge + 4c contrarian
        a = evaluate_side(V["A"], self.cfg, s)
        self.assertTrue(a["ok"]); self.assertIn("flow", a["tags"]); self.assertIn("relaxed_floor", a["tags"])
        self.assertFalse(evaluate_side(V["B"], self.cfg, s)["ok"])
        self.assertFalse(evaluate_side(V["D"], self.cfg, s)["ok"])

    def test_maker_price_and_fee(self):
        e = evaluate_side(V["E"], self.cfg, side(0.40, 0.43, 0.52))
        self.assertAlmostEqual(e["price"], 0.41)
        self.assertEqual(e["fee"], maker_fee(e["contracts"], 0.41))
        self.assertIn("maker", e["tags"])
        e1 = evaluate_side(V["E"], self.cfg, side(0.40, 0.41, 0.52))   # 1c spread -> join bid, don't cross
        self.assertAlmostEqual(e1["price"], 0.40)

    def test_D_uses_news_boost(self):
        news = dict(NO_NEWS, total=2.0, neg_opp=1.0)
        s = side(0.39, 0.40, 0.475, news=news)              # net edge ~3.5c, +2 news -> 5.5 >= 5
        d = evaluate_side(V["D"], self.cfg, s)
        self.assertTrue(d["ok"], d["reason"]); self.assertIn("news_boost", d["tags"])
        self.assertFalse(evaluate_side(V["D"], self.cfg, side(0.39, 0.40, 0.475))["ok"])


class PortfolioSim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.T = "KXNBAGAME-26NOV01TORLAL-TOR"

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, bid, ask, fair=0.60):
        return {"game": "Toronto @ LA", "live": False, "game_block": None, "sides": [side(bid, ask, fair)]}

    def test_maker_fill_model(self):
        p = Portfolio(V["E"], self.cfg, Path(self.tmp.name))
        mk = side(0.40, 0.43, 0.60)["mk"]
        p.consider([self.row(0.40, 0.43)])
        o = p.pending[self.T]
        self.assertAlmostEqual(o["price"], 0.41)
        self.assertAlmostEqual(p.ledger.spent_today(), o["cost"])          # reserved
        ts = lambda dt: datetime.fromtimestamp(o["placed_at"] + dt, timezone.utc).isoformat()
        trades = [{"side": "yes", "yes_price": 0.41, "count": 999, "ts": ts(5)},     # buyer-initiated: no fill
                  {"side": "no", "yes_price": 0.42, "count": 999, "ts": ts(5)},      # above our bid: no fill
                  {"side": "no", "yes_price": 0.41, "count": 999, "ts": ts(-5)}]     # before we were placed
        p.process_pending(lambda t: mk, lambda t, s: trades, lambda t: False)
        self.assertIn(self.T, p.pending)
        trades.append({"side": "no", "yes_price": 0.40, "count": 5, "ts": ts(10)})   # 5 sold through our price
        p.process_pending(lambda t: mk, lambda t, s: trades, lambda t: False, now_ts=o["expires_at"] + 1)
        self.assertNotIn(self.T, p.pending)
        pos = p.ledger.positions[self.T]
        self.assertEqual(pos["contracts"], 5)                              # partial fill at timeout
        self.assertAlmostEqual(p.ledger.spent_today(), pos["entry_cost"])  # remainder refunded
        self.assertEqual(pos["entry_fee"], maker_fee(5, 0.41))

    def test_maker_cancel_no_fill(self):
        p = Portfolio(V["E"], self.cfg, Path(self.tmp.name))
        mk = side(0.40, 0.43, 0.60)["mk"]
        p.consider([self.row(0.40, 0.43)])
        p.process_pending(lambda t: mk, lambda t, s: [], lambda t: True)    # game started
        self.assertEqual(p.pending, {}); self.assertEqual(p.ledger.positions, {})
        self.assertAlmostEqual(p.ledger.spent_today(), 0.0)

    def test_maker_fill_when_ask_reaches_price(self):
        p = Portfolio(V["E"], self.cfg, Path(self.tmp.name))
        p.consider([self.row(0.40, 0.43)])
        n = p.pending[self.T]["contracts"]
        p.process_pending(lambda t: side(0.39, 0.41, 0.6)["mk"], lambda t, s: [], lambda t: False)
        self.assertEqual(p.ledger.positions[self.T]["contracts"], n)

    def test_hold_to_settle_ignores_take_profit(self):
        f, c = (Portfolio(V[k], self.cfg, Path(self.tmp.name)) for k in "FC")
        for p in (f, c):
            p.consider([self.row(0.40, 0.41)])
            self.assertIn(self.T, p.ledger.positions)
        up = side(0.60, 0.61, 0.6)["mk"]
        for p in (f, c):
            p.manage_exits(lambda t: up)
        self.assertIn(self.T, f.ledger.positions)        # F holds
        self.assertNotIn(self.T, c.ledger.positions)     # C took profit
        settled = parse_market({**kmarket(self.T, "Toronto", 0.99, 1.0, status="finalized"), "result": "yes"},
                               "KXNBAGAME")
        f.manage_exits(lambda t: settled)
        cl = f.ledger.state["closed"][-1]
        self.assertEqual(cl["reason"], "settled_yes")
        self.assertAlmostEqual(cl["pnl"], cl["contracts"] - cl["entry_cost"], places=4)

    def test_limits_are_per_variant(self):
        p = Portfolio(V["B"], replace(self.cfg, max_open_positions=1), Path(self.tmp.name))
        r2 = {"game": "g2", "live": False, "game_block": None,
              "sides": [side(0.40, 0.41, 0.60, t="KXNBAGAME-26NOV02BOSNYK-BOS")]}
        d = p.consider([self.row(0.40, 0.41), r2])
        self.assertEqual(len(p.ledger.positions), 1)
        self.assertTrue(any("max_open_positions" in x["reason"] for x in d))


class FakeOdds:
    def __init__(self, games):
        self.games, self.calls, self.remaining, self.used, self.last_cost = games, 0, 400, 100, 1

    def h2h_odds(self, sport, now):
        self.calls += 1
        self.remaining -= 1
        return self.games


class EngineFlow(unittest.TestCase):
    def test_one_fetch_feeds_all_variants_and_clv_report(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(series=["KXNBAGAME"], sports=["basketball_nba"], shared_dir=f"{d}/data/shared",
                         variants_dir=f"{d}/data/variants", news_enabled=False)
            start = datetime.now(timezone.utc) + timedelta(hours=2)
            T = "KXNBAGAME-26NOV01TORLAL"
            k = FakeKalshi({f"{T}-TOR": ("Toronto", .38, .40, "active", ""),
                            f"{T}-LAL": ("Los Angeles L", .60, .62, "active", "")})
            g = consensus_from_event(odds_event("Los Angeles Lakers", "Toronto Raptors",
                                                {"a": (2.2, 1.8), "b": (2.25, 1.75), "c": (2.2, 1.8)},
                                                start=start), "basketball_nba")
            with mock.patch.dict(os.environ, {"ODDS_API_KEY": "test"}), \
                    mock.patch("tests.test_bot_flow.START", start - timedelta(minutes=5)):
                eng = Engine(cfg)
                eng.kalshi, eng.odds = k, FakeOdds([g])
                eng.odds.remaining = 400
                out, _ = eng.step()
                self.assertEqual(eng.odds.calls, 1)                 # bootstrap fetch, shared by all variants
                eng.step()
                self.assertEqual(eng.odds.calls, 1)                 # pacing: no second paid call
            names = {p.spec.name: p for p in eng.portfolios}
            for n in ("A_current", "B_odds_only_5c", "C_odds_only_3c_fee_aware", "D_no_contrarian",
                      "F_hold_to_settle"):
                self.assertIn(f"{T}-TOR", names[n].ledger.positions, n)
            self.assertIn(f"{T}-TOR", names["E_upgraded"].pending)
            self.assertTrue((Path(d) / "data/variants/A_current/state.json").exists())
            self.assertIn(f"{T}-TOR", eng.closing)
            # CLV after the game starts: close line .40 last; entry at .40 ask -> 0c vs Kalshi
            eng.closing[f"{T}-TOR"]["start"] = time.time() - 1
            eng.closing_path.write_text(json.dumps(eng.closing))
            st = names["A_current"].ledger.state
            st["positions"][f"{T}-TOR"]["opened_at"] = time.time() - 100     # entered pregame
            m = variant_metrics(st, {f"{T}-TOR": {"bid": 0.38}}, eng.closing)
            self.assertEqual(m["clv_n"], 1); self.assertAlmostEqual(m["clv_kalshi"], 0.0)
            self.assertGreater(m["clv_fair"], 10)
            pos = st["positions"][f"{T}-TOR"]
            exp = pos["contracts"] * .38 - taker_fee(pos["contracts"], .38) - pos["entry_cost"]
            self.assertAlmostEqual(m["unrealized"], exp, places=4)
            txt = render(Path(d))
            self.assertIn("F_hold_to_settle", txt)

    def test_migrate_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(shared_dir=f"{d}/data/shared", variants_dir=f"{d}/data/variants")
            (Path(d) / "data").mkdir()
            legacy = {"positions": {"X-MIA": {"contracts": 39, "entry_price": .24, "entry_cost": 9.86,
                                              "event_ticker": "X", "team": "Miami", "opened_at": 1}},
                      "spend_by_day": {"2026-10-29": 9.86}, "realized_pnl": 0.0, "closed": [],
                      "odds_last_fetch": {"nfl": 5.0}, "odds_remaining": 493}
            (Path(d) / "data/state.json").write_text(json.dumps(legacy))
            with mock.patch.object(Config, "path", lambda self, rel: Path(rel) if Path(rel).is_absolute()
                                   else Path(d) / rel):
                self.assertIn("1 open", migrate_legacy(cfg))
                self.assertEqual(migrate_legacy(cfg), "nothing to migrate")
            a = json.loads((Path(d) / "data/variants/A_current/state.json").read_text())
            self.assertIn("X-MIA", a["positions"])
            self.assertEqual(json.loads((Path(d) / "data/shared/state.json").read_text())["odds_remaining"], 493)
            self.assertFalse((Path(d) / "data/state.json").exists())


if __name__ == "__main__":
    unittest.main()
