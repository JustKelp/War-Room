"""WarRoom NBA — blind-card data scraper (step 2).

For every pool player (drafted 2010-25 with a real CES), pull:
  • measurables (height/weight) from the bbref player page, and
  • final college season per-game line + conference from Sports-Reference CBB.
International players (no U.S. college) keep measurables, get conference
"International" and no college stat line (per design).

Plain requests work on both SR sites; ~3s delay respects the rate limit. Pages +
the output cache under data/ (gitignored). Resumable — re-running only fetches
what's missing.  python scraper_nba_cards.py [--limit N]
"""
import argparse, json, os, re, time
import requests, urllib3
from bs4 import BeautifulSoup

import es_scoring_nba as N

urllib3.disable_warnings()
PAGES = os.path.join("data", "nba_card_pages")
DRAFT = os.path.join("data", "nba_draft.json")
OUT = os.path.join("data", "nba_cards.json")
os.makedirs(PAGES, exist_ok=True)
DELAY = 3
SR = "https://www.sports-reference.com"


def _surname(name):
    toks = re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
    toks = [t for t in toks if t not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return toks[-1] if toks else ""


def _cached(name):
    p = os.path.join(PAGES, name)
    return open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None


def _save(name, html):
    open(os.path.join(PAGES, name), "w", encoding="utf-8").write(html)


def bbref_page(pid):
    """(height, weight, cbb_url) from the bbref player page. The page links
    DIRECTLY to the player's own SR-CBB page — collision-proof, no name search."""
    html = _cached(f"bbref_{pid}.html")
    if html is None:
        r = requests.get(f"https://www.basketball-reference.com/players/{pid[0]}/{pid}.html",
                         timeout=30, verify=False)
        r.encoding = "utf-8"
        time.sleep(DELAY)
        if r.status_code != 200:
            return None, None, None
        html = r.text
        _save(f"bbref_{pid}.html", html)
    meta = BeautifulSoup(html, "lxml").find(id="meta")
    mt = meta.get_text(" ", strip=True) if meta else ""
    m = re.search(r"(\d-\d{1,2})\s*,?\s*(\d{2,3})\s*lb", mt)
    ht, wt = (m.group(1), int(m.group(2))) if m else (None, None)
    link = re.search(r"/cbb/players/[a-z0-9\-]+\.html", html)
    return ht, wt, (link.group(0) if link else None)


def college_card(pid, cbb_path):
    """Final college season per-game line + conference from the player's own
    SR-CBB page (linked from bbref), or None."""
    html = _cached(f"cbb_{pid}.html")
    if html is None:
        r = requests.get(SR + cbb_path, timeout=30, verify=False)
        r.encoding = "utf-8"
        time.sleep(DELAY)
        if r.status_code != 200:
            return None
        html = r.text
        _save(f"cbb_{pid}.html", html)
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    pg = soup.find("table", id="players_per_game") or soup.find("table", id="per_game")
    if not pg or not pg.find("tbody"):
        return None
    rows = [tr for tr in pg.find("tbody").find_all("tr") if "thead" not in (tr.get("class") or [])]
    if not rows:
        return None
    c = {td.get("data-stat"): td.get_text(strip=True) for td in rows[-1].find_all(["th", "td"])}

    def f(k):
        try:
            return float(c.get(k))
        except (TypeError, ValueError):
            return None
    return {"conference": c.get("conf_abbr"), "last_year": c.get("year_id"),
            "ppg": f("pts_per_g"), "rpg": f("trb_per_g"), "apg": f("ast_per_g"),
            "fg_pct": f("fg_pct"), "fg3_pct": f("fg3_pct"), "ft_pct": f("ft_pct"),
            "spg": f("stl_per_g"), "bpg": f("blk_per_g"), "mpg": f("mp_per_g")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    draft = {p["player_id"]: p for p in json.load(open(DRAFT, encoding="utf-8"))}
    ces = N.build()
    pool = [pid for pid in draft if ces.get(pid, {}).get("ces", 0) > 0]   # real-CES pool
    if args.limit:
        pool = pool[:args.limit]
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}

    for i, pid in enumerate(pool, 1):
        if pid in out:
            continue
        name = ces[pid]["name"]
        intl = draft[pid]["college"] == "International"
        ht, wt, cbb = bbref_page(pid)
        card = {"height": ht, "weight": wt, "pos": ces[pid]["pos"], "ces": ces[pid]["ces"],
                "name": name, "draft_year": draft[pid]["draft_year"], "pick": draft[pid]["pick"]}
        if intl:
            card["conference"] = "International"
        else:
            col = college_card(pid, cbb) if cbb else None
            card["conference"] = (col or {}).get("conference") or "—"
            card["college"] = col            # None if unresolved
        out[pid] = card
        if i % 25 == 0:
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            print(f"  {i}/{len(pool)} ...")

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    have_col = sum(1 for c in out.values() if c.get("college"))
    print(f"Done. {len(out)} cards; {have_col} with college stats; "
          f"{sum(1 for c in out.values() if c['conference']=='International')} international.")


if __name__ == "__main__":
    main()
