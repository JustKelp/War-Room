"""One-time: pull the per-season Awards column from basketball-reference season
pages into data/nba_awards.json  (key "pid|season" -> award string e.g.
"MVP-1,AS,NBA1"). Plain requests work on bbref; ~3s delay respects the rate limit.
Pages + the json are cached under data/ (gitignored)."""
import json, os, re, time
import requests, urllib3
from bs4 import BeautifulSoup, Comment

urllib3.disable_warnings()
CACHE = os.path.join("data", "nba_award_pages")
OUT = os.path.join("data", "nba_awards.json")
os.makedirs(CACHE, exist_ok=True)


def fetch(year):
    path = os.path.join(CACHE, f"{year}.html")
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="replace").read()
    for lg in ("NBA", "BAA"):                       # 1947-49 are BAA on bbref
        url = f"https://www.basketball-reference.com/leagues/{lg}_{year}_per_game.html"
        r = requests.get(url, timeout=30, verify=False)
        if r.status_code == 200 and "per_game_stats" in r.text:
            open(path, "w", encoding="utf-8").write(r.text)
            time.sleep(3)
            return r.text
        time.sleep(3)
    return None


def parse(html):
    soup = BeautifulSoup(html, "lxml")
    t = soup.find("table", id="per_game_stats")
    if not t:
        for c in soup.find_all(string=lambda x: isinstance(x, Comment)):
            if "per_game_stats" in c:
                t = BeautifulSoup(c, "lxml").find("table", id="per_game_stats")
                break
    out = {}
    if not t or not t.find("tbody"):
        return out
    for tr in t.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        cells = {td.get("data-stat"): td for td in tr.find_all(["th", "td"])}
        awc = cells.get("awards")
        aw = awc.get_text(strip=True) if awc else ""
        a = cells.get("name_display")
        link = a.find("a") if a else None
        if aw and link and link.get("href"):
            pid = link["href"].split("/")[-1].replace(".html", "")
            out[pid] = aw
    return out


def main():
    awards = {}
    for year in range(1947, 2027):
        html = fetch(year)
        if not html:
            continue
        n = 0
        for pid, aw in parse(html).items():
            awards[f"{pid}|{year}"] = aw
            n += 1
        print(f"{year}: {n} award-seasons")
    json.dump(awards, open(OUT, "w"), indent=0)
    print(f"\nSaved {len(awards)} award-seasons -> {OUT}")


if __name__ == "__main__":
    main()
