"""Offline tests for the LIVE runner (bot/live.py). All HTTP is mocked; nothing touches the real account."""
import os
import pickle
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

from bot.config import Config
from bot.kalshi import KalshiClient, KalshiError, load_private_key_from_env, normalize_pem, parse_market
from bot.live import LIVE_VARIANTS, LiveEngine
from bot.odds import consensus_from_event
from tests.test_logic import kmarket, odds_event

T = "KXNBAGAME-26NOV01TORLAL"


def rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


class FakeExchange:
    """Public market data + signed trading endpoints, in memory."""

    def __init__(self, quotes, start):
        self.quotes, self.start = quotes, start      # ticker -> (sub, bid, ask, status, result)
        self.bal = 1000.00
        self.pos = {}                                 # ticker -> contracts
        self.orders = {}                              # order_id -> order
        self.fills = {}                               # order_id -> [fill]
        self.sent = []
        self.fill_ratio = 1.0
        self.fail_post = None                         # exception to raise on POST (after/before placing)
        self.post_reaches_exchange = True
        self.fail_positions = False
        self.fills_visible = True
        self.foreign = {}                             # extra account positions
        self.n = 0

    # public
    def open_markets(self, series):
        return [parse_market(kmarket(t, sub, bid, ask, start=self.start + timedelta(hours=3)), series)
                for t, (sub, bid, ask, st, res) in self.quotes.items() if t.startswith(series) and st == "active"]

    def market(self, t):
        sub, bid, ask, st, res = self.quotes[t]
        m = kmarket(t, sub, bid, ask, status=st)
        m["result"] = res
        return parse_market(m, t.split("-")[0])

    def orderbook(self, t, depth=10):
        raise AssertionError("live runner must not fetch orderbooks (contrarian disabled)")

    def trades(self, t, since, limit=1000):
        return []

    # signed
    def balance_dollars(self):
        return self.bal

    def positions(self, prefixes=("KXNFLGAME-", "KXNBAGAME-")):
        if self.fail_positions:
            raise KalshiError("GET /portfolio/positions -> 503")
        out = {t: q for t, q in self.pos.items() if q}
        for t, q in self.foreign.items():
            out[t] = out.get(t, 0) + q
        return out

    def place_ioc(self, ticker, side, count, price, reduce_only=False, client_order_id=None):
        self.sent.append({"ticker": ticker, "side": side, "count": count, "price": price,
                          "reduce_only": reduce_only, "coid": client_order_id})
        if self.fail_post and not self.post_reaches_exchange:
            raise self.fail_post
        sub, bid, ask, st, res = self.quotes[ticker]
        self.n += 1
        oid = f"o{self.n}"
        if side == "bid":
            fill_px = ask if ask <= price + 1e-9 else None
            n = int(count * self.fill_ratio) if fill_px else 0
            if reduce_only:
                n = 0
            self.pos[ticker] = self.pos.get(ticker, 0) + n
            self.bal -= n * (fill_px or 0)
        else:
            fill_px = bid if bid >= price - 1e-9 else None
            n = min(int(count * self.fill_ratio) if fill_px else 0, self.pos.get(ticker, 0) if reduce_only else 10**9)
            self.pos[ticker] = self.pos.get(ticker, 0) - n
            self.bal += n * (fill_px or 0)
        fee = round(0.07 * n * (fill_px or 0) * (1 - (fill_px or 0)), 6)
        self.orders[oid] = {"order_id": oid, "client_order_id": client_order_id, "ticker": ticker,
                            "status": "executed" if n else "canceled", "fill_count_fp": f"{n:.2f}"}
        self.fills[oid] = [{"order_id": oid, "count_fp": f"{n:.2f}", "yes_price_dollars": f"{fill_px:.4f}",
                            "fee_cost": f"{fee:.6f}"}] if n else []
        if self.fail_post:
            raise self.fail_post
        return {"order_id": oid, "fill_count": f"{n:.2f}", "remaining_count": f"{count - n:.2f}"}

    def find_order_by_client_id(self, ticker, coid):
        return next((o for o in self.orders.values() if o["client_order_id"] == coid), None)

    def get_order(self, oid):
        return self.orders.get(oid)

    def fills_for_order(self, oid):
        fs = self.fills.get(oid, []) if self.fills_visible else []
        n = sum(float(f["count_fp"]) for f in fs)
        return {"count": n, "yes_value": sum(float(f["count_fp"]) * float(f["yes_price_dollars"]) for f in fs),
                "fees": sum(float(f["fee_cost"]) for f in fs), "n": len(fs)}


class LiveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.d = d
        self.start = datetime.now(timezone.utc) + timedelta(hours=2)
        self.cfg = Config(series=["KXNBAGAME"], sports=["basketball_nba"], shared_dir=str(d / "data/shared"),
                          variants_dir=str(d / "data/variants"), news_enabled=False)
        g = consensus_from_event(odds_event("Los Angeles Lakers", "Toronto Raptors",
                                            {"a": (2.2, 1.8), "b": (2.25, 1.75), "c": (2.2, 1.8)}, start=self.start),
                                 "basketball_nba")
        (d / "data/shared").mkdir(parents=True)
        (d / "data/shared/odds_cache.pkl").write_bytes(pickle.dumps({"basketball_nba": (time.time(), [g])}))
        self.x = FakeExchange({f"{T}-TOR": ("Toronto", .38, .40, "active", ""),
                               f"{T}-LAL": ("Los Angeles L", .60, .62, "active", "")}, self.start)
        self.eng = self.make()

    def make(self):
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": "test"}):
            eng = LiveEngine(self.cfg, client=self.x, live_root=str(self.d / "data/live"))
        eng.odds.h2h_odds = mock.Mock(side_effect=AssertionError("live runner must never call the Odds API"))
        eng.news = None
        return eng

    def tearDown(self):
        self.tmp.cleanup()

    def pf(self, name):
        return next(p for p in self.eng.portfolios if p.spec.name == name)


class KeyAndClient(unittest.TestCase):
    def test_pem_normalization_variants(self):
        from cryptography.hazmat.primitives import serialization
        pem = rsa_pem()
        body = pem.strip().splitlines()
        variants = [pem, pem.replace("\n", "\\n"), " ".join(body), body[0] + body[1] + " " + "".join(body[2:-1]) + body[-1]]
        for v in variants:
            k = serialization.load_pem_private_key(normalize_pem(v), password=None)
            self.assertEqual(k.key_size, 2048)
        with mock.patch.dict(os.environ, {"KALSHI_PRIVATE_KEY": " ".join(body)}):
            self.assertTrue(load_private_key_from_env().startswith(b"-----BEGIN PRIVATE KEY-----\n"))
        with self.assertRaises(ValueError) as cm:
            normalize_pem("not a key SECRETSTUFF")
        self.assertNotIn("SECRETSTUFF", str(cm.exception))

    def _client(self):
        return KalshiClient("https://api.elections.kalshi.com/trade-api/v2",
                            "https://api.elections.kalshi.com/trade-api/v2", "kid", normalize_pem(rsa_pem()))

    def test_signing_path_and_get_retry_and_post_single_shot(self):
        c = self._client()
        resp = lambda code, js: mock.Mock(status_code=code, text="x", json=lambda: js)
        calls = []

        def req(method, url, headers=None, json=None, params=None, timeout=None):
            calls.append((method, url, headers, params))
            return resp(500, {}) if len(calls) == 1 else resp(200, {"balance": 100000})
        c.s.request = req
        with mock.patch("time.sleep"), mock.patch.object(c, "_sign", wraps=c._sign) as sg:
            self.assertAlmostEqual(c.balance_dollars(), 1000.00)
            self.assertEqual(sg.call_args[0][2], "/trade-api/v2/portfolio/balance")   # path signed incl. prefix
        self.assertEqual(len(calls), 2)                                             # GET retried on 5xx
        posts = []

        def boom(method, url, **kw):
            posts.append(method)
            raise requests.Timeout()
        c.s.request = boom
        with self.assertRaises(KalshiError):
            c.place_ioc(f"{T}-TOR", "bid", 5, 0.40, client_order_id="abc")
        self.assertEqual(posts, ["POST"])                                           # never re-sent

    def test_positions_filter_and_fill_sum(self):
        c = self._client()
        pages = {"/trade-api/v2/portfolio/positions": {"market_positions": [
            {"ticker": "KXBTC15M-26NOV020615-15", "position_fp": "22.00"},
            {"ticker": f"{T}-TOR", "position_fp": "25.00"}, {"ticker": f"{T}-LAL", "position_fp": "0.00"}]},
            "/trade-api/v2/portfolio/fills": {"fills": [
                {"order_id": "o1", "count_fp": "10.00", "yes_price_dollars": "0.4000", "fee_cost": "0.1680"},
                {"order_id": "o1", "count_fp": "5.00", "yes_price_dollars": "0.3900", "fee_cost": "0.0832"}]}}
        c.s.request = lambda method, url, **kw: mock.Mock(status_code=200, text="x",
                                                          json=lambda: pages[url.split("api.elections.kalshi.com")[1]])
        self.assertEqual(c.positions(), {f"{T}-TOR": 25.0})                         # BTC bot ticker ignored
        fr = c.fills_for_order("o1")
        self.assertEqual(fr["count"], 15.0)
        self.assertAlmostEqual(fr["yes_value"], 5.95)
        self.assertAlmostEqual(fr["fees"], 0.2512)


