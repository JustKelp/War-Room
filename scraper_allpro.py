"""
WarRoom — All-Pro additions.

Adds AP first- and second-team All-Pro players (2015-2020) who aren't already in
the pool — capturing stars missed by the R1-3 / year filters (late-round picks
like Antonio Brown, undrafted players, pre-2010 vets).

PFR's per-year All-Pro page (`/years/{year}/allpro.htm`, table id `all_pro`)
lists every selection with an `all_pro_string` (e.g. "AP: 1st Tm" / "AP: 2nd
Tm") and a link to each player. That page has no college info, so for each NEW
player we render their PFR player page to pull the CFB stats link, college,
draft year, and measurements (height/weight) — everything the card needs except
the college stat line, which scraper_collegestats fills next via the cfb_url.

Scoring needs no draft scrape: tenure-AV uses draft year (parsed here) or, when
undrafted, the player's first NFL season from the career aggregation.

    python scraper_allpro.py                 # 2015-2020, AP 1st+2nd team
    python scraper_allpro.py --years 2018
    python scraper_allpro.py --dry-run
"""

import argparse
import re

from bs4 import BeautifulSoup

import models
import scrape_common as sc

SOURCE = "pfr_allpro"
BASE = "https://www.pro-football-reference.com"
ALLPRO_URL = BASE + "/years/{year}/allpro.htm"
DEFAULT_YEARS = list(range(2015, 2021))   # 2015-2020 inclusive


def parse_allpro(html: str) -> list[dict]:
    """AP 1st/2nd-team players at draftable positions from one year's page."""
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    table = soup.find("table", id="all_pro")
    out = []
    if table is None or table.find("tbody") is None:
        return out
    for tr in table.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
        if "player" not in cells:
            continue
        aps = cells["all_pro_string"].get_text(strip=True) if "all_pro_string" in cells else ""
        first = "AP: 1st Tm" in aps
        if not (first or "AP: 2nd Tm" in aps):
            continue
        slot = models.position_to_slot(cells["pos"].get_text(strip=True) if "pos" in cells else "")
        if slot is None:
            continue
        a = cells["player"].find("a")
        if not (a and a.get("href")):
            continue
        out.append({"pfr_id": a["href"].split("/players/", 1)[-1].rstrip("/"),
                    "name": cells["player"].get_text(strip=True),
                    "pos": cells["pos"].get_text(strip=True), "slot": slot,
                    "team": "1st" if first else "2nd"})
    return out


def player_info(pfr_id: str) -> dict | None:
    """CFB link, college, draft year, and measurements from a player's PFR page."""
    html = sc.render(BASE + "/players/" + pfr_id, sub="allpro_player", min_len=4000)
    if not html:
        return None
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    meta = soup.find(id="meta")
    txt = meta.get_text(" ", strip=True) if meta else ""
    cfb = None
    for a in (meta.find_all("a") if meta else []):
        if "sports-reference.com/cfb/players/" in a.get("href", ""):
            cfb = a["href"].split("?", 1)[0].replace("http://", "https://")
            break
    hw = re.search(r"(\d-\d{1,2})\s*,\s*(\d{2,3})\s*lb", txt)
    dy = re.search(r"of the (\d{4}) NFL Draft", txt)
    coll = re.search(r"College\s*:\s*([A-Za-z .&'-]+?)\s*\(", txt)
    return {"cfb_url": cfb,
            "height": hw.group(1) if hw else None,
            "weight": int(hw.group(2)) if hw else None,
            "draft_year": int(dy.group(1)) if dy else 0,
            "school": coll.group(1).strip() if coll else None}


def run(years: list[int], save: bool = True) -> int:
    seen: dict[str, dict] = {}
    for year in years:
        html = sc.render(ALLPRO_URL.format(year=year), sub="allpro", min_len=20000)
        if not html:
            print(f"  {year}: [no html]")
            continue
        rows = parse_allpro(html)
        for r in rows:                       # dedupe across years; first-team wins
            if r["pfr_id"] not in seen or r["team"] == "1st":
                seen[r["pfr_id"]] = r
        print(f"  {year}: {len(rows)} AP all-pro selections (kept slots)")

    existing = models.prospect_pfr_ids()
    new = [r for pid, r in seen.items() if pid not in existing]
    print(f"\n  unique AP all-pros: {len(seen)} | already in pool: {len(seen) - len(new)} | NEW: {len(new)}")

    added = 0
    for r in new:
        info = player_info(r["pfr_id"])
        if not info:
            print(f"    ?  {r['name']:26} (no player page)")
            continue
        if save:
            models.upsert_prospect({
                "source": SOURCE, "source_url": BASE + "/players/" + r["pfr_id"],
                "name": r["name"], "slot": r["slot"], "position": r["pos"],
                "school": info["school"], "draft_year": info["draft_year"],
                "height": info["height"], "weight": info["weight"], "hand": None, "forty": None,
                "projected_round": None, "grade": None, "report": None, "blind_report": None,
                "pfr_id": r["pfr_id"], "draft_round": None, "draft_pick": None,
                "cfb_url": info["cfb_url"], "nfl_av": None, "nfl_games": None, "is_starter": 0,
            })
        added += 1
        print(f"    +  {r['name']:26} {r['slot']:4} AP-{r['team']}  dy={info['draft_year']}  "
              f"cfb={'Y' if info['cfb_url'] else 'N'}  {info['school'] or ''}")
    return added


def main():
    ap = argparse.ArgumentParser(description="Add AP All-Pro players (PFR) not already in the pool")
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    models.init_db()
    print(f"Scraping AP All-Pro {args.years} (1st + 2nd team)")
    try:
        n = run(args.years, save=not args.dry_run)
    finally:
        sc.close_driver()
    print(f"\n  Added {n} new All-Pro players.")
    if not args.dry_run:
        print("  pool:", models.pool_summary()["total"])
        print("  Next: run scraper_collegestats.py --no-filter to fill their college cards.")


if __name__ == "__main__":
    main()
