"""
WarRoom — ES / CES scoring.

This module is the single source of truth for how a player's NFL career is graded.
It is computed OFFLINE from PythonProject2's cached PFR season pages and the result
(one Career Excel Score per player) is written into warroom.db. The running game
never touches these pages — it just reads the stored CES (see app.build_indexes).

    python es_scoring.py           # print Top-10 CES per position (sanity check)
    python es_scoring.py --build   # compute every pool player's CES -> warroom.db


WHY THIS EXISTS
───────────────
The old metric (career Approximate Value, percentiled) over-rewarded longevity and
wasn't interpretable. ES/CES replaces it with a transparent, season-by-season grade.


ES — Excel Score (one season)
─────────────────────────────
Compares a player's season to a baseline of that year's top producers at his
position, stat by stat:

    ratio  = player_stat / baseline_avg            (clamped to [0, 3])
    ratio  = 2 - player_stat/baseline_avg          for "bad" stats (clamped [-1, 2])
    ES     = 50 * Σ(weight·ratio) / Σ(weight)      + award bonuses

So an exactly-baseline season = 50; twice the baseline ≈ 100; it is uncapped, so a
historic season runs past 100 and a terrible one can dip below 0.

  • OFFENSE uses COUNTING stats (yards/TD/etc.), not rates. Rates don't separate
    starters (every starting QB has a similar completion %), but totals spread
    2-3x like the defensive box-score stats do — so all positions land on one scale
    without any artificial multiplier. The QB line merges his rushing totals.
  • BASELINE = average of the top-N producing seasons at the position that year
    (a fixed "starter" cohort): QB/TE n=50 (~1 per team), WR/RB n=100 (~1.5/team).
    Using "everyone who played" instead would let backups drag the bar down and
    inflate the thin positions (QB, TE), so we size the cohort to the league.
  • DEFENSE is split into sub-roles (DL / LB / DB) and each is benchmarked against
    its own top-50, with role-specific weights (edge = sacks/TFL, DB = INT/PD…) —
    otherwise box-score-heavy safeties bury single-currency pass rushers. PFR
    side-prefixes positions (LDT, RDE, LOLB, RCB), so def_group() matches by token;
    missing that silently dropped most DL/LB seasons.
  • AWARDS won that season are added on top of ES (see award_bonus). This is the
    lever that separates a great season from a merely good one — counting stats
    flatten the very top, awards re-expand it and push the all-time greats past 100.


CES — Career Excel Score (what the game uses)
─────────────────────────────────────────────
A career roll-up of a player's seasonal ES, built to reward sustained, high-level
play (longevity + peak), not a one-year wonder:

  • Take the player's best 9 ES seasons into 3 tiers (best 3 / next 3 / rest)
    weighted 50% / 30% / 20%.
  • Tiers fill bottom-up (a 3-season player gets one season in each tier; the big
    50% tier only fills once a long career exists), so a short career can't reach
    the top — you have to "do it for a long time."
  • Each season is also weighted by games played (availability), min(g,17)/17.

Net effect: 50 ≈ an average full-time starter's career; ~90-100 = all-time great;
short/part-time careers sit low by design.
"""

import argparse
import collections
import glob
import os
import re

from bs4 import BeautifulSoup

import models

PFR = os.environ.get("PFR_CACHE", r"C:\Users\excel\PycharmProjects\PythonProject2\pfr_cache")
YEAR_RE = re.compile(r"years_(\d{4})_")


