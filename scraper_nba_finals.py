"""WarRoom NBA — Finals starters scraper.

Collects every player who STARTED any NBA Finals game in the last 10 seasons
(2017-2026). For each year: the playoffs index -> the '-nba-finals-' series page
-> each game box score -> the 5 starters (rows before the 'Reserves' separator)
for BOTH teams. Plain requests + ~3s delay; pages cached under data/ (gitignored).

  python scraper_nba_finals.py [--start 2017] [--end 2026]
Output: data/nba_finals_starters.json -> {pid: {"name":..., "years":[...]}}
"""
import argparse, json, os, re, time
import requests, urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()
BR = "https://www.basketball-reference.com"
PAGES = os.path.join("data", "nba_finals_pages")
OUT = os.path.join("data", "nba_finals_starters.json")
os.makedirs(PAGES, exist_ok=True)
DELAY = 3


def _get(url, cache_name):
    p = os.path.join(PAGES, cache_name)
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="replace").read()
    r = requests.get(url, timeout=30, verify=False,
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.encoding = "utf-8"
    time.sleep(DELAY)
    if r.status_code != 200:
        print(f"  !! {r.status_code} {url}")
        return None
    open(p, "w", encoding="utf-8").write(r.text)
    return r.text


def finals_series_url(year):
    html = _get(f"{BR}/playoffs/NBA_{year}.html", f"playoffs_{year}.html")
    if not html:
        return None
    for href in re.findall(r'href="(/playoffs/[^"]+\.html)"', html):
        if "-nba-finals-" in href:
            return BR + href
    return None


def boxscore_urls(series_url, year):
    html = _get(series_url, f"finals_series_{year}.html")
    if not html:
        return []
    return sorted({BR + h for h in re.findall(r'href="(/boxscores/\d{8}0[A-Z]{3}\.html)"', html)})


def starters_from_box(url, year, gi):
    """Return {pid: name} for the starters (both teams) of one Finals game."""
    html = _get(url, f"box_{year}_{gi}.html")
    if not html:
        return {}
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    out = {}
    for t in soup.find_all("table", id=re.compile(r"^box-[A-Z]{3}-game-basic$")):
        body = t.find("tbody")
        if not body:
            continue
        for tr in body.find_all("tr"):
            cls = tr.get("class") or []
            if "thead" in cls:        # the 'Reserves' separator -> starters done
                break
            a = tr.find("a", href=re.compile(r"/players/"))
            nm = tr.find(["td", "th"], {"data-stat": "player"})
            if a and nm:
                out[a["href"].split("/")[-1].replace(".html", "")] = nm.get_text(strip=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2017)
    ap.add_argument("--end", type=int, default=2026)
    args = ap.parse_args()

    result = {}
    for year in range(args.start, args.end + 1):
        su = finals_series_url(year)
        if not su:
            print(f"{year}: NO finals series found")
            continue
        boxes = boxscore_urls(su, year)
        starters = {}
        for gi, bu in enumerate(boxes, 1):
            starters.update(starters_from_box(bu, year, gi))
        print(f"{year}: {su.split('/')[-1]}  {len(boxes)} games  {len(starters)} starters")
        for pid, name in starters.items():
            r = result.setdefault(pid, {"name": name, "years": []})
            r["years"].append(year)

    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"\nDone. {len(result)} distinct Finals starters {args.start}-{args.end} -> {OUT}")


if __name__ == "__main__":
    main()
