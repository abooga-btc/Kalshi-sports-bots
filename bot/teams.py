"""Team-name mapping between The Odds API (full names) and Kalshi (ticker abbreviations).

Kalshi game-winner markets look like:
    event_ticker  KXNBAGAME-26NOV01TORLAL     (date + AWAY abbr + HOME abbr)
    ticker        KXNBAGAME-26NOV01TORLAL-TOR (the team this YES contract pays on)
    yes_sub_title "Toronto"  /  "Los Angeles L"  /  "New York J"
The Odds API uses full names like "Toronto Raptors".
"""
from __future__ import annotations

import re

NFL = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAC",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}

NBA = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "Los Angeles Clippers": "LAC", "Los Angeles Lakers": "LAL", "LA Clippers": "LAC",
    "LA Lakers": "LAL", "Memphis Grizzlies": "MEM", "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC", "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS", "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}

# Alternate abbreviations other feeds or older Kalshi tickers may use -> canonical Kalshi abbr.
ABBR_ALIASES = {
    "nfl": {"JAX": "JAC", "WSH": "WAS", "LA": "LAR", "OAK": "LV", "SD": "LAC"},
    "nba": {"BRK": "BKN", "BKN": "BKN", "CHO": "CHA", "GS": "GSW", "NO": "NOP", "NY": "NYK",
            "PHO": "PHX", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"},
}

SPORT_TABLES = {"americanfootball_nfl": ("nfl", NFL), "basketball_nba": ("nba", NBA)}
SERIES_TO_SPORT = {"KXNFLGAME": "americanfootball_nfl", "KXNBAGAME": "basketball_nba"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower().replace(".", "")).strip()


_NORM_TABLES = {sport: {_norm(k): v for k, v in tbl.items()} for sport, (_, tbl) in SPORT_TABLES.items()}


def canonical_abbr(sport: str, abbr: str) -> str:
    league = SPORT_TABLES[sport][0]
    a = abbr.upper()
    return ABBR_ALIASES[league].get(a, a)


def odds_name_to_abbr(sport: str, name: str) -> str | None:
    """Map an Odds API team name to a Kalshi abbreviation (exact table, then suffix match)."""
    tbl = _NORM_TABLES[sport]
    n = _norm(name)
    if n in tbl:
        return tbl[n]
    # tolerate missing/extra city words, e.g. "Lakers" or "LA Lakers"
    nick = n.split()[-1] if n else ""
    hits = {v for k, v in tbl.items() if k.split()[-1] == nick}
    return hits.pop() if len(hits) == 1 else None


def subtitle_matches_name(subtitle: str, full_name: str) -> bool:
    """Fallback: does a Kalshi yes_sub_title like 'Los Angeles L' / 'New York J' / 'Toronto'
    describe the Odds API full name 'Los Angeles Lakers'?"""
    sub, full = _norm(subtitle), _norm(full_name)
    parts = sub.split()
    if parts and len(parts[-1]) == 1 and len(parts) > 1:  # disambiguating letter
        city, letter = " ".join(parts[:-1]), parts[-1]
        if not full.startswith(city + " "):
            return False
        nickname = full[len(city) + 1:]
        return nickname.startswith(letter)
    return full.startswith(sub + " ") or full == sub
