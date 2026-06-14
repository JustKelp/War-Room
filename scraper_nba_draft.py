"""WarRoom NBA — draft-pool scraper.

Pulls every NBA draft pick (both rounds) for a range of years from
basketball-reference, keyed by bbref player_id. The pool is later filtered to
players with a real Career Excel Score (es_scoring_nba) — that prunes busts and
keeps recognizable names, including 2nd-round stars (Jokić #41, Draymond #35,
Manu #57) and drafted internationals (Dončić, Giannis, Wembanyama).

Plain requests work on bbref; ~3s delay respects the rate limit. Pages + the
output JSON cache under data/ (gitignored). One page per draft year.

    python scraper_nba_draft.py            # scrape DRAFT_YEARS -> data/nba_draft.json
"""
import json, os, time
import requests, urllib3
from bs4 import BeautifulSoup, Comment

urllib3.disable_warnings()
DRAFT_YEARS = range(2010, 2026)            # ~16 recent classes
CACHE = os.path.join("data", "nba_draft_pages")
OUT = os.path.join("data", "nba_draft.json")
os.makedirs(CACHE, exist_ok=True)


def fetch(year):
    path = os.path.join(CACHE, f"{year}.html")
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="replace").read()
    url = f"https://www.basketball-reference.com/draft/NBA_{year}.html"
    r = requests.get(url, timeout=30, verify=False)
    r.encoding = "utf-8"
    if r.status_code != 200 or "id=\"stats\"" not in r.text:
        time.sleep(3)
        return None
    open(path, "w", encoding="utf-8").write(r.text)
    time.sleep(3)
    return r.text


def parse(html, year):
    soup = BeautifulSoup(html, "lxml")
    t = soup.find("table", id="stats")
    if not t:
        for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
            if 'id="stats"' in str(c):
                t = BeautifulSoup(str(c), "lxml").find("table", id="stats")
                break
    out = []
    if not t or not t.find("tbody"):
        return out
    for tr in t.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        c = {td.get("data-stat"): td for td in tr.find_all(["th", "td"])}
        pl, pk = c.get("player"), c.get("pick_overall")
        if not pl or not pl.find("a") or not pk:
            continue
        pid = pl.find("a")["href"].split("/")[-1].replace(".html", "")
        college = (c.get("college_name").get_text(strip=True) if c.get("college_name") else "")
        try:
            pick = int(pk.get_text(strip=True))
        except ValueError:
            continue
        out.append({"player_id": pid, "name": pl.get_text(strip=True),
                    "draft_year": year, "pick": pick,
                    "college": college or "International"})
    return out


def main():
    pool = []
    for year in DRAFT_YEARS:
        html = fetch(year)
        if not html:
            print(f"{year}: skip")
            continue
        picks = parse(html, year)
        pool.extend(picks)
        print(f"{year}: {len(picks)} picks")
    json.dump(pool, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    intl = sum(1 for p in pool if p["college"] == "International")
    print(f"\nSaved {len(pool)} draft picks -> {OUT}  ({intl} international)")


if __name__ == "__main__":
    main()
