"""
WarRoom — NBA ES / CES scoring (prototype).

The NBA twin of es_scoring.py. Implements the user's position-specific Excel Score
(decoded from "2012 NBA ES FORMULA.htm") and rolls it up into a Career Excel Score.

ES (one season, one position formula)
─────────────────────────────────────
For each of 10 stats: ratio r = player_total / baseline, where baseline = the
average of the TOP 50 (by total points) players whose primary listed position is
that position, that season. The one "bad" stat (TOV) is inverted: r = 2 - r
(uncapped — can go negative, matching the user's sheet). Shooting stats (FT/3P/2P%)
use the rate; counting stats use season totals (so availability is baked in).

    ES = anchor(year) * Σ(weight·r) / Σ(weight)   (anchor rises 35→50 from 1947→2005,
    then holds at 50 — an era skill adjustment; awards are added flat afterward)

Weights are POSITION-SPECIFIC (PG weights AST, C weights REB/BLK, etc.).

POSITION
────────
NOTE (2026-06-14): the proposed "score under all 5, highest ES = position" rule
was tested and it INVERTS positions — ratios explode where a baseline is lowest
in a player's strengths, so centers score highest at guard (Kareem '72: SG 187 vs
C 128) and guards highest at center (Westbrook '13: C 86.7 vs PG 70.6). It also
inflates CES. A profile-similarity assignment was also unreliable. So this
prototype scores each season under the player's PRIMARY LISTED position (how the
five source sheets were built); career position = most-common listed position.
Switchable if a better data-driven rule is chosen.

CES (career — what the game uses)
─────────────────────────────────
Tier-weighted roll-up of a player's best 12 seasonal ES in 4 tiers of 3 (weighted
50/30/15/5%; best ES fills Tier 1; slots fill bottom-up so the top tiers need a
long career). Every season past the top 12 adds a small 1% longevity bonus. Each
scaled by availability (min(G,82)/82).

Data: PythonProject2/nba_player_totals.csv (season totals, 1947-2026, all players
incl. internationals — Jokić/Giannis/Dončić/etc.). No scraping.

    python es_scoring_nba.py            # print all-time CES leaderboard (sanity check)
"""

import csv
import collections
import json
import os

TOTALS = os.environ.get(
    "NBA_TOTALS",
    r"C:\Users\excel\PycharmProjects\PythonProject2\nba_player_totals.csv",
)

POSITIONS = ["PG", "SG", "SF", "PF", "C"]
RATIO_CAP = 3.0        # per-stat ratio clamp: positive -> [0,3], inverted -> [-1,2]

# stat order: PTS, REB, AST, TOV(inverted), STL, BLK, FT%, 3P%, 2P%, MP
STAT_COLS = ["pts", "trb", "ast", "tov", "stl", "blk",
             "ft_percent", "x3p_percent", "x2p_percent", "mp"]
INVERTED = [False, False, False, True, False, False, False, False, False, False]

# position-specific weights, stat order PTS/REB/AST/TOV/STL/BLK/FT%/3P%/2P%/MP.
# Scoring raised league-wide; guard (PG/SG) profile tuned for skill: rebounding
# down, turnovers (ball security) up, shooting efficiencies (FT%/3P%/2P%) up;
# centers' rebounding trimmed.
WEIGHTS = {
    "PG": [3.25, 0.5, 1.7, 1.8, 1.5, 1.1, 1.6, 2.0, 1.6, 1.0],
    "SG": [3.4, 0.9, 1.4, 1.8, 1.4, 1.2, 1.6, 2.0, 1.6, 1.0],
    "SF": [3.4, 1.2, 1.3, 1.4, 1.3, 1.3, 1.3, 1.3, 1.3, 1.0],
    "PF": [3.25, 1.7, 1.2, 1.3, 1.2, 1.4, 1.0, 1.0, 1.4, 1.0],
    "C":  [3.2, 1.6, 1.0, 1.2, 1.1, 1.5, 1.0, 1.0, 1.5, 1.0],
}

MIN_MP = 500           # a season must clear this to count toward CES / position
GAMES_FULL = 82        # availability denominator (min(G,82)/82)
LEAGUES = {"NBA", "BAA"}
_MULTI = {"TOT", "2TM", "3TM", "4TM", "5TM"}

# Era skill adjustment: the "average season" anchor (the 50 in ES) rises linearly
# from 35 in the first NBA year (1947) to 50 by 2005, then holds at 50 — so being
# average in the weaker, smaller early league is worth less than today. Within a
# season the comparison is unchanged; whole early eras are just scaled down.
ERA_START_YR, ERA_FULL_YR = 1947, 2005
ERA_BASE_START, ERA_BASE_END = 35.0, 50.0


def era_anchor(year):
    if year <= ERA_START_YR:
        return ERA_BASE_START
    if year >= ERA_FULL_YR:
        return ERA_BASE_END
    frac = (year - ERA_START_YR) / (ERA_FULL_YR - ERA_START_YR)
    return ERA_BASE_START + (ERA_BASE_END - ERA_BASE_START) * frac

