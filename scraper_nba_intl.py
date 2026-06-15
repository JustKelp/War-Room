"""WarRoom NBA — international pre-NBA stat lines (step 2b).

The 100 drafted internationals have no U.S. college stats. RealGM (Cloudflare)
and proballers (robots.txt disallows Anthropic/AI crawlers) are off-limits, so we
use Wikipedia: openly licensed (CC-BY-SA), a proper API, and its "Career
statistics" tables carry each player's pre-NBA club seasons (EuroLeague, Liga ACB,
ABA League, LNB, VTB, NBL, ...).

For each international we take the FINAL pre-NBA club season (latest season range,
tie-broken by games played) as the blind-card line and the league as the
"conference". Results merge into data/nba_cards.json (college field) and are also
written to data/nba_intl_stats.json.  python scraper_nba_intl.py [--limit N]
"""
import argparse, json, os, re, time
import requests, urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()
API = "https://en.wikipedia.org/w/api.php"
H = {"User-Agent": "WarRoom/1.0 (personal hobby project; justkelpee@gmail.com)"}
OUT = os.path.join("data", "nba_cards.json")
INTL = os.path.join("data", "nba_intl_stats.json")
DELAY = 0.3

NBA_TEAMS = {
    "Atlanta", "Boston", "Brooklyn", "New Jersey", "Charlotte", "Chicago",
    "Cleveland", "Dallas", "Denver", "Detroit", "Golden State", "Houston",
    "Indiana", "L.A. Clippers", "L.A. Lakers", "Los Angeles", "Memphis", "Miami",
    "Milwaukee", "Minnesota", "New Orleans", "New York", "Oklahoma City",
    "Orlando", "Philadelphia", "Phoenix", "Portland", "Sacramento", "San Antonio",
    "Toronto", "Utah", "Washington", "Seattle", "Vancouver",
}


def page_html(name):
    """Parsed article HTML for a basketball player, or None."""
    for title in (name, name + " (basketball)"):
        try:
            j = requests.get(API, params={"action": "parse", "page": title,
                             "prop": "text", "format": "json", "redirects": 1},
                             headers=H, timeout=30, verify=False).json()
        except Exception:
            return None
        time.sleep(DELAY)
        if "parse" not in j:
            continue
        html = j["parse"]["text"]["*"]
        if "basketball" in html[:8000].lower():
            return html
    return None


def _num(s):
    try:
        return float(re.sub(r"[*†‡\s]", "", s))
    except (TypeError, ValueError):
        return None


def final_pre_nba(html, draft_year):
    """The player's pre-DRAFT scouting season: the latest pre-NBA club season at or
    before draft_year-1 (vets who later returned to Europe must not pull a
    late-career line). If none precede the draft, fall back to their earliest club
    season. Returns a card dict, or None."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for t in soup.find_all("table", class_="wikitable"):
        head = t.find("tr")
        hdr = [th.get_text(" ", strip=True) for th in head.find_all("th")]
        if "PPG" not in hdr:
            continue
        idx = {h: i for i, h in enumerate(hdr)}
        h = t.find_previous(["h2", "h3", "h4"])
        league = re.sub(r"\[edit\]", "", h.get_text(" ", strip=True)).strip() if h else ""
        for tr in t.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if len(cells) < len(hdr):
                continue
            m = re.match(r"(\d{4})[–\-](\d{2})", cells[0])   # club season range only
            if not m or cells[idx.get("Team", 1)] in NBA_TEAMS:
                continue
            sy, gp = int(m.group(1)), (_num(cells[idx["GP"]]) or 0)
            rows.append((sy, gp, {
                "conference": league or "Overseas",
                "last_year": f"{m.group(1)}-{m.group(2)}",
                "ppg": _num(cells[idx["PPG"]]), "rpg": _num(cells[idx["RPG"]]),
                "apg": _num(cells[idx["APG"]]), "fg_pct": _num(cells[idx["FG%"]]),
                "fg3_pct": _num(cells[idx["3P%"]]), "ft_pct": _num(cells[idx.get("FT%", -1)]),
                "mpg": _num(cells[idx.get("MPG", -1)]), "gp": gp, "source": "wikipedia"}))
    if not rows:
        return None
    pre = [r for r in rows if r[0] <= (draft_year or 9999) - 1]
    if pre:                                    # latest pre-draft season, most games
        return max(pre, key=lambda r: (r[0], r[1]))[2]
    return min(rows, key=lambda r: r[0])[2]    # else earliest pro season


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    cards = json.load(open(OUT, encoding="utf-8"))
    intl = [(pid, c["name"]) for pid, c in cards.items() if c["conference"] == "International"]
    if args.limit:
        intl = intl[:args.limit]
    done = json.load(open(INTL, encoding="utf-8")) if os.path.exists(INTL) else {}

    hit = miss = 0
    for i, (pid, name) in enumerate(intl, 1):
        if pid not in done:
            html = page_html(name)
            done[pid] = final_pre_nba(html, cards[pid].get("draft_year")) if html else None
        rec = done[pid]
        if rec:
            hit += 1
            cards[pid]["college"] = rec
            cards[pid]["conference"] = rec["conference"]
        else:
            miss += 1
        if i % 20 == 0:
            json.dump(done, open(INTL, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            print(f"  {i}/{len(intl)}  hit={hit} miss={miss}")

    json.dump(done, open(INTL, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    json.dump(cards, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(f"Done. {hit} pre-NBA lines, {miss} unresolved (stay 'International').")


if __name__ == "__main__":
    main()
