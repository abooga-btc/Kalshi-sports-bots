"""Offline unit tests: odds math, vig removal, team matching, signals, fees, quota, missing key."""
import io
import os
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest import mock

from bot.config import Config, load_config
from bot.kalshi import group_events, parse_market
from bot.odds import (MissingOddsKey, OddsClient, american_to_decimal, american_to_prob,
                      consensus_from_event, decimal_to_prob, devig)
from bot.strategy import (contracts_for_budget, entry_signal, exit_signal, match_game,
                          odds_min_interval, taker_fee)
from bot.teams import NBA, NFL, odds_name_to_abbr, subtitle_matches_name

T0 = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)


def kmarket(ticker, sub, bid, ask, start=T0, size=500, status="active", volume=100000, prev=None):
    return {"volume_fp": str(volume), "volume_24h_fp": str(volume / 4), "open_interest_fp": str(volume * 0.9),
            "previous_price_dollars": f"{prev if prev is not None else (bid + ask) / 2:.4f}",
            "last_price_dollars": f"{ask:.4f}","ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": status,
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "yes_ask_size_fp": str(size),
            "yes_sub_title": sub, "occurrence_datetime": start.isoformat().replace("+00:00", "Z"),
            "close_time": (start + timedelta(days=2)).isoformat()}


def odds_event(home, away, prices_by_book, start=T0):
    return {"id": "e1", "home_team": home, "away_team": away, "commence_time": start.isoformat(),
            "bookmakers": [{"key": b, "markets": [{"key": "h2h", "outcomes": [
                {"name": home, "price": ph}, {"name": away, "price": pa}]}]} for b, (ph, pa) in prices_by_book.items()]}


class OddsMath(unittest.TestCase):
    def test_american(self):
        self.assertAlmostEqual(american_to_prob(-150), 0.6)
        self.assertAlmostEqual(american_to_prob(150), 0.4)
        self.assertAlmostEqual(american_to_prob(100), 0.5)
        self.assertAlmostEqual(american_to_decimal(-200), 1.5)
        self.assertAlmostEqual(american_to_decimal(250), 3.5)
        with self.assertRaises(ValueError):
            american_to_prob(0)

    def test_decimal(self):
        self.assertAlmostEqual(decimal_to_prob(2.0), 0.5)
        with self.assertRaises(ValueError):
            decimal_to_prob(1.0)

    def test_devig(self):
        # -110 / -110 -> 52.38% each, sums to 1.0476; no-vig = 50/50
        p = devig({"A": american_to_prob(-110), "B": american_to_prob(-110)})
        self.assertAlmostEqual(p["A"], 0.5)
        p = devig({"A": american_to_prob(-200), "B": american_to_prob(170)})
        self.assertAlmostEqual(sum(p.values()), 1.0)
        self.assertAlmostEqual(p["A"], 0.6667 / (0.6667 + 0.3704), places=3)

    def test_consensus_average(self):
        ev = odds_event("Los Angeles Lakers", "Toronto Raptors",
                        {"dk": (1.5, 2.7), "fd": (1.55, 2.6), "mgm": (1.45, 2.85)})
        g = consensus_from_event(ev, "basketball_nba")
        self.assertEqual(g.n_books, 3)
        self.assertAlmostEqual(sum(g.fair.values()), 1.0)
        self.assertTrue(0.62 < g.fair["Los Angeles Lakers"] < 0.66)

    def test_consensus_skips_three_way(self):
        ev = odds_event("A", "B", {"x": (2.0, 2.0)})
        ev["bookmakers"][0]["markets"][0]["outcomes"].append({"name": "Draw", "price": 10})
        self.assertIsNone(consensus_from_event(ev, "americanfootball_nfl"))


class Teams(unittest.TestCase):
    def test_tables_complete(self):
        self.assertEqual(len(set(NFL.values())), 32)
        self.assertEqual(len(set(NBA.values())), 30)

    def test_lookup(self):
        self.assertEqual(odds_name_to_abbr("basketball_nba", "Los Angeles Lakers"), "LAL")
        self.assertEqual(odds_name_to_abbr("basketball_nba", "LA Clippers"), "LAC")
        self.assertEqual(odds_name_to_abbr("americanfootball_nfl", "Jacksonville Jaguars"), "JAC")
        self.assertEqual(odds_name_to_abbr("americanfootball_nfl", "new york jets"), "NYJ")
        self.assertEqual(odds_name_to_abbr("basketball_nba", "Philadelphia 76ers"), "PHI")
        self.assertEqual(odds_name_to_abbr("basketball_nba", "Trail Blazers"), "POR")
        self.assertIsNone(odds_name_to_abbr("basketball_nba", "Seattle SuperSonics"))
        self.assertEqual(odds_name_to_abbr("basketball_nba", "Lakers"), "LAL")

    def test_subtitle_fallback(self):
        self.assertTrue(subtitle_matches_name("Los Angeles L", "Los Angeles Lakers"))
        self.assertFalse(subtitle_matches_name("Los Angeles L", "Los Angeles Clippers"))
        self.assertTrue(subtitle_matches_name("New York J", "New York Jets"))
        self.assertFalse(subtitle_matches_name("New York G", "New York Jets"))
        self.assertTrue(subtitle_matches_name("Toronto", "Toronto Raptors"))

    def test_match_game(self):
        mks = [parse_market(kmarket("KXNBAGAME-26NOV01TORLAL-TOR", "Toronto", .40, .42), "KXNBAGAME"),
               parse_market(kmarket("KXNBAGAME-26NOV01TORLAL-LAL", "Los Angeles L", .57, .59), "KXNBAGAME"),
               # same teams, different date -> must not match
               parse_market(kmarket("KXNBAGAME-26NOV20LALTOR-TOR", "Toronto", .5, .52, T0 + timedelta(days=18)), "KXNBAGAME"),
               parse_market(kmarket("KXNBAGAME-26NOV20LALTOR-LAL", "Los Angeles L", .47, .49, T0 + timedelta(days=18)), "KXNBAGAME")]
        events = group_events(mks)
        self.assertEqual(mks[0].event_teams, ("TOR", "LAL"))
        g = consensus_from_event(odds_event("Los Angeles Lakers", "Toronto Raptors", {"a": (1.6, 2.4)}), "basketball_nba")
        pairs = match_game(g, events)
        self.assertEqual({m.ticker for m, _ in pairs}, {"KXNBAGAME-26NOV01TORLAL-TOR", "KXNBAGAME-26NOV01TORLAL-LAL"})
        self.assertEqual(dict((m.team_abbr, t) for m, t in pairs)["LAL"], "Los Angeles Lakers")


