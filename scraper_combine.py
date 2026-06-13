"""
WarRoom — physical measurements source (draft-card data).

The redesigned card (memo §2) leads with physical measurements, but a key data
finding (2026-06-10): Sports-Reference CFB player pages do NOT carry height /
weight / 40-time — their meta header is only School/Position/Draft. So the
college-stats scrape cannot supply measurements.

PFR's per-year combine pages do: one table of height, weight, 40-yard dash (plus
other drills) for every combine invitee, with links back to the player's PFR
page. We render one page per draft year and stamp height/weight/forty onto the
matching prospect rows (matched by pfr_id, falling back to normalized name).

Gap: players who skipped the combine have no row here and keep NULL measurements
(acceptable — the card layer can hide a missing field or such players can be held
out of the daily pool, same approach as the earlier measurable gaps).

PFR is Cloudflare-gated, so pages go through the browser renderer.

    python scraper_combine.py                 # all years 2016-2025
    python scraper_combine.py --years 2024 2025
    python scraper_combine.py --dry-run
"""

import argparse

from bs4 import BeautifulSoup

import models
import scrape_common as sc

BASE = "https://www.pro-football-reference.com"
COMBINE_URL = BASE + "/draft/{year}-combine.htm"
CACHE_SUB = "combine"
DEFAULT_YEARS = list(range(2016, 2026))


def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def parse_combine(html: str) -> tuple[dict, dict]:
    """Return (by_pfr, by_name) maps of measurements from one combine page.
    Only rows whose position maps to a roster slot are kept."""
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    table = soup.find("table", id="combine")
    by_pfr, by_name = {}, {}
    if table is None or table.find("tbody") is None:
        return by_pfr, by_name

    for tr in table.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {c.get("data-stat"): c for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
        name = cells.get("player")
        if name is None or not name.get_text(strip=True):
            continue
        pos = cells["pos"].get_text(strip=True) if "pos" in cells else ""
        if models.position_to_slot(pos) is None:
            continue

        m = {
            "height": (cells["height"].get_text(strip=True) or None) if "height" in cells else None,
            "weight": _i(cells["weight"].get_text(strip=True)) if "weight" in cells else None,
            "forty": _f(cells["forty_yd"].get_text(strip=True)) if "forty_yd" in cells else None,
        }
        a = cells["player"].find("a")
        pfr_id = None
        if a and a.get("href") and "/players/" in a["href"]:
            pfr_id = a["href"].split("/players/", 1)[-1].rstrip("/")
        if pfr_id:
            by_pfr[pfr_id] = m
        by_name[models.norm_name(name.get_text(strip=True))] = m
    return by_pfr, by_name


def scrape(years: list[int], save: bool = True) -> int:
    total = 0
    for year in years:
        html = sc.render(COMBINE_URL.format(year=year), sub=CACHE_SUB, min_len=20000)
        if not html:
            print(f"  {year}: [no html]")
            continue
        by_pfr, by_name = parse_combine(html)
        n = models.apply_measurements(year, by_pfr, by_name) if save else 0
        print(f"  {year}: combine rows pfr={len(by_pfr)} name={len(by_name)}  "
              f"prospects updated={n}")
        total += n
    return total


def main():
    ap = argparse.ArgumentParser(description="Scrape NFL combine measurements (PFR)")
    ap.add_argument("--years", type=int, nargs="+", default=DEFAULT_YEARS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    models.init_db()
    print(f"Scraping PFR combine measurements {args.years}")
    try:
        n = scrape(args.years, save=not args.dry_run)
    finally:
        sc.close_driver()
    print(f"\n  Updated {n} prospect rows with measurements.")
    if not args.dry_run:
        print("  measurement coverage:", models.measurement_coverage())


if __name__ == "__main__":
    main()
