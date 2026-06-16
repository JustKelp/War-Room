"""WarRoom NBA — legends + legendary draft busts (pool 253 -> 300).

Adds 37 all-time legends (mostly pre-2012, so missed by the All-NBA windows) and
10 canonical draft busts. Cards via the same routing as scrape_nba_newcards:
college -> SR-CBB, international -> RealGM ("Overseas"), HS-to-pro -> measurables
("High School"). Draft year + overall pick are parsed straight from the bbref
player-page meta (so busts show their infamous slot: Bowie '84 #2, Oden '07 #1).

Output: data/nba_legends_cards.json (merged into nba_cards.json by the rebuild).
"""
import json, os, re
import es_scoring_nba as N
import scraper_nba_cards as C            # bbref_page(), college_card(), PAGES
import scrape_nba_newcards as NC         # hs_card()
import scraper_nba_realgm as RGM

OUT = os.path.join("data", "nba_legends_cards.json")

LEGENDS = ["jordami01", "johnsma02", "birdla01", "abdulka01", "onealsh01", "olajuha01",
           "iversal01", "barklch01", "malonka01", "stockjo01", "robinda01", "ewingpa01",
           "pippesc01", "thomais01", "nowitdi01", "garneke01", "wadedw01", "nashst01",
           "kiddja01", "allenra02", "millere01", "paytoga01", "cartevi01", "mcgratr01",
           "piercpa01", "wilkido01", "drexlcl01", "malonmo01", "ervinju01", "chambwi01",
           "russebi01", "roberos01", "westje01", "parisro01", "mchalke01", "gasolpa01",
           "anthoca01"]
BUSTS = ["odengr01", "bowiesa01", "milicda01", "brownkw01", "olowomi01", "thabeha01",
         "morriad01", "flynnjo01", "willide02", "okafoja01"]
INTL = {"nowitdi01", "gasolpa01", "milicda01"}                 # -> Overseas (RealGM)
HS_PREP = {"garneke01", "mcgratr01", "brownkw01", "malonmo01"}  # HS-to-pro, measurables-only
HARD_DRAFT = {"malonmo01": (1974, 0)}                          # bbref meta doesn't parse Moses


def parse_draft(pid):
    p = os.path.join(C.PAGES, f"bbref_{pid}.html")
    if not os.path.exists(p):
        return 0, 0
    html = open(p, encoding="utf-8", errors="replace").read()
    m = re.search(r"(\d+)(?:st|nd|rd|th) overall.{0,40}?(\d{4}) NBA Draft", html)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"Draft:.{0,160}?(\d{4}) NBA Draft", html, re.S)
    return (int(m.group(1)), 0) if m else (0, 0)


def main():
    ces = N.build()
    out = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    rgm_map = None
    pids = LEGENDS + BUSTS

    for i, pid in enumerate(pids, 1):
        if pid in out:
            continue
        info = ces.get(pid, {})
        ht, wt, cbb = C.bbref_page(pid)
        dy, pk = HARD_DRAFT.get(pid) or parse_draft(pid)
        card = {"height": ht, "weight": wt, "pos": info.get("pos"), "ces": info.get("ces"),
                "name": info.get("name", pid), "draft_year": dy, "pick": pk}
        col = None
        if pid in INTL:
            if rgm_map is None:
                rgm_map = RGM.url_map()
            for url in rgm_map.get(RGM.slug(card["name"]), [])[:4]:
                html = RGM.page(pid, url)
                if html:
                    _, rec = RGM.parse(html, dy or None)
                    if rec:
                        col = rec; col["conference"] = "Overseas"; break
            card["conference"] = (col or {}).get("conference") or "Overseas"
        elif pid in HS_PREP:
            card["conference"] = "High School"
        elif cbb:
            col = C.college_card(pid, cbb)
            card["conference"] = (col or {}).get("conference") or "—"
        else:                                    # Pippen (NAIA) etc. — measurables only
            card["conference"] = "—"
        card["college"] = col
        out[pid] = card
        kind = "STATS" if col else "measurables-only"
        print(f"{i:2}/{len(pids)} {pid:11} {card['name'][:22]:22} "
              f"{card['conference']:12} draft {dy} #{pk:<3} ces={card['ces']} {kind}")
        if i % 15 == 0:
            json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    have = sum(1 for c in out.values() if c.get("college"))
    print(f"\nDone. {len(out)} legend/bust cards; {have} with a stat line, {len(out)-have} measurables-only.")


if __name__ == "__main__":
    main()
