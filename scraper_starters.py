"""
WarRoom — current-starters source (pool expansion).

Adds today's NFL starters to the pool alongside the drafted-R1-3 players. Rather
than scrape anything new, this reuses the PFR team roster pages already cached by
PythonProject2 (`pfr_cache/..._2025_roster_htm.html`, one per team). The roster
table carries everything we need locally: games started (gs), position, school,
height, weight, season AV, and the player's pfr_id.

Definition of a current starter (per product owner, 2026-06-10): a player who
**started 2+ games in 2025** (gs >= 2) at a roster position the game uses
(QB/RB/WR/TE/DEF).

Dedup: many starters are already in the pool as drafted R1-3 players. We match on
pfr_id — if a starter is already there, we just flag is_starter=1 (and backfill
height/weight); only genuinely new players (older vets, late-round, undrafted)
get inserted as source='pfr_starter'. New starters have no cfb_url, so the
college-stats scrape resolves them by name search (school-verified).

    python scraper_starters.py --dry-run     # parse + counts, no DB writes
    python scraper_starters.py               # apply to the pool
"""

import argparse
import glob
import os
import re

from bs4 import BeautifulSoup

import models

PFR_CACHE = os.environ.get(
    "PFR_CACHE",
    r"C:\Users\excel\PycharmProjects\PythonProject2\pfr_cache",
)
ROSTER_GLOB = "*_2025_roster_htm.html"
SOURCE = "pfr_starter"
MIN_GS = 2


def _int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _draft_year(text: str) -> int:
    """Best-effort draft/entry year from the roster's draft_info cell
    (e.g. '... of the 2017 NFL Draft', or 'Undrafted Free Agent, 2019'). 0 if
    none — keeps the (name_key, draft_year) college-stats key non-NULL."""
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")
    return int(years[-1]) if years else 0


def parse_roster(html: str, source_url: str) -> list[dict]:
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    table = soup.find("table", id="roster")
    if table is None or table.find("tbody") is None:
        return []

    out = []
    for tr in table.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
        if "player" not in cells or not cells["player"].get_text(strip=True):
            continue

        gs = _int(cells["gs"].get_text(strip=True)) if "gs" in cells else None
        if not gs or gs < MIN_GS:
            continue
        pos = cells["pos"].get_text(strip=True) if "pos" in cells else ""
        slot = models.position_to_slot(pos)
        if slot is None:
            continue

        a = cells["player"].find("a")
        pfr_id = None
        if a and a.get("href") and "/players/" in a["href"]:
            pfr_id = a["href"].split("/players/", 1)[-1].rstrip("/")

        def txt(k):
            return cells[k].get_text(strip=True) if k in cells else ""

        out.append({
            "source": SOURCE, "source_url": source_url,
            "name": cells["player"].get_text(strip=True),
            "slot": slot, "position": pos,
            "school": txt("college_id") or None,
            "draft_year": _draft_year(txt("draft_info")),
            "height": txt("height") or None,
            "weight": _int(txt("weight")),
            "hand": None, "forty": None,
            "projected_round": None, "grade": None,
            "report": None, "blind_report": None,
            "pfr_id": pfr_id,
            "draft_round": None, "draft_pick": None, "cfb_url": None,
            "nfl_av": float(txt("av")) if txt("av") else None,
            "nfl_games": _int(txt("g")),
            "is_starter": 1,
            "gs": gs,
        })
    return out


def run(save: bool) -> dict:
    files = sorted(glob.glob(os.path.join(PFR_CACHE, ROSTER_GLOB)))
    if not files:
        raise SystemExit(f"No 2025 roster files under {PFR_CACHE}")

    existing = models.prospect_pfr_ids()
    starters, seen = [], set()
    for f in files:
        url = "https://" + re.sub(r"_htm\.html$", ".htm", os.path.basename(f)[len("https___"):]).replace("_", "/")
        for p in parse_roster(open(f, encoding="utf-8", errors="replace").read(), url):
            key = p["pfr_id"] or models.norm_name(p["name"])
            if key in seen:                 # traded mid-season → on two rosters
                continue
            seen.add(key)
            starters.append(p)

    matched = [p for p in starters if p["pfr_id"] and p["pfr_id"] in existing]
    new = [p for p in starters if not (p["pfr_id"] and p["pfr_id"] in existing)]

    if save:
        for p in matched:
            models.mark_starter(p["pfr_id"], p["height"], p["weight"])
        for p in new:
            models.upsert_prospect(p)

    by_slot = {}
    for p in new:
        by_slot[p["slot"]] = by_slot.get(p["slot"], 0) + 1
    return {"teams": len(files), "starters": len(starters),
            "already_in_pool": len(matched), "new": len(new), "new_by_slot": by_slot}


def main():
    ap = argparse.ArgumentParser(description="Add 2025 starters (gs>=2) from cached PFR rosters")
    ap.add_argument("--dry-run", action="store_true", help="Parse + report, don't write")
    args = ap.parse_args()

    models.init_db()
    print(f"Reading 2025 rosters from {PFR_CACHE} (gs>={MIN_GS}, slots {models.ROSTER_SLOTS})")
    res = run(save=not args.dry_run)
    print(f"  teams: {res['teams']}")
    print(f"  starters (gs>=2, kept slots): {res['starters']}")
    print(f"  already in pool (drafted R1-3): {res['already_in_pool']}  -> flagged is_starter")
    print(f"  new players added: {res['new']}  {res['new_by_slot']}")
    if not args.dry_run:
        print("  pool summary:", models.pool_summary())


if __name__ == "__main__":
    main()
