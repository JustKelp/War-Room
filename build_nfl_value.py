"""
WarRoom — build our own career-value inputs (replaces PFR's AV for scoring).

Career Value uses only public NFL honors + role, which are inherently fair
across career lengths (a 4-year star and a 12-year compiler are separated by
honors, not accumulated yardage):

    all_pro             1st-team All-Pro seasons
    pro_bowls           Pro Bowl selections
    primary_starter_yrs years as a primary starter

For the 908 drafted players these three are already columns in the PFR draft
tables we cached — so this pass re-parses those cached pages with NO scraping.
The ~300 non-drafted players (current starters / older All-Pros) need their PFR
player page; that's a later pass (`--players`, renders) — until then they fall
back to a games-started proxy in app._career_value.

    python build_nfl_value.py            # re-parse cached draft pages (free)
"""

import argparse
import glob
import os

from bs4 import BeautifulSoup

import models
import scrape_common as sc

DRAFT_CACHE = os.path.join(sc.CACHE_ROOT, "draft")


def _i(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def from_cached_drafts() -> int:
    """Populate nfl_value for every drafted player from the cached draft pages."""
    n = 0
    for f in sorted(glob.glob(os.path.join(DRAFT_CACHE, "*.html"))):
        soup = BeautifulSoup(open(f, encoding="utf-8", errors="replace").read()
                             .replace("<!--", "").replace("-->", ""), "lxml")
        table = soup.find("table", id="drafts")
        if not table or not table.find("tbody"):
            continue
        for tr in table.find("tbody").find_all("tr"):
            if "thead" in (tr.get("class") or []):
                continue
            pcell = tr.find(attrs={"data-stat": "player"})
            a = pcell.find("a") if pcell else None
            if not (a and a.get("href") and "/players/" in a["href"]):
                continue
            pfr_id = a["href"].split("/players/", 1)[-1].rstrip("/")
            cells = {c.get("data-stat"): c.get_text(strip=True)
                     for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
            models.upsert_nfl_value({
                "pfr_id": pfr_id,
                "games": _i(cells.get("g")),
                "all_pro": _i(cells.get("all_pros_first_team")) or 0,
                "pro_bowls": _i(cells.get("pro_bowls")) or 0,
                "primary_starter_yrs": _i(cells.get("years_as_primary_starter")) or 0,
                "source": "draft",
            })
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Build Career-Value inputs (own AV replacement)")
    ap.add_argument("--players", action="store_true",
                    help="(later) also render non-drafted players' PFR pages for honors")
    ap.parse_args()
    models.init_db()
    n = from_cached_drafts()
    print(f"nfl_value rows from cached draft pages: {n}")
    print("  (non-drafted players use a games-started fallback until the --players pass)")


if __name__ == "__main__":
    main()
