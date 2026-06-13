"""
WarRoom — unified ingestion pipeline (proposal §7).

One re-runnable command that scrapes/normalizes prospect scouting profiles from
all three sources into the prospects table. Every source has its own cache, so
re-runs only fetch what's new; the game reads the resulting DB and never scrapes
live during play.

    python ingest.py                 # all three sources
    python ingest.py --only walterfootball pauline
    python ingest.py --dry-run

Sources:
  walterfootball — static HTML, all years (~2010+). The historical backbone.
  pauline        — Tony Pauline's PFN reports, enumerated via the WP REST API.
  nflcom         — NFL.com / Lance Zierlein, rendered via a headless browser;
                   current draft cycle only (no historical year index), adds a
                   numeric prospect grade.
"""

import argparse

import models
import scrape_common as sc
import scraper_walterfootball as wf
import scraper_nflcom as nflcom
import scraper_pauline as pauline

SOURCES = ("walterfootball", "pauline", "nflcom")


def run(only: list[str], save: bool) -> None:
    models.init_db()
    todo = only or list(SOURCES)

    if "walterfootball" in todo:
        print("\n=== WalterFootball (all years) ===")
        wf.scrape(wf.ALL_YEARS, wf.ALL_POSITIONS, save=save)

    if "pauline" in todo:
        print("\n=== Tony Pauline (PFN) ===")
        pauline.scrape(save=save)

    if "nflcom" in todo:
        print("\n=== NFL.com (Zierlein) — current cycle ===")
        try:
            nflcom.scrape(save=save)
        finally:
            sc.close_driver()

    print("\n=== Pool summary ===")
    s = models.pool_summary()
    con = models.connect()
    by_src = {r["source"]: r["n"] for r in
              con.execute("SELECT source, COUNT(*) n FROM prospects GROUP BY source")}
    con.close()
    print("  total:", s["total"], "| by source:", by_src)
    print("  by slot:", s["by_slot"])


def main():
    ap = argparse.ArgumentParser(description="WarRoom multi-source prospect ingestion")
    ap.add_argument("--only", nargs="+", choices=SOURCES, help="Run only these sources")
    ap.add_argument("--dry-run", action="store_true", help="Parse but don't write to the DB")
    args = ap.parse_args()
    run(args.only, save=not args.dry_run)


if __name__ == "__main__":
    main()
