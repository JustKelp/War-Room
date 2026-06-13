"""
WarRoom — WalterFootball scouting-report scraper (the historical backbone, §5).

WalterFootball publishes static per-position draft pages, e.g.
https://walterfootball.com/draft2024QB.php . Coverage runs ~2010 → current.

The page markup changed over time, so we dispatch between two parsers:

  Modern (≈2022+): Bootstrap `panel` blocks, each prospect linking to a
  `scoutingreport...php` page; measurables + report in a `panel-body`.

  Legacy (≈2010–2021): flat `<li>` blocks — a `<b>` heading
  ("Name, POS, School / Height / Weight / 40 / Projected Round"), then
  "<i>date:</i> report text".

Both yield the same normalized prospect dict; identifying tokens are stripped
into a blind report (shared scrape_common.redact). Rows are stored via models.

Run:
    python scraper_walterfootball.py                 # all years, all slots
    python scraper_walterfootball.py --years 2024    # one year
    python scraper_walterfootball.py --dry-run
"""

import argparse
import re

from bs4 import BeautifulSoup

import models
import scrape_common as sc

SOURCE = "walterfootball"

# All draft years WalterFootball exposes (missing years 404 and are skipped).
ALL_YEARS = list(range(2010, 2026))
# Position pages that exist and map to a WarRoom roster slot. DEF is assembled
# from several defensive pages (LB/EDGE/SS/FS/DB pages 404 on this site).
ALL_POSITIONS = ["QB", "RB", "WR", "DE", "DT", "NT", "OLB", "ILB", "CB", "S"]


# ── PARSING ──────────────────────────────────────────────────────────────────

def _num(s: str) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return float(m.group()) if m else None


def _parse_measurables(heading_text: str) -> dict:
    out = {"height": None, "weight": None, "hand": None, "forty": None, "projected_round": None}
    if m := re.search(r"Height:\s*([\d\-]+)", heading_text):
        out["height"] = m.group(1)
    if m := re.search(r"Weight:\s*(\d+)", heading_text):
        out["weight"] = int(m.group(1))
    if m := re.search(r"Hand:\s*(\d+(?:\.\d+)?)", heading_text):
        out["hand"] = m.group(1)
    if m := re.search(r"40 Time:\s*(\d+(?:\.\d+)?)", heading_text):
        out["forty"] = float(m.group(1))
    if m := re.search(r"Projected Round[^:]*:\s*([^\n]+?)(?:\s{2,}|$)", heading_text):
        out["projected_round"] = m.group(1).strip().rstrip(".")
    return out


def _split_heading(heading_text: str, page_pos: str) -> tuple[str, str | None]:
    """From 'Name, POS, School  Height: ...' pull POS + school."""
    pos, school = page_pos, None
    m = re.search(r",\s*([A-Za-z/]{1,4}),\s*(.+?)(?:\s+Height:|\s*$)", heading_text)
    if m:
        pos = m.group(1).upper().strip()
        school = m.group(2).strip()
    return pos, school


def _row(name, slot, pos, school, year, url, report, meas) -> dict:
    return {
        "source": SOURCE, "source_url": url, "name": name, "slot": slot,
        "position": pos, "school": school, "draft_year": year,
        "projected_round": meas.get("projected_round"), "grade": None,
        "report": report, "blind_report": sc.redact(report, name, school or ""),
        "pfr_id": None,
        "height": meas.get("height"), "weight": meas.get("weight"),
        "hand": meas.get("hand"), "forty": meas.get("forty"),
    }


def _parse_modern(soup, page_pos, year, url) -> list[dict]:
    prospects = []
    for a in soup.select('a[href*="scoutingreport"]'):
        name = a.get_text(strip=True).rstrip("*").strip()
        if not name:
            continue
        b = a.find_parent(["b", "strong"])
        if not b:
            continue
        heading_text = b.get_text(" ", strip=True)
        pos, school = _split_heading(heading_text, page_pos)
        slot = models.position_to_slot(pos) or models.position_to_slot(page_pos)
        if not slot:
            continue
        meas = _parse_measurables(heading_text)
        report = ""
        heading_div = b.find_parent("div")
        body = heading_div.find_next_sibling("div", class_="panel-body") if heading_div else None
        if body:
            report = "\n\n".join(
                p.get_text(" ", strip=True) for p in body.find_all("p") if p.get_text(strip=True)
            )
        prospects.append(_row(name, slot, pos, school, year, url, report, meas))
    return prospects


def _parse_legacy(soup, page_pos, year, url) -> list[dict]:
    """Flat <li> blocks used on ~2010–2021 pages."""
    prospects = []
    for li in soup.find_all("li"):
        b = li.find("b")
        if not b:
            continue
        heading_text = b.get_text(" ", strip=True)
        if "Height:" not in heading_text and "Projected Round" not in heading_text:
            continue
        # name = text before the first comma, sans trailing asterisks
        name = re.split(r",", heading_text)[0].rstrip("*").strip()
        if not name:
            continue
        pos, school = _split_heading(heading_text, page_pos)
        slot = models.position_to_slot(pos) or models.position_to_slot(page_pos)
        if not slot:
            continue
        meas = _parse_measurables(heading_text)
        # report = the <li> text after the heading, minus a leading "date:" stamp
        full = li.get_text(" ", strip=True)
        report = full[len(heading_text):].strip() if full.startswith(heading_text) else full
        report = re.sub(r"^\d{1,2}/\d{1,2}/\d{2,4}:\s*", "", report).strip()
        if not report:
            continue
        prospects.append(_row(name, slot, pos, school, year, url, report, meas))
    return prospects


def parse_position_page(html: str, page_pos: str, year: int, url: str) -> list[dict]:
    """Dispatch to the modern parser, falling back to the legacy one."""
    soup = BeautifulSoup(html, "lxml")
    rows = _parse_modern(soup, page_pos, year, url)
    if not rows:
        rows = _parse_legacy(soup, page_pos, year, url)
    return rows


# ── RUN ──────────────────────────────────────────────────────────────────────

def scrape(years: list[int], positions: list[str], save: bool = True) -> list[dict]:
    all_rows: list[dict] = []
    for year in years:
        year_rows = 0
        for pos in positions:
            url = f"https://walterfootball.com/draft{year}{pos}.php"
            html = sc.fetch_static(url, sub="walterfootball", min_len=110000)
            if not html:
                continue
            rows = parse_position_page(html, pos, year, url)
            if save:
                for r in rows:
                    models.upsert_prospect(r)
            all_rows.extend(rows)
            year_rows += len(rows)
        print(f"  {year}: {year_rows} prospects")
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Scrape WalterFootball prospect profiles (all years)")
    ap.add_argument("--years", type=int, nargs="+", default=ALL_YEARS)
    ap.add_argument("--positions", nargs="+", default=ALL_POSITIONS)
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    args = ap.parse_args()

    models.init_db()
    print(f"Scraping WalterFootball: years={args.years[0]}-{args.years[-1]} "
          f"positions={args.positions}")
    rows = scrape(args.years, args.positions, save=not args.dry_run)
    print(f"\n  Parsed {len(rows)} prospects total.")
    if not args.dry_run:
        print("  Pool summary:", models.pool_summary())


if __name__ == "__main__":
    main()
