"""
WarRoom — NFL.com (Lance Zierlein) prospect scraper.

NFL.com's draft prospect pages are a JavaScript single-page app: the written
report, measurables, and numeric grade are NOT in the static HTML. We render
each page in a real browser (scrape_common.render → undetected-chromedriver)
and parse the resulting DOM.

The markup uses randomized CSS-module class names, so parsing is label-driven:
we locate stable heading text ("Prospect Info", "Prospect Grade", "Analysis" →
"Overview"/"Strengths"/"Weaknesses", "Combine Results") and read the values
around it.

Run:
    python scraper_nflcom.py --years 2024 2025      # render + ingest
    python scraper_nflcom.py --parse-cache          # re-parse cached pages only
"""

import argparse
import re

from bs4 import BeautifulSoup

import models
import scrape_common as sc

SOURCE = "nflcom"
PROSPECTS_INDEX = "https://www.nfl.com/draft/tracker/prospects"
_POS_RE = (r"QB|RB|FB|HB|WR|TE|OT|OG|OL|OC|C|G|T|DE|DT|NT|EDGE|DL|"
           r"LB|ILB|OLB|MLB|EDG|CB|S|FS|SS|DB|K|P|LS")


# ── PARSING ──────────────────────────────────────────────────────────────────

def _label_block(soup, label, tag):
    """Text of the container holding a heading whose text == label."""
    for h in soup.find_all(tag):
        if h.get_text(strip=True).lower() == label.lower():
            # climb until the container has meaningfully more than the label
            node = h
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                txt = node.get_text(" ", strip=True)
                if len(txt) > len(label) + 12:
                    return txt
    return None


def _info_text(soup):
    """Pipe-joined text of the 'Prospect Info' block (College/Height/Weight/...)."""
    for h in soup.find_all("h3"):
        if h.get_text(strip=True).lower() == "prospect info":
            node = h
            for _ in range(4):
                node = node.parent
                if node is None:
                    break
                txt = node.get_text(" | ", strip=True)
                if "College" in txt or "Height" in txt:
                    return txt
    return ""


def _after(label, text, pat):
    m = re.search(re.escape(label) + r"\s*\|?\s*" + pat, text)
    return m.group(1).strip() if m else None


def parse_prospect(html: str, url: str) -> dict | None:
    """Parse one rendered NFL.com prospect page into a normalized prospect dict.
    Returns None if it isn't a draftable roster position or has no report."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if not h1:
        return None
    name = h1.get_text(strip=True)

    header = h1.parent.get_text(" | ", strip=True) if h1.parent else ""
    pos_m = re.search(r"\|\s*(" + _POS_RE + r")\s*\|\s*Prospect Info", header)
    position = (pos_m.group(1) if pos_m else "").upper()
    slot = models.position_to_slot(position)
    if not slot:
        return None  # TE/OL/K/P/etc. — not a WarRoom roster slot

    info = _info_text(soup)
    school = _after("College", info, r"([A-Za-z&.'\- ]+?)\s*\|")
    height = _after("Height", info, r"([\d'\"\- ]+?)\s*\|")
    weight_s = _after("Weight", info, r"(\d+)")
    hand = _after("Hand", info, r"([\d /]+)")

    combine = _label_block(soup, "40-Yard Dash", "h4") or ""
    forty_m = re.search(r"40-Yard Dash\s*\|?\s*(\d\.\d{2})", combine)
    forty = float(forty_m.group(1)) if forty_m else None

    grade_block = _label_block(soup, "Prospect Grade", "h3") or ""
    grade_m = re.search(r"(\d\.\d{1,2})", grade_block)
    grade = float(grade_m.group(1)) if grade_m else None
    # descriptor after the number, e.g. "Will eventually be plus starter"
    desc_m = re.search(r"\d\.\d{1,2}\s+([A-Z][^|]+?)(?:\s+Info|\s+View|$)", grade_block)
    grade_desc = desc_m.group(1).strip() if desc_m else None

    # draft class year, e.g. heading "2026 Draft Results"
    yr_m = re.search(r"(20\d\d)\s+Draft", soup.get_text(" ", strip=True))
    draft_year = int(yr_m.group(1)) if yr_m else None

    sections = []
    for lbl in ("Overview", "Strengths", "Weaknesses"):
        block = _label_block(soup, lbl, "h4")
        if block:
            sections.append(block.strip())
    report = "\n\n".join(sections)
    if grade_desc:
        report = f"Grade: {grade} — {grade_desc}\n\n" + report
    if not report:
        return None

    try:
        weight = int(weight_s) if weight_s else None
    except ValueError:
        weight = None

    return {
        "source": SOURCE,
        "source_url": url,
        "name": name,
        "slot": slot,
        "position": position,
        "school": school,
        "draft_year": draft_year,
        "height": height,
        "weight": weight,
        "hand": hand,
        "forty": forty,
        "projected_round": None,
        "grade": grade,
        "report": report,
        "blind_report": sc.redact(report, name, school or ""),
        "pfr_id": None,
    }


# ── DISCOVERY ────────────────────────────────────────────────────────────────

def discover(year: int | None = None) -> list[str]:
    """Render the prospect tracker and return individual prospect page URLs.
    NFL.com exposes the active draft cycle; an optional ?year= filter is tried."""
    url = PROSPECTS_INDEX + (f"/all?year={year}" if year else "")
    # The tracker infinite-scrolls ~50 at a time; scroll to load the full class.
    html = sc.render(url, sub="nflcom_index", wait=8, scroll=30, force=True)
    if not html:
        return []
    found = re.findall(r"/prospects/[a-z0-9\-]+/[0-9a-f\-]{20,}", html)
    return sorted({"https://www.nfl.com" + u for u in found})


# ── RUN ──────────────────────────────────────────────────────────────────────

def scrape(save: bool = True) -> list[dict]:
    """Scrape NFL.com's active draft cycle. NFL.com only exposes the current
    class (no historical year index), so we discover it once; each page's own
    draft year is read during parsing."""
    rows: list[dict] = []
    urls = discover(None)
    print(f"  discovered {len(urls)} prospect pages (current cycle)")
    for url in urls:
        html = sc.render(url, sub="nflcom")
        if not html:
            continue
        p = parse_prospect(html, url)
        if not p:
            continue
        if save:
            models.upsert_prospect(p)
        rows.append(p)
        print(f"    + {p['name']:24} {p['position']:4} grade={p['grade']}")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Scrape NFL.com (Zierlein) prospect profiles")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    args = ap.parse_args()

    models.init_db()
    print("Scraping NFL.com (Zierlein) — current draft cycle")
    try:
        rows = scrape(save=not args.dry_run)
    finally:
        sc.close_driver()
    print(f"\n  Parsed {len(rows)} draftable prospects.")
    if not args.dry_run:
        print("  Pool summary:", models.pool_summary())


if __name__ == "__main__":
    main()
