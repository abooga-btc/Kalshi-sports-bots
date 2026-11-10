"""Offline tests for the multi-signal entry: volume gate, demand/contrarian, ESPN news, combined score."""
import unittest
from datetime import datetime, timedelta, timezone

from bot.config import Config
from bot.espn import TeamNews, build_team_news
from bot.kalshi import group_events, parse_market
from bot.signals import (Demand, combine, contrarian_score, depth_near_top, game_demand, news_score,
                         volume_gate)
from tests.test_logic import kmarket

NOW = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
T = "KXNFLGAME-26NOV01NEMIA"


def game(ne=(.76, .77), mia=(.23, .24), vol=600000, prev_ne=None):
    return [parse_market(kmarket(f"{T}-NE", "New England", *ne, volume=vol, prev=prev_ne), "KXNFLGAME"),
            parse_market(kmarket(f"{T}-MIA", "Miami", *mia, volume=vol), "KXNFLGAME")]


class Volume(unittest.TestCase):
    def test_floor_and_rank(self):
        cfg = Config()
        mks = []
        for i, v in enumerate([900000, 300000, 60000, 20000]):
            t = f"KXNBAGAME-26OCT29A{i}B{i}"
            mks += [parse_market(kmarket(f"{t}-AAA", "A", .5, .51, volume=v / 2), "KXNBAGAME"),
                    parse_market(kmarket(f"{t}-BBB", "B", .49, .5, volume=v / 2), "KXNBAGAME")]
        g = volume_gate(group_events(mks), cfg)
        self.assertEqual([g[f"KXNBAGAME-26OCT29A{i}B{i}"]["pass"] for i in range(4)], [True, True, False, False])
        self.assertIn("rank 3/4", g["KXNBAGAME-26OCT29A2B2"]["why"])       # 60k passes floor, fails top-50%
        self.assertIn("floor", g["KXNBAGAME-26OCT29A3B3"]["why"])
        self.assertEqual(g["KXNBAGAME-26OCT29A0B0"]["volume"], 900000)


