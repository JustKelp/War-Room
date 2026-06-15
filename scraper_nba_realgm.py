"""WarRoom NBA — international pre-NBA stat lines from RealGM (step 2b, primary).

RealGM is the gold standard for international club stats and is permissive
(robots.txt: only crawl-delay 2, no Disallow, no AI opt-out). Its pages sit behind
a Cloudflare browser-check, cleared with curl_cffi's TLS impersonation. The
all-player-*.xml sitemaps give a complete name->URL index (no Cloudflared search).

For each drafted international we read the "International Season Stats" per-game
table and take the pre-DRAFT season (latest at/before draft_year-1, else earliest)
as the blind-card line, with the league as the "conference". Upgrades the
Wikipedia results in data/nba_cards.json; RealGM misses keep their Wikipedia line.

    python scraper_nba_realgm.py [--limit N]
"""
import argparse, json, os, re, time, unicodedata, warnings
from curl_cffi import requests as creq
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore")
PAGES = os.path.join("data", "realgm_pages")
SITEMAPS = os.path.join("data", "realgm_sitemaps")
OUT = os.path.join("data", "nba_cards.json")
RGM = os.path.join("data", "nba_realgm_stats.json")
os.makedirs(PAGES, exist_ok=True)
os.makedirs(SITEMAPS, exist_ok=True)
DELAY = 2
BASE = "https://basketball.realgm.com"


def slug(n):
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]+", "-", n.lower()).strip("-")


def get(url):
    for a in range(3):
        try:
            r = creq.get(url, impersonate="chrome120", timeout=60, verify=False)
            if r.status_code == 200 and "Just a moment" not in r.text[:200]:
                return r.text
        except Exception:
            pass
        time.sleep(DELAY * (a + 1))
    return None


def url_map():
    """name-slug -> [Summary URLs] from the 9 player sitemaps (cached)."""
    m = {}
    for i in range(1, 10):
        p = os.path.join(SITEMAPS, f"all-player-{i}.xml")
        txt = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else None
        if txt is None:
            txt = get(f"{BASE}/sitemap/all-player-{i}.xml") or ""
            open(p, "w", encoding="utf-8").write(txt)
            time.sleep(DELAY)
        for u in re.findall(r"<loc>([^<]+/Summary/\d+)</loc>", txt):
            m.setdefault(slug(u.split("/player/")[1].split("/Summary")[0]), []).append(u)
    return m


def page(pid, url):
    p = os.path.join(PAGES, f"{pid}.html")
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="replace").read()
    html = get(url)
    if html:
        open(p, "w", encoding="utf-8").write(html)
        time.sleep(DELAY)
    return html


def parse(html, draft_year):
    """Pre-draft international club season from the per-game table, or None.
    Also returns whether the page is an NBA player (for namesake disambiguation)."""
    soup = BeautifulSoup(html, "lxml")
    is_nba = any(h.get_text(strip=True) == "NBA Regular Season Stats"
                 for h in soup.find_all(["h2", "h3"]))
    table = None
    for t in soup.find_all("table"):
        cap = t.find_previous(["h2", "h3"])
        if cap and cap.get_text(" ", strip=True).strip() == "International Season Stats":
            hdr = [th.get_text(strip=True) for th in t.find("thead").find_all("th")]
            if "PTS" in hdr and "FGA" not in hdr:        # the per-game table
                table = (t, {h: i for i, h in enumerate(hdr)})
                break
    if not table:
        return is_nba, None
    t, idx = table

    def num(cells, k):
        try:
            return float(cells[idx[k]].replace("*", "").strip())
        except (KeyError, ValueError, IndexError):
            return None
    rows = []
    for tr in t.find("tbody").find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        m = re.match(r"(\d{4})-(\d{2})", cells[idx["Season"]].replace("*", "").strip())
        if not m:
            continue
        rows.append((int(m.group(1)), {
            "conference": cells[idx["League"]].split(",")[0].strip() or "Overseas",
            "last_year": f"{m.group(1)}-{m.group(2)}",
            "ppg": num(cells, "PTS"), "rpg": num(cells, "REB"), "apg": num(cells, "AST"),
            "fg_pct": num(cells, "FG%"), "fg3_pct": num(cells, "3P%"), "ft_pct": num(cells, "FT%"),
            "mpg": num(cells, "MIN"), "gp": num(cells, "GP"), "source": "realgm"}))
    if not rows:
        return is_nba, None
    pre = [r for r in rows if r[0] <= (draft_year or 9999) - 1]
    rec = (max(pre, key=lambda r: r[0]) if pre else min(rows, key=lambda r: r[0]))[1]
    return is_nba, rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    cards = json.load(open(OUT, encoding="utf-8"))
    intl = [(pid, c["name"], c.get("draft_year")) for pid, c in cards.items()
            if c["conference"] == "International" or (c.get("college") or {}).get("source") == "wikipedia"]
    if args.limit:
        intl = intl[:args.limit]
    m = url_map()
    done = json.load(open(RGM, encoding="utf-8")) if os.path.exists(RGM) else {}

    hit = 0
    for i, (pid, name, dy) in enumerate(intl, 1):
        if pid not in done:
            rec = None
            for url in m.get(slug(name), [])[:4]:          # namesake disambiguation
                html = page(pid, url) if len(m.get(slug(name), [])) == 1 else get(url)
                if not html:
                    continue
                is_nba, r = parse(html, dy)
                if r and (is_nba or len(m.get(slug(name), [])) == 1):
                    rec = r
                    break
            done[pid] = rec
        if done[pid]:
            hit += 1
            cards[pid]["college"] = done[pid]
            cards[pid]["conference"] = done[pid]["conference"]
        if i % 20 == 0:
            json.dump(done, open(RGM, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            json.dump(cards, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            print(f"  {i}/{len(intl)}  realgm hits={hit}")

    json.dump(done, open(RGM, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    json.dump(cards, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    statless = sum(1 for c in cards.values() if not c.get("college"))
    print(f"Done. RealGM resolved {hit}/{len(intl)}. Cards stat-less now: {statless}.")


if __name__ == "__main__":
    main()
