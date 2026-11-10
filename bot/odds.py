"""The Odds API v4 client + odds math (implied probability, vig removal, consensus)."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests


class OddsAPIError(RuntimeError):
    pass


class MissingOddsKey(OddsAPIError):
    pass


# ---------------- odds math ----------------
def american_to_prob(american: float) -> float:
    a = float(american)
    if a == 0:
        raise ValueError("American odds cannot be 0")
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def decimal_to_prob(decimal_odds: float) -> float:
    d = float(decimal_odds)
    if d <= 1.0:
        raise ValueError(f"decimal odds must be > 1, got {d}")
    return 1.0 / d


def american_to_decimal(american: float) -> float:
    a = float(american)
    return 1 + a / 100.0 if a > 0 else 1 + 100.0 / -a


def devig(probs: dict[str, float]) -> dict[str, float]:
    """Proportional (multiplicative) vig removal: scale implied probs so they sum to 1."""
    total = sum(probs.values())
    if total <= 0:
        raise ValueError("implied probabilities must be positive")
    return {k: v / total for k, v in probs.items()}


@dataclass
class GameOdds:
    sport: str
    event_id: str
    home: str
    away: str
    commence: datetime
    fair: dict[str, float]          # team full name -> consensus no-vig win probability
    n_books: int
    book_probs: dict[str, dict[str, float]] = field(default_factory=dict)
    fetched_at: float = 0.0

    def is_live(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return self.commence <= now


def consensus_from_event(ev: dict, sport: str, fetched_at: float = 0.0, odds_format: str = "decimal") -> GameOdds | None:
    """Turn one Odds API event (h2h market) into a no-vig consensus, averaging across books."""
    home, away = ev["home_team"], ev["away_team"]
    per_book: dict[str, dict[str, float]] = {}
    for bk in ev.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            raw = {}
            for o in mkt.get("outcomes", []):
                price = o.get("price")
                if price is None:
                    continue
                try:
                    raw[o["name"]] = decimal_to_prob(price) if odds_format == "decimal" else american_to_prob(price)
                except ValueError:
                    pass
            # only use books quoting exactly the two teams (skip 3-way/draw lines)
            if set(raw) == {home, away}:
                per_book[bk.get("key", bk.get("title", "?"))] = devig(raw)
    if not per_book:
        return None
    fair = {t: statistics.fmean(p[t] for p in per_book.values()) for t in (home, away)}
    fair = devig(fair)  # re-normalise after averaging (guards float drift)
    return GameOdds(sport=sport, event_id=ev.get("id", ""), home=home, away=away,
                    commence=datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")),
                    fair=fair, n_books=len(per_book), book_probs=per_book, fetched_at=fetched_at)


# ---------------- API client ----------------
class OddsClient:
    """Frugal client. /events is free (does not count against quota); /odds costs
    (#markets x #regions) = 1 request per call with markets=h2h, regions=us."""

    def __init__(self, api_key: str | None, base_url: str = "https://api.the-odds-api.com/v4",
                 regions: str = "us", timeout: float = 15):
        if not api_key:
            raise MissingOddsKey(
                "ODDS_API_KEY is not set. Get a free key at https://the-odds-api.com, then put "
                "ODDS_API_KEY=... in kalshi-bot/.env (or export it) and re-run.")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.regions = regions
        self.timeout = timeout
        self.s = requests.Session()
        self.remaining: int | None = None
        self.used: int | None = None
        self.last_cost: int | None = None

    def _get(self, path: str, params: dict) -> list | dict:
        params = {**params, "apiKey": self._key}
        try:
            r = self.s.get(self.base_url + path, params=params, timeout=self.timeout)
        except requests.RequestException as e:  # never echo the URL (it contains the key)
            raise OddsAPIError(f"network error calling Odds API {path}: {type(e).__name__}") from None
        h = r.headers
        if h.get("x-requests-remaining") is not None:
            self.remaining = int(float(h["x-requests-remaining"]))
            self.used = int(float(h.get("x-requests-used", 0)))
            self.last_cost = int(float(h.get("x-requests-last", 0)))
        if r.status_code == 401:
            raise MissingOddsKey("Odds API rejected ODDS_API_KEY (401). Check the key in .env.")
        if r.status_code == 429:
            raise OddsAPIError("Odds API quota exhausted or rate limited (429).")
        if r.status_code != 200:
            raise OddsAPIError(f"Odds API {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    def events(self, sport: str) -> list[dict]:
        """Free endpoint: schedule (ids, teams, commence_time) without odds."""
        return self._get(f"/sports/{sport}/events", {"dateFormat": "iso"})

    def h2h_odds(self, sport: str, fetched_at: float) -> list[GameOdds]:
        data = self._get(f"/sports/{sport}/odds", {"regions": self.regions, "markets": "h2h",
                                                    "oddsFormat": "decimal", "dateFormat": "iso"})
        out = []
        for ev in data:
            g = consensus_from_event(ev, sport, fetched_at)
            if g:
                out.append(g)
        return out