# Award points added to a season's ES (user's scheme). Voted awards count only on
# a win (the "-1" finish). All-NBA / All-Defensive tiers are exclusive; the rest
# stack. (6MOY / MIP / CPOY are in the data but intentionally excluded.)
AWARDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nba_awards.json")


def load_awards():
    try:
        return json.load(open(AWARDS_PATH, encoding="utf-8"))
    except FileNotFoundError:
        return {}


def award_points(s):
    if not s:
        return 0.0
    toks = {t.strip() for t in s.split(",")}
    p = 0.0
    if "MVP-1" in toks:  p += 7
    if "DPOY-1" in toks: p += 5
    if "ROY-1" in toks:  p += 2
    if "NBA1" in toks:   p += 5
    elif "NBA2" in toks: p += 3
    elif "NBA3" in toks: p += 2
    if "DEF1" in toks:   p += 3
    elif "DEF2" in toks: p += 2
    if "AS" in toks:     p += 1
    return p


# NBA/BAA champion by bbref season year -> the title team's code (public record;
# not in the regular-season data). Every player on that team's roster gets +4.
CHAMP_BONUS = 4
CHAMPIONS = {
    2025: "OKC", 2024: "BOS", 2023: "DEN", 2022: "GSW", 2021: "MIL", 2020: "LAL",
    2019: "TOR", 2018: "GSW", 2017: "GSW", 2016: "CLE", 2015: "GSW", 2014: "SAS",
    2013: "MIA", 2012: "MIA", 2011: "DAL", 2010: "LAL", 2009: "LAL", 2008: "BOS",
    2007: "SAS", 2006: "MIA", 2005: "SAS", 2004: "DET", 2003: "SAS", 2002: "LAL",
    2001: "LAL", 2000: "LAL", 1999: "SAS", 1998: "CHI", 1997: "CHI", 1996: "CHI",
    1995: "HOU", 1994: "HOU", 1993: "CHI", 1992: "CHI", 1991: "CHI", 1990: "DET",
    1989: "DET", 1988: "LAL", 1987: "LAL", 1986: "BOS", 1985: "LAL", 1984: "BOS",
    1983: "PHI", 1982: "LAL", 1981: "BOS", 1980: "LAL", 1979: "SEA", 1978: "WSB",
    1977: "POR", 1976: "BOS", 1975: "GSW", 1974: "BOS", 1973: "NYK", 1972: "LAL",
    1971: "MIL", 1970: "NYK", 1969: "BOS", 1968: "BOS", 1967: "PHI", 1966: "BOS",
    1965: "BOS", 1964: "BOS", 1963: "BOS", 1962: "BOS", 1961: "BOS", 1960: "BOS",
    1959: "BOS", 1958: "STL", 1957: "BOS", 1956: "PHW", 1955: "SYR", 1954: "MNL",
    1953: "MNL", 1952: "MNL", 1951: "ROC", 1950: "MNL", 1949: "MNL", 1948: "BLB",
    1947: "PHW",
}