class Signals(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def mk(self, bid, ask, size=500):
        return parse_market(kmarket("KXNFLGAME-26NOV09BUFMIN-MIN", "Minnesota", bid, ask, size=size), "KXNFLGAME")

    def test_fee(self):
        self.assertEqual(taker_fee(20, 0.50), 0.35)   # 0.07*20*.25 = 0.35
        self.assertEqual(taker_fee(1, 0.50), 0.02)    # 0.0175 rounds up
        self.assertEqual(taker_fee(0, 0.5), 0.0)

    def test_sizing_respects_budget(self):
        n = contracts_for_budget(10, 0.45, include_fees=True)
        self.assertLessEqual(n * 0.45 + taker_fee(n, 0.45), 10)
        self.assertEqual(contracts_for_budget(10, 0.45, include_fees=False), 22)

    def test_entry_requires_edge(self):
        sig, why = entry_signal(self.mk(.42, .45), "Minnesota Vikings", 0.50, self.cfg)
        self.assertIsNotNone(sig, why)                 # exactly 5c edge -> buy
        self.assertAlmostEqual(sig.edge, 0.05)
        sig, why = entry_signal(self.mk(.43, .46), "Minnesota Vikings", 0.50, self.cfg)
        self.assertIsNone(sig)                         # 4c edge -> no
        self.assertIn("edge", why)

    def test_entry_filters(self):
        self.assertIsNone(entry_signal(self.mk(.30, .45), "x", 0.60, self.cfg)[0])    # spread 15c
        self.assertIsNone(entry_signal(self.mk(.01, .03), "x", 0.20, self.cfg)[0])    # below min price
        sig, _ = entry_signal(self.mk(.44, .45, size=3), "x", 0.60, self.cfg)
        self.assertEqual(sig.contracts, 3)                                            # liquidity cap
        self.cfg.edge_cents = 10
        self.assertIsNone(entry_signal(self.mk(.42, .45), "x", 0.52, self.cfg)[0])

    def test_take_profit_and_stop(self):
        cfg = self.cfg
        cfg.include_fees = False
        self.assertFalse(exit_signal(10.0, 20, 0.55, cfg)[0])   # +10%
        ok, why, val = exit_signal(10.0, 20, 0.575, cfg)        # +15%
        self.assertTrue(ok); self.assertIn("take_profit", why); self.assertAlmostEqual(val, 11.5)
        self.assertFalse(exit_signal(10.0, 20, 0.20, cfg)[0])   # stop-loss off by default
        cfg.stop_loss_pct = 30
        self.assertIn("stop_loss", exit_signal(10.0, 20, 0.30, cfg)[1])
        cfg.include_fees = True
        self.assertFalse(exit_signal(10.0, 20, 0.575, cfg)[0])  # fees eat the 15% -> hold


class QuotaAndConfig(unittest.TestCase):
    def test_quota_spacing(self):
        now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
        gap = odds_min_interval(500, 20, now)
        self.assertTrue(300 < gap < 1000)                  # ~2.5 days left / 480 calls
        self.assertEqual(odds_min_interval(20, 20, now), float("inf"))
        self.assertEqual(odds_min_interval(None, 20, now), 60.0)

    def test_env_override(self):
        cfg = load_config(env={"EDGE_CENTS": "7", "DRY_RUN": "false", "SERIES": "KXNBAGAME"})
        self.assertEqual(cfg.edge_cents, 7.0)
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.series, ["KXNBAGAME"])
        self.assertTrue(load_config(env={}).dry_run)

    def test_missing_key_is_clear(self):
        with self.assertRaises(MissingOddsKey) as cm:
            OddsClient(None)
        self.assertIn("ODDS_API_KEY", str(cm.exception))

    def test_main_exits_gracefully_without_key(self):
        from bot import main as m
        with mock.patch.dict(os.environ, {"ODDS_API_KEY": ""}), mock.patch.object(m, "load_dotenv"):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = m.main(["--once"])
        self.assertEqual(rc, 2)
        self.assertIn("ODDS_API_KEY is not set", err.getvalue())

    def test_live_requires_dry_run_false(self):
        from bot import main as m
        with mock.patch.dict(os.environ, {"DRY_RUN": "true"}), mock.patch.object(m, "load_dotenv"):
            with self.assertRaises(SystemExit):
                m.main(["--live", "--once"])



class Signing(unittest.TestCase):
    def test_rsa_pss_signature_verifies(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from bot.kalshi import KalshiClient
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
        k = KalshiClient("https://x/trade-api/v2", key_id="kid", private_key_pem=pem)
        sig = k._sign("1703123456789", "POST", "/trade-api/v2/portfolio/events/orders?x=1")
        key.public_key().verify(base64.b64decode(sig), b"1703123456789POST/trade-api/v2/portfolio/events/orders",
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
        self.assertTrue(k.can_trade)


if __name__ == "__main__":
    unittest.main()
