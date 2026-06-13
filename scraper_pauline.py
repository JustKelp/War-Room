"""
WarRoom — Tony Pauline scouting-report scraper (proposal §5, source #3).

Pauline's deep-volume reports live at Pro Football Network (his last home; he
passed in 2024). PFN is WordPress, so we enumerate his work precisely via the
REST API filtered to his author id — no guessing, no other analysts mixed in:

    /wp-json/wp/v2/posts?author=125   (Tony Pauline, slug "tpauline")

His signature format is a per-school roundup page ("Utah 2023 NFL Draft
Scouting Reports") containing many prospects, each an <h3> "Name, POS" heading
followed by prose "Strengths / Weaknesses / Overall". We parse every player
section out of each page. Measurables aren't generally present (optional, §4);
the written report is the payload.

Run:
    python scraper_pauline.py                 # all Pauline scouting pages
    python scraper_pauline.py --limit 20      # first 20 pages
    python scraper_pauline.py --dry-run
"""

import argparse
import re

from bs4 import BeautifulSoup

import models
import scrape_common as sc

SOURCE = "pauline"
PAULINE_AUTHOR_ID = 125
API = ("https://www.profootballnetwork.com/wp-json/wp/v2/posts"
       f"?author={PAULINE_AUTHOR_ID}&per_page=100&_fields=link")
_POS_RE = (r"QB|RB|FB|HB|WR|TE|OT|OG|OL|C|G|T|DE|DT|NT|EDGE|DL|"
           r"LB|ILB|OLB|MLB|CB|S|FS|SS|DB|K|P|LS")
# Promo/boilerplate fragments that get interleaved into the prose.
_PROMO = re.compile(r"(MORE:|FREE Mock Draft|Free Tools from PFSN|"
                    r"Mock Draft Simulator|Playoff Predictor|NFL Draft HQ)", re.I)


# ── DISCOVERY ────────────────────────────────────────────────────────────────

def discover() -> list[str]:
    """All of Pauline's scouting-report page URLs, via the WP REST API."""
    import requests, urllib3
    urllib3.disable_warnings()
    urls: list[str] = []
    page = 1
    while True:
        try:
            r = requests.get(API + f"&page={page}", timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        except requests.exceptions.SSLError:
            r = requests.get(API + f"&page={page}", timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        urls += [p["link"] for p in batch]
        if page >= int(r.headers.get("X-WP-TotalPages", page)):
            break
        page += 1
    return [u for u in urls if "scouting-report" in u]


def _school_year(url: str) -> tuple[str | None, int | None]:
    """Pull '<school>-<year>-nfl-draft-scouting-reports' from the slug."""
    slug = url.rstrip("/").split("/")[-1]
    m = re.match(r"(.*?)-(20\d\d)-nfl-draft-scouting-report", slug)
    if m:
        return m.group(1).replace("-", " ").title(), int(m.group(2))
    m = re.search(r"(20\d\d)", slug)
    return None, (int(m.group(1)) if m else None)


# ── PARSING ──────────────────────────────────────────────────────────────────

def parse_page(html: str, url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    art = soup.find("article") or soup
    school, year = _school_year(url)

    rows: list[dict] = []
    for h in art.find_all(["h2", "h3"]):
        head = h.get_text(" ", strip=True)
        # Stop once we reach the trailing "Free Tools from PFSN" promo block.
        if h.name == "h2" and "Free Tools" in head:
            break
        m = re.match(r"(.+?),\s*(" + _POS_RE + r")\b", head)
        if not m:
            continue
        name = m.group(1).strip()
        pos = m.group(2).upper()
        slot = models.position_to_slot(pos)
        if not slot:
            continue

        # Report = text from this heading to the next heading, promos removed.
        parts = []
        for sib in h.next_siblings:
            if getattr(sib, "name", None) in ("h2", "h3"):
                break
            if not hasattr(sib, "get_text"):
                continue
            t = sib.get_text(" ", strip=True)
            if t and not _PROMO.search(t):
                parts.append(t)
        report = "\n".join(parts).strip()
        if len(report) < 40:
            continue

        rows.append({
            "source": SOURCE, "source_url": url, "name": name, "slot": slot,
            "position": pos, "school": school, "draft_year": year,
            "height": None, "weight": None, "hand": None, "forty": None,
            "projected_round": None, "grade": None,
            "report": report, "blind_report": sc.redact(report, name, school or ""),
            "pfr_id": None,
        })
    return rows


# ── RUN ──────────────────────────────────────────────────────────────────────

def scrape(limit: int | None = None, save: bool = True) -> list[dict]:
    urls = discover()
    if limit:
        urls = urls[:limit]
    print(f"  {len(urls)} Pauline scouting pages to parse")
    all_rows: list[dict] = []
    for i, url in enumerate(urls, 1):
        html = sc.fetch_static(url, sub="pauline", min_len=20000)
        if not html:
            continue
        rows = parse_page(html, url)
        if save:
            for r in rows:
                models.upsert_prospect(r)
        all_rows.extend(rows)
        if i % 25 == 0 or rows:
            print(f"    [{i}/{len(urls)}] +{len(rows):2} from {url.rstrip('/').split('/')[-1][:45]}")
    return all_rows


def main():
    ap = argparse.ArgumentParser(description="Scrape Tony Pauline (PFN) scouting reports")
    ap.add_argument("--limit", type=int, default=None, help="Only the first N pages")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    args = ap.parse_args()

    models.init_db()
    print("Scraping Tony Pauline scouting reports (PFN author id 125)")
    rows = scrape(limit=args.limit, save=not args.dry_run)
    print(f"\n  Parsed {len(rows)} prospect reports.")
    if not args.dry_run:
        print("  Pool summary:", models.pool_summary())


if __name__ == "__main__":
    main()