def load_champion_players():
    """Set of (player_id, year) for every player on that year's title team."""
    out = set()
    with open(TOTALS, encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            try:
                y = int(r["season"])
            except (TypeError, ValueError):
                continue
            if CHAMPIONS.get(y) == r.get("team"):
                out.add((r["player_id"], y))
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def load_seasons():
    """{season:int -> [player_row dicts]} deduped to one row per player (the
    multi-team TOT aggregate when a player was traded)."""
    by_season_player = collections.defaultdict(dict)
    with open(TOTALS, encoding="utf-8", errors="replace") as fh:
        for r in csv.DictReader(fh):
            if r.get("lg") not in LEAGUES:
                continue
            try:
                yr = int(r["season"])
            except (TypeError, ValueError):
                continue
            cur = by_season_player[yr].get(r["player_id"])
            if cur is None or r.get("team") in _MULTI:
                by_season_player[yr][r["player_id"]] = r
    return {yr: list(d.values()) for yr, d in by_season_player.items()}


def primary_pos(row):
    p = (row.get("pos") or "").split("-")[0].strip().upper()
    return {"G": "SG", "F": "SF"}.get(p, p) if p not in POSITIONS else p


def team_count(rows):
    """Distinct teams in the league that season (ignores multi-team TOT rows)."""
    return len({r.get("team") for r in rows if r.get("team") and r.get("team") not in _MULTI})


STARTERS_PER_TEAM = 5


def starter_baseline(rows):
    """ONE league-wide starter benchmark for the season: average each stat over the
    top (teams × 5) players by MINUTES played. Positions are expressed through the
    weights, not separate baselines — so every player is measured against the same
    'average starter' and the position whose weights fit best is their position."""
    pool = sorted(rows, key=lambda r: _f(r.get("mp")), reverse=True)[:team_count(rows) * STARTERS_PER_TEAM]
    if not pool:
        return [0.0] * len(STAT_COLS)
    return [sum(_f(r.get(c)) for r in pool) / len(pool) for c in STAT_COLS]


def es(row, weights, baseline, anchor=50.0):
    """ES of a player-season under one position's WEIGHTS vs the common starter
    baseline (None if no usable stats that era). `anchor` = era_anchor(year)."""
    num = den = 0.0
    for i, col in enumerate(STAT_COLS):
        b = baseline[i]
        if not b:                       # stat untracked that era -> drop it
            continue
        r = _f(row.get(col)) / b
        if INVERTED[i]:
            r = max(-1.0, min(2.0, 2 - r))      # bad stat, clamped
        else:
            r = max(0.0, min(RATIO_CAP, r))     # no single category can explode
        num += weights[i] * r
        den += weights[i]
    return (anchor * num / den) if den else None


# ── CES roll-up — best 12 seasons in 4 tiers of 3 + a 1% longevity adder ──────
TIER_OF_SLOT = [4, 3, 2, 1] * 3                                 # 12 slots, 3 per tier
TIER_W = {1: 0.50 / 3, 2: 0.30 / 3, 3: 0.15 / 3, 4: 0.05 / 3}   # tiers 50/30/15/5%
EXTRA_W = 0.01                                                  # each season past the top 12


def ces(seasons):
    """seasons = list of (ES, games) -> Career Excel Score.

    The best 12 seasons fill 4 tiers of 3 (best ES -> Tier 1), weighted
    50/30/15/5%; slots fill bottom-up so the high tiers need a long career. Every
    season past the top 12 then adds a small 1% longevity bonus. Each season scaled
    by availability (min(G,82)/82)."""
    seasons = sorted(seasons, key=lambda s: s[0], reverse=True)
    top, extra = seasons[:12], seasons[12:]
    counts = collections.Counter(TIER_OF_SLOT[i] for i in range(len(top)))
    assigned, idx = [], 0
    for tier in (1, 2, 3, 4):
        for _ in range(counts[tier]):
            assigned.append((tier, top[idx]))
            idx += 1
    total = 0.0
    for tier, (e, g) in assigned:
        total += TIER_W[tier] * (min(g or 0, GAMES_FULL) / GAMES_FULL) * e
    for e, g in extra:
        total += EXTRA_W * (min(g or 0, GAMES_FULL) / GAMES_FULL) * e
    return total


def build():
    """-> {player_id: {name, pos, ces, seasons}}"""
    seasons_by_year = load_seasons()
    baselines = {yr: starter_baseline(rows) for yr, rows in seasons_by_year.items()}
    awards = load_awards()
    champs = load_champion_players()

    players = collections.defaultdict(lambda: {"name": "", "seasons": [], "pos_years": collections.Counter()})
    for yr, rows in seasons_by_year.items():
        bl, a = baselines[yr], era_anchor(yr)
        for r in rows:
            if _f(r.get("mp")) < MIN_MP:
                continue
            # Score the season under the formula CLOSEST to the player — his listed
            # (bbref) position — vs the common starter baseline. (Tested picking the
            # best-fit position by max weight-score: it scrambles, so position comes
            # from the data; the common baseline + position weights do the rest.)
            pos = primary_pos(r)
            if pos not in POSITIONS:
                continue
            e = es(r, WEIGHTS[pos], bl, anchor=a)
            if e is None:
                continue
            pid = r["player_id"]
            e += award_points(awards.get(f"{pid}|{yr}", ""))   # season award bonus (flat)
            if (pid, yr) in champs:                            # title-team season
                e += CHAMP_BONUS
            P = players[pid]
            P["name"] = r.get("player") or P["name"]
            P["seasons"].append((e, _f(r.get("g"))))
            P["pos_years"][pos] += 1

    out = {}
    for pid, P in players.items():
        if not P["seasons"]:
            continue
        pos = P["pos_years"].most_common(1)[0][0]
        out[pid] = {"name": P["name"], "pos": pos,
                    "ces": round(ces(P["seasons"]), 1), "seasons": len(P["seasons"])}
    return out


def main():
    players = build()
    ranked = sorted(players.values(), key=lambda p: p["ces"], reverse=True)
    print(f"Scored {len(players)} players from {TOTALS}\n")
    print("=== ALL-TIME CES — TOP 30 ===")
    for i, p in enumerate(ranked[:30], 1):
        print(f"{i:2}. {p['ces']:6.1f}  {p['name'][:26]:26} {p['pos']:3} ({p['seasons']} qualifying seasons)")
    print()
    for pos in POSITIONS:
        top = [p for p in ranked if p["pos"] == pos][:10]
        print(f"\n=== ALL-TIME TOP 10 {pos} ===")
        for i, p in enumerate(top, 1):
            print(f"{i:2}. {p['ces']:6.1f}  {p['name'][:26]:26} ({p['seasons']} szn)")


if __name__ == "__main__":
    main()
