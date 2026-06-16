"""WarRoom NBA — rebuild data/nba_cards.json as the curated recognizable pool.

Takes the 253-player set (data/_nba_set.json['scoreable']) = union of:
 lottery picks last 5 drafts, top-5 last 10, #1 last 15; All-NBA all-3 teams last 5,
 1st+2nd last 10, 1st-team last 15; current starters (gs>=30, 2026); Finals
 starters last 10 seasons. Merges existing + newly-scraped cards, applies the
 display-label rules (overseas leagues -> "Overseas"; high-school/prep -> "High
 School"; US college keeps its conference), and writes the pool in place.
The old full 652-card pool is backed up first.
"""
import json, shutil, collections

CARDS = "data/nba_cards.json"
NEW = "data/nba_newcards.json"
SET = "data/_nba_set.json"
BACKUP = "data/nba_cards_full652_backup.json"

# measurables-only players who are foreign-born -> "Overseas" (the rest of the
# stat-less group are US prep / G-League Ignite / Overtime Elite -> "High School")
FOREIGN_MO = {"antetgi01", "kuminjo01", "daniedy01"}
# US development-league competitions that should read as prep route, not overseas
US_DEV_CONF = {"NBA G League", "Intercontinental Cup"}


def label(card):
    """Return the display conference per the user's rules."""
    col = card.get("college")
    src = (col or {}).get("source")
    pid_conf = card.get("conference")
    if src == "hs" or pid_conf == "High School":
        return "High School"
    if not col:                                   # measurables-only
        return "Overseas" if card["_pid"] in FOREIGN_MO else "High School"
    if src in ("realgm", "wikipedia"):            # foreign / development club
        return "High School" if (col.get("conference") in US_DEV_CONF) else "Overseas"
    return pid_conf                               # US college -> keep real conference


def main():
    existing = json.load(open(CARDS, encoding="utf-8"))
    new = json.load(open(NEW, encoding="utf-8"))
    sset = json.load(open(SET, encoding="utf-8"))["scoreable"]
    shutil.copy(CARDS, BACKUP)

    pool = {}
    for pid in sset:
        card = dict(new.get(pid) or existing[pid])
        card["_pid"] = pid
        conf = label(card)
        card.pop("_pid")
        card["conference"] = conf
        if card.get("college"):
            card["college"]["conference"] = conf
        pool[pid] = card

    json.dump(pool, open(CARDS, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    # report
    by_pos = collections.Counter(c["pos"] for c in pool.values())
    by_conf = collections.Counter(c["conference"] for c in pool.values())
    stat = sum(1 for c in pool.values() if c.get("college"))
    print(f"Rebuilt {CARDS}: {len(pool)} players (backup -> {BACKUP})")
    print(f"  by position: {dict(by_pos)}")
    print(f"  with stat line: {stat}   measurables-only: {len(pool)-stat}")
    print(f"  Overseas: {by_conf['Overseas']}   High School: {by_conf['High School']}")
    print("\n  Non-college label assignments (review):")
    for pid, c in sorted(pool.items(), key=lambda kv: kv[1]["conference"]):
        col = c.get("college"); src = (col or {}).get("source")
        if src in ("realgm", "wikipedia", "hs") or not col:
            kind = "HS-stats" if src == "hs" else ("foreign-stats" if src in ("realgm", "wikipedia") else "measurables-only")
            print(f"    {c['conference']:12} | {c['name'][:24]:24} | {kind}")


if __name__ == "__main__":
    main()
