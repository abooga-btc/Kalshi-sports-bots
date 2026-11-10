"""Free, keyless ESPN news + injury feed -> per-team 'negative news' flags (rule/keyword based).

Endpoints (verified 2026-10-29, no key):
  https://site.api.espn.com/apis/site/v2/sports/{football/nfl|basketball/nba}/injuries
  https://site.api.espn.com/apis/site/v2/sports/{football/nfl|basketball/nba}/news?limit=50
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from .teams import odds_name_to_abbr

ESPN_PATH = {"americanfootball_nfl": "football/nfl", "basketball_nba": "basketball/nba"}
BASE = "https://site.api.espn.com/apis/site/v2/sports"

STATUS_WEIGHT = {"active": 0.0, "out": 1.0, "doubtful": 0.75, "questionable": 0.4, "day-to-day": 0.4,
                 "suspension": 1.0, "injured reserve": 1.0, "probable": 0.1}
# shortComment overrides for ESPN's coarse "Day-To-Day" status
COMMENT_STATUS = [(re.compile(r"\b(ruled out|is out|won't play|will not play|will miss|out for|sidelined|to miss)\b", re.I), "out"),
                  (re.compile(r"\bdoubtful\b", re.I), "doubtful"),
                  (re.compile(r"\bquestionable\b", re.I), "questionable"),
                  (re.compile(r"\b(probable|expected to play|will play|cleared)\b", re.I), "probable")]
STALE_HINT = re.compile(r"\b(remains out|still out|continues to|remains sidelined|again)\b", re.I)
NEG_NEWS = re.compile(r"\b(ruled out|out for|injur\w*|sidelined|will miss|to miss|suspend\w*|torn|tears?|"
                      r"surgery|fracture\w*|concussion|doubtful|questionable|day-to-day|illness)\b", re.I)


@dataclass
class NewsFlag:
    team: str
    kind: str          # 'injury' | 'article'
    who: str
    status: str
    weight: float
    when: str
    note: str


@dataclass
class TeamNews:
    flags: list = field(default_factory=list)

    @property
    def neg(self) -> float:
        return round(min(2.0, sum(f.weight for f in self.flags)), 3)


def _age_h(ts: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return 1e9
    return (now - t).total_seconds() / 3600


STATS = "https://site.web.api.espn.com/apis/common/v3/sports"


def fetch_key_players(session, sport: str, season_year: int | None, cfg, timeout: float = 20) -> dict[str, float]:
    """Stat leaders from ESPN's public stats endpoint -> {lowercase name: key weight}.
    NBA: top `nba_key_top_n` scorers this season AND last season (covers stars who haven't played yet).
    NFL: top `nfl_key_rushers` rushers + top `nfl_key_receivers` receivers (QBs are always key)."""
    path = ESPN_PATH[sport]
    out: dict[str, float] = {}

    def leaders(params):
        r = session.get(f"{STATS}/{path}/statistics/byathlete", params=params, timeout=timeout)
        r.raise_for_status()
        return [a["athlete"]["displayName"].lower() for a in r.json().get("athletes", [])]

    if sport == "basketball_nba":
        seasons = [None] + ([season_year - 1] if season_year else [])
        for yr in seasons:
            p = {"limit": cfg.nba_key_top_n, "sort": "offensive.avgPoints:desc"}
            if yr:
                p.update(season=yr, seasontype=2)
            for n in leaders(p):
                out[n] = 1.0
    else:
        for sort, n in (("rushing.rushingYards:desc", cfg.nfl_key_rushers),
                        ("receiving.receivingYards:desc", cfg.nfl_key_receivers)):
            for name in leaders({"limit": n, "sort": sort}):
                out[name] = max(out.get(name, 0), cfg.nfl_skill_key_weight)
    return out


def build_team_news(sport: str, injuries: dict, articles: list, cfg, now: datetime | None = None,
                    key_weights: dict[str, float] | None = None) -> dict[str, TeamNews]:
    now = now or datetime.now(timezone.utc)
    out: dict[str, TeamNews] = {}
    key_weights = dict(key_weights or {})
    for n in cfg.key_players:
        key_weights[n.lower()] = 1.0
    for team in injuries.get("injuries", []):
        abbr = odds_name_to_abbr(sport, team.get("displayName", ""))
        if not abbr:
            continue
        for inj in team.get("injuries", []):
            age = _age_h(inj.get("date", ""), now)
            if age > cfg.news_fresh_hours:
                continue
            ath = inj.get("athlete", {})
            name = ath.get("displayName", "?")
            pos = (ath.get("position") or {}).get("abbreviation", "")
            comment = inj.get("shortComment", "") or ""
            status = (inj.get("status") or "").lower()
            for rx, st in COMMENT_STATUS:        # refine coarse statuses from the blurb
                if rx.search(comment):
                    status = st
                    break
            sw = STATUS_WEIGHT.get(status, 0.3)
            kw = key_weights.get(name.lower(), 0.0)
            if sport == "americanfootball_nfl" and pos == "QB":
                kw = 1.0
            is_key = kw > 0
            kw = kw if is_key else cfg.non_key_player_weight
            if STALE_HINT.search(comment):
                kw *= 0.5
            w = round(sw * kw, 3)
            if w <= 0.05:
                continue
            why = ("KEY " if is_key else "") + (f"{pos} " if pos else "")
            out.setdefault(abbr, TeamNews()).flags.append(
                NewsFlag(abbr, "injury", name, status, w, inj.get("date", ""), (why + comment)[:160]))
    for a in articles:
        if _age_h(a.get("published", ""), now) > cfg.news_fresh_hours:
            continue
        text = f"{a.get('headline', '')}. {a.get('description', '')}"
        if not NEG_NEWS.search(text):
            continue
        teams = {odds_name_to_abbr(sport, c.get("description", "")) for c in a.get("categories", []) if c.get("type") == "team"}
        teams.discard(None)
        if len(teams) != 1:      # only attribute when exactly one team is tagged
            continue
        abbr = teams.pop()
        out.setdefault(abbr, TeamNews()).flags.append(
            NewsFlag(abbr, "article", "", "keyword", cfg.article_weight, a.get("published", ""), a.get("headline", "")[:160]))
    return out


class ESPNNews:
    def __init__(self, cfg, timeout: float = 20):
        self.cfg, self.timeout = cfg, timeout
        self.s = requests.Session()
        self.cache: dict[str, tuple[float, dict]] = {}   # sport -> (fetched_at, team_news)
        self.keys: dict[str, tuple[float, dict]] = {}    # sport -> (fetched_at, key weights), refreshed daily

    def fetch_raw(self, sport: str) -> tuple[dict, list]:
        path = ESPN_PATH[sport]
        inj = self.s.get(f"{BASE}/{path}/injuries", timeout=self.timeout)
        news = self.s.get(f"{BASE}/{path}/news", params={"limit": 50}, timeout=self.timeout)
        inj.raise_for_status(); news.raise_for_status()
        return inj.json(), news.json().get("articles", [])

    def team_news(self, sport: str, force: bool = False) -> dict[str, TeamNews]:
        hit = self.cache.get(sport)
        if hit and not force and time.time() - hit[0] < self.cfg.news_refresh_sec:
            return hit[1]
        try:
            injuries, articles = self.fetch_raw(sport)
            kh = self.keys.get(sport)
            if not kh or time.time() - kh[0] > 86400:
                try:
                    yr = (injuries.get("season") or {}).get("year")
                    self.keys[sport] = (time.time(), fetch_key_players(self.s, sport, yr, self.cfg, self.timeout))
                except Exception as e:
                    print(f"ESPN key-player fetch failed for {sport}: {type(e).__name__}: {e}")
                    self.keys.setdefault(sport, (time.time(), {}))
            tn = build_team_news(sport, injuries, articles, self.cfg, key_weights=self.keys[sport][1])
        except Exception as e:  # news is advisory: on failure keep old data (or none)
            print(f"ESPN news fetch failed for {sport}: {type(e).__name__}: {e}")
            return hit[1] if hit else {}
        self.cache[sport] = (time.time(), tn)
        return tn
