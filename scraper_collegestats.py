"""
WarRoom — college production stats scraper (post-pivot draft-card data).

The redesigned draft card (see warroom-update-memo.md §2) shows college
statistical averages, which NONE of the three scouting sources carried. This
adapter backfills them from Sports-Reference College Football into the
`college_stats` table, keyed by a normalized name and joined onto a prospect at
card-build time.

Technique (house style): Sports-Reference serves its stat tables in static HTML,
but wraps the secondary tables (rushing/receiving, defense) inside HTML
comments. We fetch statically, strip the comment markers, then parse every table
by its `data-stat` cell attributes — robust to column-order changes. No browser
render is needed. As a bonus the per-season rows carry a conference column, so
this same scrape fills the card's conference field too.

Resolution: each prospect name is run through SR's search endpoint, which 302-
redirects to the player page on a unique hit; otherwise we scan the results for
a candidate whose school matches. Resolved URLs are cached so re-runs are cheap.

    python scraper_collegestats.py --sample 8        # validate on a few players
    python scraper_collegestats.py --slot QB --limit 50
    python scraper_collegestats.py                   # full work list (long)

NOTE: college football does not officially track receiving TARGETS, so SR has no
targets column for the vast majority of players; that card field is captured
when present and left NULL otherwise.
"""

import argparse
import json
import os
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

import models
import nfl_career
import scrape_common as sc

SOURCE = "sportsref_cfb"
BASE = "https://www.sports-reference.com"
SEARCH = BASE + "/cfb/search/search.fcgi?search={}"
CACHE_SUB = "collegestats"
_RESOLVED_PATH = os.path.join(sc.CACHE_ROOT, CACHE_SUB, "_resolved.json")


# ── redirect-aware fetch + resolved-URL cache ────────────────────────────────