def num(s):
    s = (s or "").replace(",", "").strip().replace("%", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_category(kind, table_id):
    """{year: {pfr_id: {data-stat: value}}} for one season-stat category."""
    out = collections.defaultdict(dict)
    for f in glob.glob(os.path.join(PFR, "*years_*_%s_htm.html" % kind)):
        m = YEAR_RE.search(os.path.basename(f))
        if not m:
            continue
        year = int(m.group(1))
        html = open(f, encoding="utf-8", errors="replace").read().replace("<!--", "").replace("-->", "")
        t = BeautifulSoup(html, "lxml").find("table", id=table_id)
        if not t or not t.find("tbody"):
            continue
        for tr in t.find("tbody").find_all("tr"):
            if "thead" in (tr.get("class") or []):
                continue
            cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
            pcell = cells.get("name_display")
            a = pcell.find("a") if pcell else None
            if not (a and a.get("href") and "/players/" in a["href"]):
                continue
            pid = a["href"].split("/players/", 1)[-1].rstrip("/")
            row = {k: cells[k].get_text(strip=True) for k in cells}
            row["pos"] = (row.get("pos") or "").upper()
            out[year][pid] = row
    return out


# ── stat specs ───────────────────────────────────────────────────────────────
# each: (label, weight, getter(row)->value, inverted?, counting?)
def g(k):
    return lambda r: num(r.get(k))


QB_STATS = [
    ("PassYds", 1.5, g("pass_yds"), False, True),
    ("PassTD", 1.75, g("pass_td"), False, True),
    ("Cmp", 1.0, g("pass_cmp"), False, True),
    ("INT", 1.0, g("pass_int"), True, True),
    ("Sacks", 0.5, g("pass_sacked"), True, True),
    ("RushYds", 0.5, g("rush_yds"), False, True),
    ("RushTD", 0.5, g("rush_td"), False, True),
    ("G", 1.0, g("games"), False, True),
]
RB_STATS = [
    ("RushYds", 1.5, g("rush_yds"), False, True),
    ("RushTD", 1.25, g("rush_td"), False, True),
    ("ATT", 1.0, g("rush_att"), False, True),
    ("Rec", 0.8, g("rec"), False, True),
    ("RecYds", 1.0, g("rec_yds"), False, True),
    ("RecTD", 0.8, g("rec_td"), False, True),
    ("FMB", 0.5, g("fumbles"), True, True),
    ("G", 1.0, g("games"), False, True),
]
WR_STATS = [
    ("RecYds", 1.5, g("rec_yds"), False, True),
    ("RecTD", 1.5, g("rec_td"), False, True),
    ("Rec", 1.25, g("rec"), False, True),
    ("Targets", 0.8, g("targets"), False, True),     # drops out pre-1992 (untracked)
    ("RushYds", 0.4, g("rush_yds"), False, True),    # jet sweeps / gadget usage
    ("FMB", 0.5, g("fumbles"), True, True),
    ("G", 1.0, g("games"), False, True),
]
# DEF getters (label, getter, inverted, counting); weights are per sub-role below.
_DEF_BASE = [
    ("Sacks", g("sacks"), False, True),
    ("INT", g("def_int"), False, True),
    ("TFL", g("tackles_loss"), False, True),
    ("Tackles", g("tackles_combined"), False, True),
    ("PD", g("pass_defended"), False, True),
    ("FF", g("fumbles_forced"), False, True),
    # DefTD removed: flukey, and the INT/fumble-recovery that scored it is already counted.
    ("FR", g("fumbles_rec"), False, True),
    ("G", g("games"), False, True),
]
_DEF_WEIGHTS = {
    "DL": {"Sacks": 2.0, "TFL": 1.5, "Tackles": 1.0, "FF": 1.0, "FR": 0.5, "INT": 0.5, "PD": 0.5, "G": 1.0},
    "LB": {"Tackles": 1.5, "TFL": 1.3, "Sacks": 1.2, "INT": 1.0, "PD": 1.0, "FF": 1.0, "FR": 0.5, "G": 1.0},
    "DB": {"INT": 1.5, "PD": 1.5, "Tackles": 1.0, "FF": 1.0, "Sacks": 0.8, "TFL": 0.8, "FR": 0.5, "G": 1.0},
}
DEF_STATS = [(label, 1.0, fn, inv, cnt) for label, fn, inv, cnt in _DEF_BASE]   # labels for averaging


def def_stats(group):
    w = _DEF_WEIGHTS[group]
    return [(label, w[label], fn, inv, cnt) for label, fn, inv, cnt in _DEF_BASE]


def def_group(pos):
    """Season-page defensive position -> DL / LB / DB. Matches by token because PFR
    side-prefixes positions (LDT, RDE, LOLB, RCB…).
        DL: DE RE LE DT NT (+EDGE, side-prefixed)   LB: anything ending LB
        DB: S FS SS CB NB (+side-prefixed corners)"""
    p = (pos or "").upper().strip()
    if not p:
        return None
    if p.endswith("LB"):
        return "LB"
    if p in ("RE", "LE") or "DE" in p or "DT" in p or "NT" in p or "EDGE" in p or p == "DL":
        return "DL"
    if "CB" in p or "NB" in p or "DB" in p or p in ("S", "FS", "SS") or p.endswith("S"):
        return "DB"
    return None


def qualifies(slot, r):
    """A real SEASON for a player (counts toward CES). Starter-pace based, no
    minimum game count — a short starting season still counts (CES down-weights it
    by games), so e.g. Brady's 12-game 2016 isn't thrown out."""
    games = num(r.get("games")) or 0
    if slot == "QB":
        att = num(r.get("pass_att")) or 0
        return games > 0 and att / games >= 14
    if slot == "RB":
        return (num(r.get("rush_att")) or 0) >= 100 and games >= 10
    if slot in ("WR", "TE"):
        tg = num(r.get("targets"))
        base = (tg >= 50) if tg is not None else (num(r.get("rec")) or 0) >= 35
        return base and games >= 10
    return games >= 12


def award_bonus(awards, slot):
    """Flat ES points for awards WON that season. Voted awards (MVP/OPOY/DPOY/
    OROY/DROY) only count on a 1st-place finish ('AP MVP-1' = won, '-5' = 5th);
    Pro Bowl + All-Pro are membership. Bonuses stack. CPOY intentionally excluded."""
    if not awards:
        return 0.0
    toks = [t.strip() for t in awards.split(",")]
    has = lambda code: code in toks
    won = lambda prefix: any(t.startswith(prefix) and t.endswith("-1") for t in toks)
    defense = slot == "DEF"
    b = 0.0
    if has("PB"):
        b += 2 if defense else 1
    if has("AP-1"):
        b += 5 if defense else 3
    elif has("AP-2"):
        b += 3 if defense else 2
    if won("AP MVP"):
        b += 10 if (defense or slot == "RB") else 7
    if defense:
        if won("AP DPoY"):
            b += 8
        if won("AP DRoY"):
            b += 4
    else:
        if won("AP OPoY"):
            b += 5
        if won("AP ORoY"):
            b += 3
    return b


def year_avgs(rows, stats):
    """Per-stat average over the given rows. 0 avg (untracked era) -> None (drop)."""
    avg = {}
    for label, w, fn, inv, cnt in stats:
        vals = []
        for r in rows:
            v = fn(r)
            if v is None and cnt:
                v = 0.0
            if v is not None:
                vals.append(v)
        a = (sum(vals) / len(vals)) if vals else None
        avg[label] = a if a else None
    return avg


def es(row, stats, avg):
    numer = denom = 0.0
    for label, w, fn, inv, cnt in stats:
        a = avg.get(label)
        if not a:
            continue
        v = fn(row)
        if v is None and cnt:
            v = 0.0
        if v is None:
            continue
        r = v / a
        r = max(-1.0, min(2.0, 2 - r)) if inv else max(0.0, min(3.0, r))
        numer += w * r
        denom += w
    return 50 * numer / denom if denom else None


TIER_OF_SLOT = [3, 2, 1, 3, 2, 1, 3, 2, 1]               # fill order: T3, T2, T1, repeated
TIER_W = {1: 0.50 / 3, 2: 0.30 / 3, 3: 0.20 / 3}


def ces(seasons):
    """seasons = list of (ES, games) -> Career Excel Score."""
    seasons = sorted(seasons, key=lambda s: s[0], reverse=True)[:9]
    counts = collections.Counter(TIER_OF_SLOT[i] for i in range(len(seasons)))
    assigned, idx = [], 0
    for tier in (1, 2, 3):                                # best ES seasons -> highest tiers
        for _ in range(counts[tier]):
            assigned.append((tier, seasons[idx]))
            idx += 1
    total = 0.0
    for tier, (e, games) in assigned:
        total += TIER_W[tier] * (min(games or 0, 17) / 17) * e
    return total


def merge(base, extra, keys):
    """{year:{pid:row}} = base rows with selected `keys` pulled in from `extra`
    (a QB's rushing totals / a RB's receiving totals onto one row)."""
    out = {}
    for y, byid in base.items():
        out[y] = {}
        for pid, r in byid.items():
            rr = dict(r)
            e = (extra.get(y, {}) if extra else {}).get(pid)
            if e:
                for k in keys:
                    rr[k] = e.get(k)
            out[y][pid] = rr
    return out


def _topN_avgs(rows_by_year, stats, posok, rankfn, n):
    """Baseline = average over the top-N producing seasons at the position that year."""
    per_year = {}
    for year, byid in rows_by_year.items():
        rows = sorted((r for r in byid.values() if posok(r["pos"])), key=rankfn, reverse=True)[:n]
        if rows:
            per_year[year] = year_avgs(rows, stats)
    return per_year


def build_context():
    """Parse the season pages once and precompute every per-year baseline."""
    cats = {k: parse_category(k, k) for k in ("passing", "rushing", "receiving", "defense")}
    ctx = {
        "QROWS": merge(cats["passing"], cats["rushing"], ("rush_yds", "rush_td")),
        "RROWS": merge(cats["rushing"], cats["receiving"], ("rec", "rec_yds", "rec_td")),
        "WROWS": merge(cats["receiving"], cats["rushing"], ("rush_yds",)),
        "DROWS": cats["defense"],
    }
    f = lambda *ks: (lambda r: sum((num(r.get(k)) or 0) for k in ks))
    ctx["QAVG"] = _topN_avgs(ctx["QROWS"], QB_STATS, lambda p: p == "QB", f("pass_yds", "rush_yds"), 50)
    ctx["RAVG"] = _topN_avgs(ctx["RROWS"], RB_STATS, lambda p: p in ("RB", "HB", "FB"), f("rush_yds", "rec_yds"), 100)
    ctx["WAVG"] = _topN_avgs(ctx["WROWS"], WR_STATS, lambda p: p == "WR", f("rec_yds", "rush_yds"), 100)
    ctx["TAVG"] = _topN_avgs(ctx["WROWS"], WR_STATS, lambda p: p == "TE", f("rec_yds", "rush_yds"), 50)
    def_rank = lambda r: ((num(r.get("tackles_combined")) or 0) + 6 * (num(r.get("sacks")) or 0)
                          + 8 * (num(r.get("def_int")) or 0) + 3 * (num(r.get("pass_defended")) or 0)
                          + 6 * (num(r.get("fumbles_forced")) or 0) + 2 * (num(r.get("tackles_loss")) or 0))
    ctx["DAVG"] = {grp: _topN_avgs(ctx["DROWS"], DEF_STATS, (lambda p, grp=grp: def_group(p) == grp),
                                   def_rank, 50) for grp in ("DL", "LB", "DB")}
    return ctx


def _collect(rows_by_year, slot, stats, avg_by_year, pid):
    s = []
    for y, byid in rows_by_year.items():
        r = byid.get(pid)
        if r and qualifies(slot, r) and avg_by_year.get(y):
            e = es(r, stats, avg_by_year[y])
            if e is not None:
                s.append((e + award_bonus(r.get("awards"), slot), num(r.get("games"))))
    return s


def player_ces(pfr_id, slot, ctx):
    """(CES, qualifying_seasons) for one player. 0.0 if no qualifying NFL seasons."""
    if not pfr_id:
        return 0.0, 0
    if slot == "QB":
        seasons = _collect(ctx["QROWS"], "QB", QB_STATS, ctx["QAVG"], pfr_id)
    elif slot == "RB":
        seasons = _collect(ctx["RROWS"], "RB", RB_STATS, ctx["RAVG"], pfr_id)
    elif slot == "WR":
        seasons = _collect(ctx["WROWS"], "WR", WR_STATS, ctx["WAVG"], pfr_id)
    elif slot == "TE":
        seasons = _collect(ctx["WROWS"], "TE", WR_STATS, ctx["TAVG"], pfr_id)
    else:
        seasons = []
        for y, byid in ctx["DROWS"].items():
            r = byid.get(pfr_id)
            if not (r and qualifies("DEF", r)):
                continue
            grp = def_group(r["pos"])
            if grp and ctx["DAVG"][grp].get(y):
                e = es(r, def_stats(grp), ctx["DAVG"][grp][y])
                if e is not None:
                    seasons.append((e + award_bonus(r.get("awards"), "DEF"), num(r.get("games"))))
    return (ces(seasons) if seasons else 0.0), len(seasons)


def compute_pool_ces():
    """{prospect_id: CES} for every player in the pool."""
    ctx = build_context()
    return {p["id"]: player_ces(p.get("pfr_id"), p["slot"], ctx)[0]
            for p in models.get_prospects()}


def build_db():
    mapping = compute_pool_ces()
    n = models.set_ces(mapping)
    nonzero = sum(1 for v in mapping.values() if v)
    print("Stored CES for %d prospects (%d with a graded career)." % (n, nonzero))


def _print_top10():
    ctx = build_context()
    by_slot = collections.defaultdict(list)
    for p in models.get_prospects():
        c, ns = player_ces(p.get("pfr_id"), p["slot"], ctx)
        if ns:
            by_slot[p["slot"]].append((c, ns, p["name"]))
    for slot in ("QB", "RB", "WR", "TE", "DEF"):
        print("\n=== TOP 10 CES — %s ===" % slot)
        for c, ns, name in sorted(by_slot[slot], reverse=True)[:10]:
            print("  %6.1f  %-26s (%d qualifying seasons)" % (c, name, ns))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="WarRoom ES/CES scoring")
    ap.add_argument("--build", action="store_true", help="compute CES for the pool and write to warroom.db")
    args = ap.parse_args()
    models.init_db()
    if args.build:
        build_db()
    else:
        _print_top10()