class DemandAndContrarian(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_depth_near_top(self):
        self.assertEqual(depth_near_top([(0.40, 5), (0.50, 10), (0.52, 20), (0.53, 30)], 0.03), 60)
        self.assertEqual(depth_near_top([], 0.03), 0)

    def test_game_demand_maps_no_side_to_opponent(self):
        mks = game()
        books = {f"{T}-NE": {"yes": [(0.76, 100)], "no": [(0.23, 50)]},
                 f"{T}-MIA": {"yes": [(0.23, 30)], "no": [(0.76, 70)]}}
        trades = {f"{T}-NE": [{"side": "yes", "count": 900}, {"side": "no", "count": 10}],
                  f"{T}-MIA": [{"side": "no", "count": 90}]}
        d = game_demand(mks, books, trades, 0.03)
        self.assertEqual(d.book, {"NE": 170, "MIA": 80})     # NE: yes(NE)+no(MIA); MIA: yes(MIA)+no(NE)
        self.assertEqual(d.flow, {"NE": 990, "MIA": 10})
        self.assertAlmostEqual(Demand.share(d.flow, "NE"), 0.99)

    def test_contrarian_favors_side_against_heavy_flow_and_overpay(self):
        ne, mia = game(prev_ne=0.72)
        d = Demand(book={"NE": 50, "MIA": 50}, flow={"NE": 99000, "MIA": 1000})
        c = contrarian_score("MIA", ne, 0.739, d, self.cfg)        # NE mid .765 vs fair .739 = +2.6c overpaid
        self.assertAlmostEqual(c["flow_c"], (0.99 - 0.70) / 0.30 * 2.5, places=2)
        self.assertEqual(c["book_c"], 0.0)                          # balanced book
        self.assertAlmostEqual(c["overpay_c"], 1.6, places=1)
        self.assertEqual(c["drift_c"], 1.0)                         # NE rose 5c (> 3c) while overpriced
        self.assertLessEqual(c["total"], self.cfg.contra_max_cents)
        # the crowd's side gets nothing
        c2 = contrarian_score("NE", mia, 0.261, d, self.cfg)
        self.assertEqual(c2["flow_c"], 0.0)
        self.assertEqual(c2["overpay_c"], 0.0)

    def test_thin_flow_ignored(self):
        ne, _ = game()
        c = contrarian_score("MIA", ne, 0.80, Demand(flow={"NE": 900, "MIA": 10}), self.cfg)
        self.assertEqual(c["flow_c"], 0.0)                           # 910 < min_flow_contracts


class News(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def inj(self, team, name, status, comment, hours_ago, pos="G"):
        return {"displayName": team, "injuries": [{
            "status": status, "shortComment": comment,
            "date": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
            "athlete": {"displayName": name, "position": {"abbreviation": pos}}}]}

    def test_key_player_out_blocks_and_old_news_ignored(self):
        injuries = {"injuries": [
            self.inj("Toronto Raptors", "Kawhi Leonard", "Out", "Leonard (knee) is expected to be sidelined for weeks", 2, "F"),
            self.inj("Los Angeles Lakers", "Luka Doncic", "Out", "Doncic (back) is out", 40),          # stale
            self.inj("Orlando Magic", "Bench Guy", "Day-To-Day", "Guy (ankle) is questionable for Friday", 1)]}
        tn = build_team_news("basketball_nba", injuries, [], self.cfg, now=NOW,
                             key_weights={"kawhi leonard": 1.0, "luka doncic": 1.0})
        self.assertEqual(tn["TOR"].neg, 1.0)
        self.assertNotIn("LAL", tn)
        self.assertAlmostEqual(tn["ORL"].neg, 0.4 * 0.15, places=3)   # questionable * non-key weight
        n = news_score("TOR", "ORL", tn, self.cfg)
        self.assertTrue(n["blocked"])
        n2 = news_score("ORL", "TOR", tn, self.cfg)
        self.assertFalse(n2["blocked"])
        self.assertGreater(n2["total"], 1.5)                           # boost the opponent of the injured team

    def test_nfl_qb_always_key_and_comment_refines_status(self):
        injuries = {"injuries": [self.inj("Baltimore Ravens", "Lamar Jackson", "Day-To-Day",
                                          "Jackson (ankle) has been ruled out for Sunday", 3, "QB")]}
        tn = build_team_news("americanfootball_nfl", injuries, [], self.cfg, now=NOW)
        self.assertEqual(tn["BAL"].flags[0].status, "out")
        self.assertEqual(tn["BAL"].neg, 1.0)

    def test_article_keyword_single_team_only(self):
        arts = [{"published": (NOW - timedelta(hours=1)).isoformat(), "headline": "Celtics star ruled out with torn ACL",
                 "description": "", "categories": [{"type": "team", "description": "Boston Celtics"}]},
                {"published": (NOW - timedelta(hours=1)).isoformat(), "headline": "Bulls injury report vs Celtics",
                 "description": "", "categories": [{"type": "team", "description": "Boston Celtics"},
                                                   {"type": "team", "description": "Chicago Bulls"}]},
                {"published": (NOW - timedelta(hours=1)).isoformat(), "headline": "Knicks win again",
                 "description": "", "categories": [{"type": "team", "description": "New York Knicks"}]}]
        tn = build_team_news("basketball_nba", {"injuries": []}, arts, self.cfg, now=NOW)
        self.assertEqual(set(tn), {"BOS"})
        self.assertEqual(tn["BOS"].neg, self.cfg.article_weight)


class Combine(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.no_news = {"total": 0.0, "neg_self": 0.0, "neg_opp": 0.0, "blocked": False}

    def test_pure_edge_path_unchanged(self):
        self.assertTrue(combine(5.0, {"total": 0.0}, self.no_news, self.cfg)[0])
        self.assertFalse(combine(4.0, {"total": 0.0}, self.no_news, self.cfg)[0])

    def test_relaxed_floor_with_strong_signals(self):
        ok, score, why = combine(2.1, {"total": 4.1}, self.no_news, self.cfg)
        self.assertTrue(ok); self.assertAlmostEqual(score, 6.2); self.assertIn("relaxed", why)
        # strong signals but paying above fair -> no
        self.assertFalse(combine(-0.5, {"total": 5.0}, self.no_news, self.cfg)[0])
        # signals below 'strong' -> normal 5c floor applies
        self.assertFalse(combine(2.5, {"total": 2.9}, self.no_news, self.cfg)[0])
        # strong but total score too low
        self.assertFalse(combine(0.5, {"total": 3.5}, self.no_news, self.cfg)[0])

    def test_news_block_overrides_everything(self):
        n = {"total": -2.0, "neg_self": 1.0, "neg_opp": 0.0, "blocked": True}
        ok, _, why = combine(9.0, {"total": 5.0}, n, self.cfg)
        self.assertFalse(ok); self.assertIn("BLOCKED", why)


if __name__ == "__main__":
    unittest.main()
