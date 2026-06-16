"""WarRoom NBA — cards for the recognizable-pool additions (the 46 players in
data/_nba_set.json['need'] that weren't in the old 652 pool).

Routes each player to the right source:
  • college     -> SR-CBB final season (bbref -> /cbb link), real conference
  • HS (stats)  -> bbref high-school game logs aggregated to the final season,
                   conference shown as "High School"
  • international-> RealGM pre-draft club season, conference shown as "Overseas"
  • else        -> measurables-only (conference "High School" for prep-to-pro)

Output: data/nba_newcards.json  (merged into nba_cards.json by the rebuild step).
"""
import json, os, re, time
import requests, urllib3
from bs4 import BeautifulSoup

import es_scoring_nba as N
import scraper_nba_cards as C            # bbref_page(), college_card()
import scraper_nba_realgm as RGM         # url_map(), page(), parse(), slug()

urllib3.disable_warnings()
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUT = os.path.join("data", "nba_newcards.json")
DELAY = 3

INTL = {"dragigo01", "gasolma01", "pachuza01"}   # no college, played overseas pre-NBA


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def hs_card(pid, hs_path):
    """Aggregate the player's FINAL high-school season game log into a per-game
    line (PPG/RPG/APG/FG%/3P%). Conference labelled 'High School'."""
    url = f"https://www.basketball-reference.com{hs_path}"
    p = os.path.join(C.PAGES, f"hs_{pid}.html")
    if os.path.exists(p):
        html = open(p, encoding="utf-8", errors="replace").read()
    else:
        r = requests.get(url, headers=H, verify=False, timeout=30)
        r.encoding = "utf-8"
        time.sleep(DELAY)
        if r.status_code != 200:
            return None
        html = r.text
        open(p, "w", encoding="utf-8").write(html)
    soup = BeautifulSoup(html.replace("<!--", "").replace("-->", ""), "lxml")
    tables = soup.find_all("table", id=re.compile(r"^hs-\d\d-\d\d$"))
    if not tables:
        return None
    t = tables[-1]                       # final HS season
    season = t.get("id").replace("hs-", "")
    g = pts = trb = ast = fg = fga = fg3 = fg3a = ft = fta = 0
    for tr in t.find("tbody").find_all("tr"):
        d = {c.get("data-stat"): c.get_text(strip=True) for c in tr.find_all(["th", "td"])}
        if _f(d.get("pts")) is None:     # spacer / suspended-game rows
            continue
        g += 1
        pts += _f(d.get("pts")) or 0; trb += _f(d.get("trb")) or 0; ast += _f(d.get("ast")) or 0
        fg += _f(d.get("fg")) or 0; fga += _f(d.get("fga")) or 0
        fg3 += _f(d.get("fg3")) or 0; fg3a += _f(d.get("fg3a")) or 0
        ft += _f(d.get("ft")) or 0; fta += _f(d.get("fta")) or 0
    if not g:
        return None
    pct = lambda a, b: round(a / b, 3) if b else None
    return {"conference": "High School", "last_year": f"20{season[3:]}" if season[3:] < "30" else f"19{season[3:]}",
            "ppg": round(pts / g, 1), "rpg": round(trb / g, 1), "apg": round(ast / g, 1),
            "fg_pct": pct(fg, fga), "fg3_pct": pct(fg3, fg3a), "ft_pct": pct(ft, fta),
            "spg": None, "bpg": None, "mpg": None, "gp": g, "source": "hs"}


def main():
    need = json.load(open(os.path.join("data", "_nba_set.json"), encoding="utf-8"))["need"]
    ces = N.build()
    draft = {d["player_id"]: d for d in json.load(open(os.path.join("data", "nba_draft.json"), encoding="utf-8"))}
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    rgm_map = None

    for i, pid in enumerate(need, 1):
        if pid in out:
            continue
        info = ces.get(pid, {})
        name = info.get("name") or draft.get(pid, {}).get("name", pid)
        ht, wt, cbb = C.bbref_page(pid)
        card = {"height": ht, "weight": wt, "pos": info.get("pos"), "ces": info.get("ces"),
                "name": name, "draft_year": draft.get(pid, {}).get("draft_year", 0),
                "pick": draft.get(pid, {}).get("pick", 0)}
        col = None
        # detect HS link from the cached bbref page
        bb = open(os.path.join(C.PAGES, f"bbref_{pid}.html"), encoding="utf-8", errors="replace").read()
        hs_link = re.search(r'/players/[a-z]/[a-z0-9]+/[a-z\-]*high-school[a-z\-]*\.html', bb)
        if pid in INTL:
            if rgm_map is None:
                rgm_map = RGM.url_map()
            for url in rgm_map.get(RGM.slug(name), [])[:4]:
                html = RGM.page(pid, url)
                if html:
                    _, rec = RGM.parse(html, card["draft_year"] or None)
                    if rec:
                        col = rec; col["conference"] = "Overseas"; break
            card["conference"] = (col or {}).get("conference") or "Overseas"
        elif cbb:
            col = C.college_card(pid, cbb)
            card["conference"] = (col or {}).get("conference") or "—"
        elif hs_link:
            col = hs_card(pid, hs_link.group(0))
            card["conference"] = "High School"
        else:                            # prep / HS-to-pro with no stats page
            card["conference"] = "High School"
        card["college"] = col
        out[pid] = card
        print(f"{i:2}/{len(need)} {pid:11} {name[:24]:24} "
              f"{card['conference']:14} {'STATS' if col else 'measurables-only'}")
        if i % 15 == 0:
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    have = sum(1 for c in out.values() if c.get("college"))
    print(f"\nDone. {len(out)} new cards; {have} with a stat line, "
          f"{len(out)-have} measurables-only.")


if __name__ == "__main__":
    main()
