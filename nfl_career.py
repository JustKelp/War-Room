"""
WarRoom — NFL career aggregation for the draft-board eligibility filter.

The board should only carry players with a real NFL footprint; otherwise the
reveal and the relative scoring mean nothing ("players won't know them"). The
product-owner rule (2026-06-10): a player belongs on the board only if they
average **>= 6 games played and >= 2 games started per active season** across
their NFL career.

We compute that entirely from local data: PythonProject2 cached every team's PFR
roster page for every season (2000-2026), and each roster row carries that
season's games (g) and games started (gs) plus the player's pfr_id. We aggregate
across all of them, keyed by pfr_id:

    g       total career games
    gs      total career games started
    seasons number of distinct ACTIVE seasons (years the player logged g > 0)

Averages are derived at filter time (g/seasons, gs/seasons). Players who never
appear with g > 0 (never on the board source data, or pure practice-squad/IR
careers) are absent from the map and therefore excluded.

The aggregation is cached to JSON so it runs once.

    python nfl_career.py            # build/refresh the cache, print coverage
    python nfl_career.py --rebuild
"""

import argparse
import glob
import json
import os
import re

from bs4 import BeautifulSoup

PFR_CACHE = os.environ.get(
    "PFR_CACHE",
    r"C:\Users\excel\PycharmProjects\PythonProject2\pfr_cache",
)
ROSTER_GLOB = "*_roster_htm.html"
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "nfl_career.json")
_YEAR_RE = re.compile(r"_((?:19|20)\d{2})_roster")


def _int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _pfr_id(a) -> str | None:
    if a and a.get("href") and "/players/" in a["href"]:
        return a["href"].split("/players/", 1)[-1].rstrip("/")
    return None


def build(verbose: bool = True) -> dict:
    """Aggregate every cached roster page into {pfr_id: {g, gs, seasons}}."""
    files = sorted(glob.glob(os.path.join(PFR_CACHE, ROSTER_GLOB)))
    acc: dict[str, dict] = {}
    for f in files:
        m = _YEAR_RE.search(os.path.basename(f))
        if not m:
            continue
        year = m.group(1)
        html = open(f, encoding="utf-8", errors="replace").read()
        soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
        table = soup.find("table", id="roster")
        if table is None or table.find("tbody") is None:
            continue
        for tr in table.find("tbody").find_all("tr"):
            if "thead" in (tr.get("class") or []):
                continue
            cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
            if "player" not in cells:
                continue
            pid = _pfr_id(cells["player"].find("a"))
            if not pid:
                continue
            g = _int(cells["g"].get_text(strip=True)) if "g" in cells else 0
            gs = _int(cells["gs"].get_text(strip=True)) if "gs" in cells else 0
            av = _int(cells["av"].get_text(strip=True)) if "av" in cells else 0
            g, gs, av = g or 0, gs or 0, av or 0
            rec = acc.setdefault(pid, {"g": 0, "gs": 0, "av": 0, "_years": set()})
            rec["g"] += g
            rec["gs"] += gs
            rec["av"] += av                 # career Approximate Value (scoring metric)
            if g > 0:                       # only seasons the player actually played
                rec["_years"].add(year)

    out = {pid: {"g": r["g"], "gs": r["gs"], "av": r["av"], "seasons": len(r["_years"]),
                 "first_year": min(int(y) for y in r["_years"])}
           for pid, r in acc.items() if r["_years"]}
    if verbose:
        print(f"  parsed {len(files)} roster files -> {len(out)} players with NFL game time")
    return out


def load(rebuild: bool = False, verbose: bool = False) -> dict:
    if not rebuild and os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    data = build(verbose=verbose)
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return data


def passes(career: dict, pfr_id: str | None, min_gp: float, min_gs: float) -> bool:
    """True if the player averages >= min_gp games and >= min_gs starts per
    active season. Unknown player (no NFL game time) -> False."""
    if not pfr_id:
        return False
    rec = career.get(pfr_id)
    if not rec or not rec.get("seasons"):
        return False
    s = rec["seasons"]
    return (rec["g"] / s) >= min_gp and (rec["gs"] / s) >= min_gs


def main():
    ap = argparse.ArgumentParser(description="Build NFL career games/starts aggregation")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    data = load(rebuild=args.rebuild, verbose=True)
    # quick sanity: how many pass the default 6 GP / 2 GS rule
    ok = sum(1 for r in data.values()
             if r["seasons"] and r["g"] / r["seasons"] >= 6 and r["gs"] / r["seasons"] >= 2)
    print(f"  players in map: {len(data)}; pass 6GP/2GS rule: {ok}")
    print(f"  cache: {CACHE_PATH}")


if __name__ == "__main__":
    main()