class LiveFlow(LiveBase):
    def test_entries_sizing_limit_and_real_fills(self):
        self.eng.step()
        buys = [s for s in self.x.sent if s["side"] == "bid"]
        self.assertEqual(len(buys), 2)                                  # F and D each buy TOR once
        for b in buys:
            self.assertEqual(b["ticker"], f"{T}-TOR")
            self.assertEqual(b["price"], 0.40)                          # limit = evaluated ask, never higher
            self.assertFalse(b["reduce_only"])
            self.assertLessEqual(b["count"] * 0.40 + 0.07 * b["count"] * .4 * .6 + 0.01, 10.0)
        for name in LIVE_VARIANTS:
            p = self.pf(name).ledger.positions[f"{T}-TOR"]
            self.assertEqual(p["contracts"], 23.0)
            self.assertLessEqual(p["entry_cost"], 10.0)
            self.assertTrue(p["order_id"].startswith("o"))
            self.assertEqual(self.pf(name).intents, {})
        self.eng.step()
        self.assertEqual(len([s for s in self.x.sent if s["side"] == "bid"]), 2)   # no duplicate buys
        self.eng.odds.h2h_odds.assert_not_called()

    def test_partial_and_zero_fill(self):
        self.x.fill_ratio = 0.5
        self.eng.step()
        self.assertEqual(self.pf("F_hold_to_settle").ledger.positions[f"{T}-TOR"]["contracts"], 11.0)
        self.assertLess(self.pf("F_hold_to_settle").ledger.spent_today(), 5.1)
        self.tearDown(); self.setUp()
        self.x.fill_ratio = 0.0
        self.eng.step()
        for name in LIVE_VARIANTS:
            self.assertEqual(self.pf(name).ledger.positions, {})
            self.assertEqual(self.pf(name).ledger.spent_today(), 0.0)
            self.assertEqual(self.pf(name).intents, {})

    def test_post_timeout_is_reconciled_not_resent(self):
        self.x.fail_post = KalshiError("POST /portfolio/events/orders network error: Timeout")
        only_f = self.pf("F_hold_to_settle")
        self.eng.portfolios = [only_f]
        self.eng.step()
        self.assertEqual(len(self.x.sent), 1)
        self.assertEqual(only_f.ledger.positions, {})
        self.assertEqual(len(only_f.intents), 1)                        # unresolved until looked up
        self.x.fail_post = None
        self.x.fills_visible = False
        self.eng.step()                                                 # fills not visible: still unknown
        self.assertEqual(len(self.x.sent), 1)
        self.assertEqual(len(only_f.intents), 1)
        self.x.fills_visible = True
        with mock.patch("time.sleep"):
            self.eng.step()
        self.assertEqual(len(self.x.sent), 1)                           # never re-sent
        self.assertEqual(only_f.ledger.positions[f"{T}-TOR"]["contracts"], 23.0)
        self.assertEqual(only_f.intents, {})

    def test_post_error_that_never_reached_exchange_is_dropped_later(self):
        self.x.fail_post = KalshiError("POST -> 400 bad request")
        self.x.post_reaches_exchange = False
        f = self.pf("F_hold_to_settle")
        self.eng.portfolios = [f]
        self.eng.step()
        self.assertEqual(len(f.intents), 1)
        self.x.fail_post = None
        self.eng.step()                                                 # inside grace period: still blocked
        self.assertEqual(len(self.x.sent), 1)
        for i in f.intents.values():
            i["created_at"] -= 1000
        self.eng.step()                                                 # dropped, then a fresh buy is allowed
        self.assertEqual(len(self.x.sent), 2)
        self.assertIn(f"{T}-TOR", f.ledger.positions)

    def test_restart_reconciles_and_never_double_buys(self):
        self.eng.step()
        n = len(self.x.sent)
        eng2 = self.make()
        self.eng = eng2
        eng2.step()
        self.assertEqual(len(self.x.sent), n)
        self.assertEqual(eng2.guard.blocked_events, {})

    def test_untracked_exchange_position_blocks_game(self):
        self.x.foreign = {f"{T}-TOR": 7}
        self.eng.step()
        self.assertEqual(self.x.sent, [])
        self.assertIn(T, self.eng.guard.blocked_events)

    def test_positions_api_down_means_no_orders(self):
        self.x.fail_positions = True
        self.eng.step()
        self.assertEqual(self.x.sent, [])

    def test_kill_file(self):
        (self.d / "data/live").mkdir(parents=True, exist_ok=True)
        (self.d / "data/live/KILL").touch()
        self.eng.step()
        self.assertEqual(self.x.sent, [])
        import json
        self.assertTrue(json.loads((self.d / "data/live/heartbeat.json").read_text())["kill"])

    def test_balance_floor(self):
        self.x.bal = 55.0                                               # 55 - ~10 < 50
        self.eng.step()
        self.assertEqual(self.x.sent, [])
        self.x.bal = 400.0
        self.eng.step()
        self.assertEqual(len(self.x.sent), 2)

    def test_caps_max_open_and_daily_spend(self):
        f = self.pf("F_hold_to_settle")
        for i in range(5):
            f.ledger.positions[f"KXNBAGAME-26NOV0{i}AAABBB-AAA"] = {"contracts": 1, "entry_cost": 0.5, "entry_fee": 0,
                                                                   "entry_price": .5, "event_ticker": f"E{i}", "team": "x"}
            self.x.pos[f"KXNBAGAME-26NOV0{i}AAABBB-AAA"] = 1
        d = self.pf("D_no_contrarian")
        d.ledger.add_spend(45.0)
        self.x.quotes.update({t: ("A", .40, .50, "active", "") for t in f.ledger.positions})
        self.eng.step()
        self.assertEqual(self.x.sent, [])                               # F: 5 open; D: $45 + ~$10 > $50

    def test_cooldown_after_exit(self):
        d = self.pf("D_no_contrarian")
        d.ledger.state["closed"].append({"event_ticker": T, "closed_at": time.time() - 60, "pnl": 1.0})
        self.eng.portfolios = [d]
        self.eng.step()
        self.assertEqual(self.x.sent, [])

    def test_take_profit_d_sells_reduce_only_f_holds(self):
        self.eng.step()
        self.x.quotes[f"{T}-TOR"] = ("Toronto", .52, .53, "active", "")
        with mock.patch("time.sleep"):
            self.eng.step()
        sells = [s for s in self.x.sent if s["side"] == "ask"]
        self.assertEqual(len(sells), 1)
        self.assertTrue(sells[0]["reduce_only"])
        self.assertEqual(sells[0]["count"], 23)
        self.assertEqual(sells[0]["price"], 0.52)
        d, f = self.pf("D_no_contrarian"), self.pf("F_hold_to_settle")
        self.assertEqual(d.ledger.positions, {})
        self.assertIn("take_profit", d.ledger.state["closed"][0]["reason"])
        self.assertGreater(d.ledger.state["closed"][0]["pnl"], 0)
        self.assertIn(f"{T}-TOR", f.ledger.positions)                   # F holds to settlement
        self.assertEqual(self.x.pos[f"{T}-TOR"], 23)
        self.x.quotes[f"{T}-TOR"] = ("Toronto", .99, 1.0, "finalized", "yes")
        self.x.quotes[f"{T}-LAL"] = ("Los Angeles L", 0, .01, "finalized", "no")
        self.eng.step()
        self.assertEqual(f.ledger.positions, {})
        self.assertGreater(f.ledger.state["closed"][0]["pnl"], 10)

    def test_partial_take_profit_keeps_remainder(self):
        self.eng.step()
        self.x.quotes[f"{T}-TOR"] = ("Toronto", .52, .53, "active", "")
        self.x.fill_ratio = 0.5
        with mock.patch("time.sleep"):
            self.eng.step()
        d = self.pf("D_no_contrarian")
        self.assertEqual(d.ledger.positions[f"{T}-TOR"]["contracts"], 12.0)
        self.assertIn("partial", d.ledger.state["closed"][0]["reason"])

    def test_mismatch_trims_after_two_checks(self):
        self.eng.step()
        self.x.pos[f"{T}-TOR"] = 30                                     # user sold 16 of the bot's 46 by hand
        self.eng.step()
        self.assertEqual(self.pf("D_no_contrarian").ledger.positions[f"{T}-TOR"]["contracts"], 23)
        self.eng.step()
        self.assertEqual(sum(p.ledger.positions.get(f"{T}-TOR", {}).get("contracts", 0)
                             for p in self.eng.portfolios), 30)
        self.assertEqual(self.pf("F_hold_to_settle").ledger.positions[f"{T}-TOR"]["contracts"], 23)

    def test_cli_requires_live_flag(self):
        from bot.live import main
        self.assertEqual(main([]), 2)

    def test_only_approved_variants(self):
        self.assertEqual(sorted(p.spec.name for p in self.eng.portfolios), sorted(LIVE_VARIANTS))
        self.assertFalse((self.d / "data/live/A_current").exists())


if __name__ == "__main__":
    unittest.main()