def _load_resolved() -> dict:
    if os.path.exists(_RESOLVED_PATH):
        with open(_RESOLVED_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_resolved(d: dict) -> None:
    os.makedirs(os.path.dirname(_RESOLVED_PATH), exist_ok=True)
    with open(_RESOLVED_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=0)


# Sports-Reference hard-blocks plain HTTP (Cloudflare 403), so every fetch goes
# through the browser. Search 302-redirects to the unique player page, so we
# render the search URL and read where the browser landed.

_SLUG_RE = re.compile(r"/cfb/players/([a-z0-9\-]+)\.html")


def _clean_url(url: str | None) -> str | None:
    """Drop tracking query strings / fragments (SR's JS decorates links with
    ?__hstc=... HubSpot params, which broke the .html check and the cache key)."""
    if not url:
        return url
    return url.split("?", 1)[0].split("#", 1)[0]


def _is_player(url: str) -> bool:
    url = _clean_url(url)
    return bool(url) and "/cfb/players/" in url and url.endswith(".html")


def _url_slug(url: str | None) -> str | None:
    m = _SLUG_RE.search(_clean_url(url) or "")
    return m.group(1) if m else None


def _slug_name_ok(slug: str | None, name: str) -> bool:
    """True if the page's slug shares a real name token with the player we asked
    for. Catches stuck-page mis-resolution on the name-search path, where the
    'wanted' URL is itself derived from the stale page (so the canonical check
    can't help). Conservative: only rejects when both sides have tokens to
    compare and they share none."""
    if not slug:
        return True
    stoks = {t for t in slug.split("-") if len(t) >= 4 and not t.isdigit()}
    ntoks = {t for t in models.norm_name(name).split() if len(t) >= 4}
    if not stoks or not ntoks:
        return True
    return bool(stoks & ntoks)


def _page_slug(html: str) -> str | None:
    """The player slug the rendered page actually belongs to, from its canonical
    link. Used to detect a stale/stuck browser page that returned the wrong
    player (the undetected-chromedriver failure that mass-assigned one page)."""
    m = re.search(r'rel="canonical"[^>]*href="[^"]*?/cfb/players/([a-z0-9\-]+)\.html', html)
    if not m:
        m = re.search(r'href="[^"]*?/cfb/players/([a-z0-9\-]+)\.html"[^>]*rel="canonical"', html)
    return m.group(1) if m else None


# ── player-page parsing ──────────────────────────────────────────────────────

def _souped(html: str) -> BeautifulSoup:
    # Expose tables that SR hides inside HTML comments.
    return BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")


def _career_cells(table) -> dict | None:
    """{data_stat: text} for the table's 'Career' totals row (in tfoot)."""
    foot = table.find("tfoot")
    if not foot:
        return None
    for tr in foot.find_all("tr"):
        lead = tr.find(["th", "td"])
        if lead and lead.get_text(strip=True).lower().startswith("career"):
            return {c.get("data-stat"): c.get_text(strip=True)
                    for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
    return None


def _num(cells: dict, *keys) -> float | None:
    for k in keys:
        v = cells.get(k)
        if v:
            try:
                return float(v.replace(",", ""))
            except ValueError:
                pass
    return None


def _measurements(soup) -> tuple[str | None, int | None]:
    """Height ('6-2') and weight (lb) from the player page meta header, e.g.
    '6-2, 219lb (188cm, 99kg)'. Card measurements (memo §2)."""
    meta = soup.find(id="meta")
    text = meta.get_text(" ", strip=True) if meta else soup.get_text(" ", strip=True)
    m = re.search(r"\b(\d-\d{1,2})\b\s*,?\s*(\d{2,3})\s*lb", text)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


MIN_QUALIFYING_GAMES = 8    # the card's "final season" is the latest one in which
                            # the player appeared in >= this many games (skips a
                            # partial/injury senior year that would misrepresent him)


def _to_f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _season_rows(table) -> dict:
    """{year:int -> {data-stat: text}} for the table's per-season rows. The SR
    year cell can carry an asterisk (e.g. '2013*' = All-American) which is
    stripped. Duplicate years (mid-career transfers) keep the last row."""
    rows = {}
    body = table.find("tbody")
    if not body:
        return rows
    for tr in body.find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {c.get("data-stat"): c.get_text(strip=True)
                 for c in tr.find_all(["th", "td"]) if c.get("data-stat")}
        yr = re.sub(r"[^0-9]", "", cells.get("year_id") or "")
        if yr:
            rows[int(yr)] = cells
    return rows


def _cn(cells: dict | None, *keys):
    if not cells:
        return None
    for k in keys:
        v = cells.get(k)
        if v not in (None, ""):
            fv = _to_f(v)
            if fv is not None:
                return fv
    return None


def parse_player(html: str, url: str) -> dict | None:
    """The player's FINAL college season — the stat line the draft card shows.
    'Final' = the most recent season in which he appeared in >=
    MIN_QUALIFYING_GAMES games (drops a partial/injury senior year that would
    misrepresent him); falls back to his most recent season if none qualify.
    One coherent season is taken across all stat tables (same year_id), so a QB's
    passing + rushing come from the same year. None if no usable table is found.
    (College Football reference does not record games STARTED, so the games-
    played bar is the only availability filter.)"""
    soup = _souped(html)
    height, weight = _measurements(soup)
    all_tables = [t for t in soup.find_all("table") if t.get("id")]

    def _find(base):
        # SR suffixes stat-table ids (e.g. 'passing_standard', 'defense_standard').
        for t in all_tables:
            tid = t.get("id")
            if tid == base or tid.startswith(base + "_"):
                return t
        return None

    bases = {b: _find(b) for b in ("passing", "rushing", "receiving", "defense", "scoring")}
    rows_by_base = {b: _season_rows(t) for b, t in bases.items() if t is not None}
    if not rows_by_base:
        return None

    # Games per year = max across the player's tables; pick the latest year that
    # clears the games bar (else the latest year on record).
    years = set().union(*(set(r) for r in rows_by_base.values()))
    games_by_year = {}
    for rows in rows_by_base.values():
        for yr, c in rows.items():
            g = _to_f(c.get("games") or c.get("g"))
            if g is not None:
                games_by_year[yr] = max(games_by_year.get(yr, 0.0), g)
    qual = [y for y in years if games_by_year.get(y, 0.0) >= MIN_QUALIFYING_GAMES]
    last_year = max(qual) if qual else (max(years) if years else None)
    if last_year is None:
        return None

    def cells(base):
        return rows_by_base.get(base, {}).get(last_year)

    p, ru = cells("passing"), cells("rushing")
    re_ = cells("receiving") or ru     # SR often merges rec_* into the rushing table
    d = cells("defense")
    conference = next((c.get("conf_abbr") for c in (cells(b) for b in
                       ("defense", "receiving", "rushing", "passing"))
                       if c and c.get("conf_abbr")), None)

    tackles = _cn(d, "tackles_total", "tackles_combined", "tackles")
    if tackles is None and d:
        solo, ast = _cn(d, "tackles_solo"), _cn(d, "tackles_assists")
        if solo is not None or ast is not None:
            tackles = (solo or 0) + (ast or 0)

    return {
        "source": SOURCE, "source_url": url,
        "height": height, "weight": weight,
        "games": int(games_by_year.get(last_year, 0)) or None,
        "seasons": len(qual) or None, "last_year": last_year, "conference": conference,
        "pass_cmp": _cn(p, "pass_cmp"), "pass_att": _cn(p, "pass_att"),
        "pass_yds": _cn(p, "pass_yds"), "pass_td": _cn(p, "pass_td"),
        "pass_int": _cn(p, "pass_int"),
        "rush_att": _cn(ru, "rush_att"), "rush_yds": _cn(ru, "rush_yds"),
        "rush_td": _cn(ru, "rush_td"),
        "rec": _cn(re_, "rec"), "rec_yds": _cn(re_, "rec_yds"),
        "rec_td": _cn(re_, "rec_td"), "targets": _cn(re_, "targets", "rec_targets"),
        "tackles": tackles, "sacks": _cn(d, "sacks"), "def_int": _cn(d, "def_int"),
        "tfl": _cn(d, "tackles_loss", "tfl"),
    }


# ── resolution ───────────────────────────────────────────────────────────────

def _school_matches(html: str, school: str) -> bool:
    if not school:
        return True
    key = re.sub(r"[^a-z]", "", school.lower())
    page = re.sub(r"[^a-z]", "", html.lower())
    # match on the distinctive head of the school name (handles St./State etc.)
    return key[:6] in page if len(key) >= 6 else key in page


def resolve(name: str, school: str, resolved: dict) -> tuple[str | None, str | None]:
    """Return (player_url, html) for a prospect via SR's search endpoint, all
    through the browser. Caches the resolved URL and verifies the school so we
    never store the wrong player. A cached empty string marks a known miss."""
    if name in resolved:
        url = resolved[name]
        if not url:
            return None, None
        html = sc.render(url, sub=CACHE_SUB, min_len=3000)
        return (url, html) if html else (None, None)

    final_url, html = sc.render_get(SEARCH.format(quote_plus(name)))
    cand = []
    if _is_player(final_url):
        cand = [(final_url, html)]          # search hit a unique player
    elif html:                              # disambiguation page — scan results
        links = sorted(set(re.findall(r"/cfb/players/[a-z0-9\-]+\.html", html)))
        cand = [(BASE + u, None) for u in links[:4]]

    for url, page in cand:
        if page is None:
            page = sc.render(url, sub=CACHE_SUB, min_len=3000)
        if page and _school_matches(page, school):
            sc.cache_html(url, page, CACHE_SUB)   # persist (render_get pages aren't cached)
            resolved[name] = url
            _save_resolved(resolved)
            return url, page

    resolved[name] = None      # remember the miss so we don't re-search
    _save_resolved(resolved)
    return None, None


# ── run ──────────────────────────────────────────────────────────────────────

def scrape(work: list[dict], save: bool = True) -> list[dict]:
    resolved = _load_resolved()
    out = []
    for w in work:
        # Drafted players carry a verified CFB link from the PFR draft table —
        # fetch it directly and skip the name search entirely.
        direct = _clean_url(w.get("cfb_url"))
        if direct and _is_player(direct):
            html = sc.render(direct, sub=CACHE_SUB, min_len=3000)
            url = direct if html else None
        else:
            url, html = resolve(w["name"], w.get("school") or "", resolved)
        if not html:
            print(f"    ?  {w['name']:26} {w['slot']:4} {w['draft_year']}  (unresolved)")
            continue

        # Integrity guard: the page must actually belong to the player we asked
        # for. A stale/stuck browser render (Cloudflare rate-limit) returns the
        # previously loaded page; without this check that page's stats get
        # mis-assigned to many players (the Adrian-Peterson mass-corruption).
        want, got = _url_slug(url), _page_slug(html)
        if want and got and want != got:
            print(f"    !  {w['name']:26} {w['slot']:4} {w['draft_year']}  "
                  f"(page mismatch: wanted {want}, got {got}) — skipped")
            continue
        if not _slug_name_ok(got, w["name"]):
            print(f"    !  {w['name']:26} {w['slot']:4} {w['draft_year']}  "
                  f"(name mismatch: page {got}) — skipped")
            resolved.pop(w["name"], None)      # don't keep the bad resolution
            _save_resolved(resolved)
            continue

        stats = parse_player(html, url)
        if not stats:
            print(f"    -  {w['name']:26} {w['slot']:4} {w['draft_year']}  (no stat table)")
            continue
        row = {
            "name_key": w["name_key"], "name": w["name"],
            "school": w.get("school"), "slot": w["slot"],
            "draft_year": w["draft_year"], **stats,
        }
        if save:
            models.upsert_college_stats(row)
        out.append(row)
        print(f"    +  {w['name']:26} {w['slot']:4} {w['draft_year']}  "
              f"G={row['games']} conf={row['conference']} "
              f"{_one_line(row)}")
    return out


def reparse(save: bool = True, render_missing: bool = False) -> tuple[int, int]:
    """Re-parse every pool player's CFB page and update college_stats — used to
    re-apply parsing changes such as the per-season games filter. Reads from the
    page cache; with render_missing=True it renders (and caches) any page that
    isn't cached yet (e.g. name-search pages that were never persisted). Returns
    (players updated, pages rendered)."""
    resolved = _load_resolved()
    work = models.distinct_prospects_to_resolve(include_resolved=True)
    n = rendered = 0
    for w in work:
        url = _clean_url(w.get("cfb_url")) or resolved.get(w["name"])
        if not url:
            continue
        html = sc._read_cache(sc._cache_path(url, CACHE_SUB), 3000)
        if not html and render_missing:
            html = sc.render(url, sub=CACHE_SUB, min_len=3000)
            rendered += 1
        if not html:
            continue
        stats = parse_player(html, url)
        if not stats:
            continue
        if save:
            models.upsert_college_stats({"name_key": w["name_key"], "name": w["name"],
                                         "school": w.get("school"), "slot": w["slot"],
                                         "draft_year": w["draft_year"], **stats})
        n += 1
    return n, rendered


def _one_line(r: dict) -> str:
    """Compact per-slot stat echo for run output / validation."""
    g = r["games"] or 1
    if r["slot"] == "QB":
        cp = (100 * r["pass_cmp"] / r["pass_att"]) if r.get("pass_att") else 0
        return f"cmp%={cp:.0f} passYds={r['pass_yds']} TD={r['pass_td']} INT={r['pass_int']} rushYds={r['rush_yds']}"
    if r["slot"] == "RB":
        ypc = (r["rush_yds"] / r["rush_att"]) if r.get("rush_att") else 0
        return f"rushYds={r['rush_yds']} TD={r['rush_td']} YPC={ypc:.1f} recYds={r['rec_yds']}"
    if r["slot"] == "WR":
        return f"rec={r['rec']} recYds={r['rec_yds']} TD={r['rec_td']} targets={r['targets']}"
    return f"tackles={r['tackles']} sacks={r['sacks']} INT={r['def_int']} TFL={r['tfl']}"


def main():
    ap = argparse.ArgumentParser(description="Scrape college production stats (Sports-Reference CFB)")
    ap.add_argument("--slot", choices=models.ROSTER_SLOTS, help="Only this roster slot")
    ap.add_argument("--limit", type=int, help="Cap number of players")
    ap.add_argument("--sample", type=int, help="Resolve N players spread across slots (validation)")
    ap.add_argument("--min-gp", type=float, default=6.0,
                    help="Board eligibility: min NFL games played per active season (default 6)")
    ap.add_argument("--min-gs", type=float, default=2.0,
                    help="Board eligibility: min NFL games started per active season (default 2)")
    ap.add_argument("--no-filter", action="store_true",
                    help="Disable the NFL-footprint eligibility filter (resolve everyone)")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    ap.add_argument("--reparse", action="store_true",
                    help="Re-parse cached CFB pages for every pool player (no rendering) "
                         "to re-apply parsing changes, e.g. the per-season games filter")
    ap.add_argument("--render-missing", action="store_true",
                    help="With --reparse, render (and cache) any CFB page that isn't cached "
                         "yet — the name-search pages that were never persisted")
    args = ap.parse_args()

    models.init_db()

    if args.reparse:
        print(f"Re-parsing CFB pages (>= {MIN_QUALIFYING_GAMES} games/season filter)"
              f"{' + rendering missing' if args.render_missing else ''}…")
        try:
            n, rendered = reparse(save=not args.dry_run, render_missing=args.render_missing)
        finally:
            sc.close_driver()
        print(f"  re-parsed {n} players ({rendered} rendered). "
              f"college_stats summary:", models.college_stats_summary())
        return

    work = models.distinct_prospects_to_resolve()

    # Board-eligibility filter: drop players without a real NFL footprint — they
    # average too few games/starts for anyone to know them at the reveal (rule:
    # >= min_gp games and >= min_gs starts per active season). See nfl_career.py.
    if not args.no_filter:
        career = nfl_career.load(verbose=True)
        before = len(work)
        work = [w for w in work
                if nfl_career.passes(career, w.get("pfr_id"), args.min_gp, args.min_gs)]
        print(f"  eligibility filter ({args.min_gp}GP/{args.min_gs}GS per season): "
              f"{before} -> {len(work)} players")

    if args.slot:
        work = [w for w in work if w["slot"] == args.slot]
    if args.sample:
        per = {}
        picked = []
        for w in work:
            if per.get(w["slot"], 0) < max(1, args.sample // 4):
                per[w["slot"]] = per.get(w["slot"], 0) + 1
                picked.append(w)
        work = picked[:args.sample]
    if args.limit:
        work = work[:args.limit]

    print(f"Resolving {len(work)} prospects against Sports-Reference CFB")
    try:
        rows = scrape(work, save=not args.dry_run)
    finally:
        sc.close_driver()
    print(f"\n  Stored {len(rows)} college-stat rows.")
    if not args.dry_run:
        print("  college_stats summary:", models.college_stats_summary())


if __name__ == "__main__":
    main()
