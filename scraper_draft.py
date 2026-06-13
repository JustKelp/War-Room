"""
WarRoom — draft-class source (post-pivot pool definition).

The pool is no longer the scouting-report rows; it is the players actually
drafted in the first three rounds of the last ten NFL drafts (2016-2025),
across the roster positions the game uses (QB/RB/WR/TE/DEF). This adapter reads
those names from Pro-Football-Reference's per-year draft tables.

Each PFR draft row is a goldmine: round, overall pick, position, college, the
player's PFR id (NFL outcome data / scoring), AND a `college_link` cell that
links straight to the player's Sports-Reference CFB page. That direct link lets
the college-stats scraper skip name-search entirely and fetch a verified page,
so the name-matching the proposal worried about (§8.9) is sidestepped for
drafted players. The row also carries NFL career Approximate Value (career_av) —
a real, defensible scoring signal that finally replaces the §6 placeholder.

PFR is on the Sports-Reference network and hard-blocks plain HTTP (Cloudflare
403), so pages go through the browser renderer, same as the other SR scrapes.
Only ten pages, one per year, so a full run is cheap.

    python scraper_draft.py                 # all years 2016-2025, rounds 1-3
    python scraper_draft.py --years 2024 2025
    python scraper_draft.py --dry-run       # parse + print, don't write
"""

import argparse

from bs4 import BeautifulSoup

import models
import scrape_common as sc

SOURCE = "pfr_draft"
BASE = "https://www.pro-football-reference.com"
DRAFT_URL = BASE + "/years/{year}/draft.htm"
CACHE_SUB = "draft"

DEFAULT_YEARS = list(range(2016, 2026))   # 2016-2025 inclusive (last 10 classes)
MAX_ROUND = 3


def _num(cells: dict, *keys) -> float | None:
    for k in keys:
        v = cells.get(k)
        if v not in (None, ""):
            try:
                return float(str(v).replace(",", ""))
            except ValueError:
                pass
    return None


def parse_draft(html: str, year: int) -> list[dict]:
    """Parse one year's draft table into prospect dicts (rounds 1-MAX_ROUND,
    draftable positions only)."""
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    table = soup.find("table", id="drafts")
    if table is None or table.find("tbody") is None:
        return []

    out = []
    for tr in table.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):       # repeated header rows
            continue
        cells = {c.get("data-stat"): c.get_text(strip=True)
                 for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
        if not cells.get("player"):
            continue

        rnd = _num(cells, "draft_round")
        if rnd is None or rnd > MAX_ROUND:
            continue

        slot = models.position_to_slot(cells.get("pos", ""))
        if slot is None:                              # OL/K/P/LS etc. — not drafted by the game
            continue

        # PFR player id and the direct CFB stats link live in cell anchors.
        pfr_id, cfb_url = None, None
        pcell = tr.find(attrs={"data-stat": "player"})
        a = pcell.find("a") if pcell else None
        if a and a.get("href"):
            pfr_id = a["href"].split("/players/", 1)[-1].rstrip("/")
        ccell = tr.find(attrs={"data-stat": "college_link"})
        ca = ccell.find("a") if ccell else None
        if ca and ca.get("href"):
            # SR's JS decorates these links with ?__hstc=... tracking params;
            # strip the query so the URL stays a clean .html player page.
            cfb_url = ca["href"].replace("http://", "https://").split("?", 1)[0]

        out.append({
            "source": SOURCE,
            "source_url": DRAFT_URL.format(year=year),
            "name": cells["player"],
            "slot": slot,
            "position": cells.get("pos", ""),
            "school": cells.get("college_id") or None,
            "draft_year": year,
            "draft_round": int(rnd),
            "draft_pick": int(_num(cells, "draft_pick") or 0) or None,
            "pfr_id": pfr_id,
            "cfb_url": cfb_url,
            "nfl_av": _num(cells, "career_av"),
            "nfl_games": int(_num(cells, "g") or 0) or None,
            # card data (measurements/conference/college stats) is filled later
            # by scraper_collegestats from the cfb_url above.
            "height": None, "weight": None, "hand": None, "forty": None,
            "projected_round": str(int(rnd)), "grade": None,
            "report": None, "blind_report": None,
        })
    return out


def scrape(years: list[int], save: bool = True) -> list[dict]:
    all_rows = []
    for year in years:
        url = DRAFT_URL.format(year=year)
        html = sc.render(url, sub=CACHE_SUB, min_len=20000)
        if not html:
            print(f"  {year}: [no html]")
            continue
        rows = parse_draft(html, year)
        if save:
            for r in rows:
                models.upsert_prospect(r)
        by_slot = {}
        for r in rows:
            by_slot[r["slot"]] = by_slot.get(r["slot"], 0) + 1
        print(f"  {year}: {len(rows):3} players  {by_slot}")
        all_rows.extend(rows)
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Scrape NFL draft classes (PFR), rounds 1-3")
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS,
                    help="Draft years (default 2016-2025)")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    args = ap.parse_args()

    models.init_db()
    print(f"Scraping PFR draft classes {args.years} (rounds 1-{MAX_ROUND}, "
          f"slots {models.ROSTER_SLOTS})")
    try:
        rows = scrape(args.years, save=not args.dry_run)
    finally:
        sc.close_driver()

    print(f"\n  Parsed {len(rows)} drafted players.")
    if not args.dry_run:
        print("  pool summary:", models.pool_summary())


if __name__ == "__main__":
    main()
