"""
WarRoom — data layer.

SQLite-backed store for draftable prospects (the blind scouting pool) and,
later, game/pick state. Single DB file in the project root, mirroring the
house style used across the other projects.
"""

import os
import re
import sqlite3

from werkzeug.security import generate_password_hash, check_password_hash

# Accounts can share StatCheck's users DB (one login across the sports games) by
# setting STATCHECK_USERS_DB to its path; otherwise a local `users` table in
# warroom.db is used. Mirrors StatGolf's approach.
STATCHECK_USERS_DB = os.environ.get("STATCHECK_USERS_DB")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,20}$")

DB_PATH = os.environ.get(
    "WARROOM_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "warroom.db"),
)

# Roster slots the game drafts for. Post-pivot the pool is built from drafted
# players (PFR draft classes) across these positions; TE was added 2026-06-10.
ROSTER_SLOTS = ("QB", "RB", "WR", "TE", "DEF")

# Raw source positions → roster slot.
_DEF_POSITIONS = {"DE", "DT", "NT", "EDGE", "CB", "S", "FS", "SS", "DB",
                  "LB", "ILB", "OLB", "MLB", "SLB", "WLB"}


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    """Normalized join key: lowercase, punctuation stripped, name suffixes
    (Jr/III/…) dropped. Used to match a prospect to its college_stats row across
    sources that format names differently (e.g. 'Michael Penix Jr.')."""
    n = (name or "").lower().replace(".", " ").replace("'", "")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    parts = [p for p in n.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


def position_to_slot(position: str) -> str | None:
    """Map a source position label to a roster slot, or None if not draftable."""
    p = (position or "").strip().upper()
    if p == "QB":
        return "QB"
    if p in ("RB", "HB", "FB"):
        return "RB"
    if p in ("WR",):
        return "WR"
    if p in ("TE",):
        return "TE"
    if p in _DEF_POSITIONS:
        return "DEF"
    return None


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = connect()
    con.execute("""
        CREATE TABLE IF NOT EXISTS prospects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source          TEXT NOT NULL,
            source_url      TEXT,
            name            TEXT NOT NULL,
            slot            TEXT NOT NULL,          -- QB / RB / WR / DEF
            position        TEXT NOT NULL,          -- raw source position
            school          TEXT,
            draft_year      INTEGER,
            height          TEXT,
            weight          INTEGER,
            hand            TEXT,
            forty           REAL,
            projected_round TEXT,
            grade           REAL,                   -- source numeric grade (e.g. NFL.com)
            report          TEXT,                   -- full report (revealed)
            blind_report    TEXT,                   -- redacted (shown in draft)
            pfr_id          TEXT,                   -- join key to PFR outcomes
            draft_round     INTEGER,                -- actual NFL draft round (pool = R1-3)
            draft_pick      INTEGER,                -- overall pick number
            cfb_url         TEXT,                   -- direct Sports-Ref CFB player page (from PFR)
            nfl_av          REAL,                   -- PFR career Approximate Value (scoring signal)
            nfl_games       INTEGER,                -- NFL career games (scoring sample size)
            is_starter      INTEGER DEFAULT 0,      -- started >=2 games in 2025 (current starter)
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(source, name, draft_year, position)
        )
    """)
    # Migrate older DBs that predate columns added later.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(prospects)")}
    for col, decl in (("grade", "REAL"), ("draft_round", "INTEGER"),
                      ("draft_pick", "INTEGER"), ("cfb_url", "TEXT"),
                      ("nfl_av", "REAL"), ("nfl_games", "INTEGER"),
                      ("is_starter", "INTEGER DEFAULT 0"),
                      ("ces", "REAL")):                  # Career Excel Score (see es_scoring.py)
        if col not in cols:
            con.execute(f"ALTER TABLE prospects ADD COLUMN {col} {decl}")

    # College production stats (the new draft-card data, post-pivot). These
    # belong to the real player, not to a scouting source, so they live in their
    # own table keyed by a normalized name and are joined onto a prospect at
    # card-build time. The counting values hold the player's FINAL college season
    # (last_year); the card shows them raw plus a couple of derived rates
    # (comp% = cmp/att, YPC = rush_yds/rush_att, yds/catch = rec_yds/rec).
    con.execute("""
        CREATE TABLE IF NOT EXISTS college_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name_key    TEXT NOT NULL,      -- norm_name() join key
            name        TEXT NOT NULL,      -- as scraped
            school      TEXT,
            conference  TEXT,
            slot        TEXT,
            draft_year  INTEGER,
            source      TEXT,               -- e.g. 'sportsref_cfb'
            source_url  TEXT,
            height      TEXT,                -- card measurement (e.g. '6-2')
            weight      INTEGER,             -- card measurement (lb)
            games       INTEGER,
            seasons     INTEGER,
            -- passing
            pass_cmp    REAL, pass_att REAL, pass_yds REAL, pass_td REAL, pass_int REAL,
            -- rushing
            rush_att    REAL, rush_yds REAL, rush_td REAL,
            -- receiving
            rec         REAL, rec_yds REAL, rec_td REAL, targets REAL,
            -- defense
            tackles     REAL, sacks REAL, def_int REAL, tfl REAL,
            created_at  TEXT DEFAULT (datetime('now')),
            UNIQUE(name_key, draft_year)
        )
    """)
    ccols = {r["name"] for r in con.execute("PRAGMA table_info(college_stats)")}
    for col, decl in (("height", "TEXT"), ("weight", "INTEGER"),
                      ("last_year", "INTEGER")):    # the player's final college season (card line)
        if col not in ccols:
            con.execute(f"ALTER TABLE college_stats ADD COLUMN {col} {decl}")

    # Daily-mode submissions — one row per completed daily board, used to rank a
    # player's roster against everyone else who played that day's seed.
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_scores (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            day         TEXT NOT NULL,          -- YYYY-MM-DD (the global seed)
            name        TEXT,
            score       REAL NOT NULL,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_scores(day)")
    _dcols = {r["name"] for r in con.execute("PRAGMA table_info(daily_scores)")}
    # attach daily results to an account (for per-account lock + streaks/history)
    if "user_id" not in _dcols:
        con.execute("ALTER TABLE daily_scores ADD COLUMN user_id INTEGER")
    # WarRoom runs more than one sport off this table; existing rows are NFL.
    if "sport" not in _dcols:
        con.execute("ALTER TABLE daily_scores ADD COLUMN sport TEXT DEFAULT 'nfl'")
    con.execute("CREATE INDEX IF NOT EXISTS idx_daily_day_sport ON daily_scores(day, sport)")

    # Our own career-value inputs (public NFL honors/role) — replaces PFR's AV
    # for scoring. Keyed by pfr_id. See build_nfl_value.py / app._career_value.
    con.execute("""
        CREATE TABLE IF NOT EXISTS nfl_value (
            pfr_id              TEXT PRIMARY KEY,
            games               INTEGER,
            all_pro             INTEGER,   -- 1st-team All-Pro seasons
            pro_bowls           INTEGER,
            primary_starter_yrs INTEGER,
            source              TEXT,      -- 'draft' (cached draft page) | 'player' (player page)
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """)

    # Local accounts (used when not sharing StatCheck's users DB).
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    con.close()


# ── NFL CAREER VALUE (our own metric inputs) ─────────────────────────────────

def upsert_nfl_value(d: dict) -> None:
    con = connect()
    con.execute(
        "INSERT INTO nfl_value (pfr_id, games, all_pro, pro_bowls, primary_starter_yrs, source) "
        "VALUES (:pfr_id,:games,:all_pro,:pro_bowls,:primary_starter_yrs,:source) "
        "ON CONFLICT(pfr_id) DO UPDATE SET games=excluded.games, all_pro=excluded.all_pro, "
        "pro_bowls=excluded.pro_bowls, primary_starter_yrs=excluded.primary_starter_yrs, "
        "source=excluded.source, updated_at=datetime('now')",
        {k: d.get(k) for k in ("pfr_id", "games", "all_pro", "pro_bowls",
                               "primary_starter_yrs", "source")},
    )
    con.commit()
    con.close()


def nfl_value_map() -> dict:
    con = connect()
    rows = con.execute("SELECT * FROM nfl_value").fetchall()
    con.close()
    return {r["pfr_id"]: dict(r) for r in rows}


# ── ACCOUNTS ──────────────────────────────────────────────────────────────────

def _users_con() -> sqlite3.Connection:
    """Connection to the users DB — StatCheck's shared one if configured, else
    WarRoom's own."""
    if STATCHECK_USERS_DB and os.path.exists(STATCHECK_USERS_DB):
        con = sqlite3.connect(f"file:{STATCHECK_USERS_DB}?mode=rw", uri=True)
    else:
        con = connect()
    con.row_factory = sqlite3.Row
    return con


def valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username or ""))


def get_user(username: str) -> dict | None:
    con = _users_con()
    row = con.execute(
        "SELECT id, username, password_hash FROM users WHERE LOWER(username)=LOWER(?)",
        (username,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def create_user(username: str, password: str) -> dict | None:
    """Create an account (salted password hash). Returns the user, or None if the
    username is taken. Caller validates username/password format first."""
    con = _users_con()
    try:
        con.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return None
    con.close()
    return get_user(username)


def verify_password(stored_hash: str, password: str) -> bool:
    return check_password_hash(stored_hash, password)


def delete_user(user_id: int) -> None:
    """Right-to-delete: remove the account and detach its daily results."""
    con = _users_con()
    con.execute("DELETE FROM users WHERE id=?", (user_id,))
    con.commit()
    con.close()
    d = connect()
    d.execute("UPDATE daily_scores SET user_id=NULL WHERE user_id=?", (user_id,))
    d.commit()
    d.close()


_CSTAT_FIELDS = (
    "name_key", "name", "school", "conference", "slot", "draft_year",
    "source", "source_url", "height", "weight", "games", "seasons", "last_year",
    "pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int",
    "rush_att", "rush_yds", "rush_td",
    "rec", "rec_yds", "rec_td", "targets",
    "tackles", "sacks", "def_int", "tfl",
)


def upsert_college_stats(d: dict) -> None:
    """Insert or replace one player's college career totals, keyed by
    (name_key, draft_year)."""
    con = connect()
    cols = ", ".join(_CSTAT_FIELDS)
    placeholders = ", ".join(f":{c}" for c in _CSTAT_FIELDS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _CSTAT_FIELDS
                        if c not in ("name_key", "draft_year"))
    con.execute(
        f"INSERT INTO college_stats ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(name_key, draft_year) DO UPDATE SET {updates}",
        {c: d.get(c) for c in _CSTAT_FIELDS},
    )
    con.commit()
    con.close()


def distinct_prospects_to_resolve(include_resolved: bool = False) -> list[dict]:
    """One row per real player (deduped across scouting sources). Normally only
    those still lacking a college_stats entry (the scrape work list); pass
    include_resolved=True to get EVERY pool player (used by the cache-only
    re-parse). Prefers a row that carries a school. Newest draft years first."""
    con = connect()
    rows = con.execute("""
        SELECT p.name, p.slot, p.draft_year,
               MAX(p.school) AS school, MAX(p.position) AS position,
               MAX(p.cfb_url) AS cfb_url, MAX(p.pfr_id) AS pfr_id
          FROM prospects p
         GROUP BY LOWER(TRIM(p.name)), p.draft_year, p.slot
         ORDER BY p.draft_year DESC, p.slot
    """).fetchall()
    con.close()
    have = set() if include_resolved else {(r["name_key"], r["draft_year"]) for r in _existing_cstats()}
    out = []
    for r in rows:
        d = dict(r)
        d["name_key"] = norm_name(d["name"])
        if (d["name_key"], d["draft_year"]) in have:
            continue
        out.append(d)
    return out


def _existing_cstats() -> list[dict]:
    con = connect()
    rows = con.execute("SELECT name_key, draft_year FROM college_stats").fetchall()
    con.close()
    return [dict(r) for r in rows]


def college_stats_map() -> dict:
    """All college_stats rows keyed by (name_key, draft_year) for joining onto a
    prospect at card-build time."""
    con = connect()
    rows = con.execute("SELECT * FROM college_stats").fetchall()
    con.close()
    return {(r["name_key"], r["draft_year"]): dict(r) for r in rows}


def college_stats_summary() -> dict:
    con = connect()
    total = con.execute("SELECT COUNT(*) FROM college_stats").fetchone()[0]
    by_slot = {r["slot"]: r["n"] for r in con.execute(
        "SELECT slot, COUNT(*) n FROM college_stats GROUP BY slot")}
    con.close()
    return {"total": total, "by_slot": by_slot}


def upsert_prospect(p: dict) -> None:
    """Insert or replace one prospect keyed by (source, name, draft_year, position)."""
    con = connect()
    con.execute("""
        INSERT INTO prospects
            (source, source_url, name, slot, position, school, draft_year,
             height, weight, hand, forty, projected_round, grade, report, blind_report,
             pfr_id, draft_round, draft_pick, cfb_url, nfl_av, nfl_games, is_starter)
        VALUES
            (:source, :source_url, :name, :slot, :position, :school, :draft_year,
             :height, :weight, :hand, :forty, :projected_round, :grade, :report, :blind_report,
             :pfr_id, :draft_round, :draft_pick, :cfb_url, :nfl_av, :nfl_games, :is_starter)
        ON CONFLICT(source, name, draft_year, position) DO UPDATE SET
            source_url      = excluded.source_url,
            slot            = excluded.slot,
            school          = excluded.school,
            height          = excluded.height,
            weight          = excluded.weight,
            hand            = excluded.hand,
            forty           = excluded.forty,
            projected_round = excluded.projected_round,
            grade           = excluded.grade,
            report          = excluded.report,
            blind_report    = excluded.blind_report,
            pfr_id          = COALESCE(excluded.pfr_id, prospects.pfr_id),
            draft_round     = excluded.draft_round,
            draft_pick      = excluded.draft_pick,
            cfb_url         = COALESCE(excluded.cfb_url, prospects.cfb_url),
            nfl_av          = excluded.nfl_av,
            nfl_games       = excluded.nfl_games,
            is_starter      = excluded.is_starter
    """, {
        "source": p["source"],
        "source_url": p.get("source_url"),
        "name": p["name"],
        "slot": p["slot"],
        "position": p["position"],
        "school": p.get("school"),
        "draft_year": p.get("draft_year"),
        "height": p.get("height"),
        "weight": p.get("weight"),
        "hand": p.get("hand"),
        "forty": p.get("forty"),
        "projected_round": p.get("projected_round"),
        "grade": p.get("grade"),
        "report": p.get("report"),
        "blind_report": p.get("blind_report"),
        "pfr_id": p.get("pfr_id"),
        "draft_round": p.get("draft_round"),
        "draft_pick": p.get("draft_pick"),
        "cfb_url": p.get("cfb_url"),
        "nfl_av": p.get("nfl_av"),
        "nfl_games": p.get("nfl_games"),
        "is_starter": p.get("is_starter", 0),
    })
    con.commit()
    con.close()


def wipe_prospects() -> None:
    """Drop every prospect row (and any redacted reports with them). Used when
    rebuilding the pool from a new authoritative source — post-pivot the pool is
    drafted players, not the old scouting-report rows."""
    con = connect()
    con.execute("DELETE FROM prospects")
    con.commit()
    con.close()


def prospect_pfr_ids() -> set:
    """All pfr_id values already in the pool — used to dedup current starters
    against drafted players (the same player is one card)."""
    con = connect()
    rows = con.execute("SELECT pfr_id FROM prospects WHERE pfr_id IS NOT NULL").fetchall()
    con.close()
    return {r["pfr_id"] for r in rows}


def mark_starter(pfr_id: str, height: str | None, weight: int | None) -> None:
    """Flag an existing pool player as a 2025 starter, backfilling height/weight
    if they were missing (drafted rows get measurements from the combine scrape,
    but the roster page already carries them)."""
    con = connect()
    con.execute(
        "UPDATE prospects SET is_starter=1, "
        "height=COALESCE(height, ?), weight=COALESCE(weight, ?) WHERE pfr_id=?",
        (height, weight, pfr_id),
    )
    con.commit()
    con.close()


def delete_prospects(ids: list[int]) -> int:
    """Remove prospect rows by id — used to prune players who fail the board
    eligibility rule (too small an NFL footprint to be recognizable)."""
    if not ids:
        return 0
    con = connect()
    con.executemany("DELETE FROM prospects WHERE id=?", [(i,) for i in ids])
    con.commit()
    con.close()
    return len(ids)


def set_ces(mapping: dict) -> int:
    """Bulk-store each prospect's Career Excel Score (id -> CES). Precomputed
    offline by es_scoring.py and shipped in the DB, so the running app never needs
    PythonProject2's season-page cache."""
    con = connect()
    con.executemany("UPDATE prospects SET ces=? WHERE id=?",
                    [(float(v), int(i)) for i, v in mapping.items()])
    con.commit()
    n = con.total_changes
    con.close()
    return n


def get_prospects(slot: str | None = None) -> list[dict]:
    con = connect()
    if slot:
        rows = con.execute("SELECT * FROM prospects WHERE slot=? ORDER BY draft_year, name", (slot,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM prospects ORDER BY slot, draft_year, name").fetchall()
    con.close()
    return [dict(r) for r in rows]


def apply_measurements(year: int, by_pfr: dict, by_name: dict) -> int:
    """Stamp height/weight/forty onto a draft year's prospects from a combine
    scrape. Matches each prospect first by pfr_id, then by normalized name.
    by_pfr / by_name map their key -> {'height','weight','forty'}. Returns the
    number of prospect rows updated."""
    con = connect()
    rows = con.execute(
        "SELECT id, name, pfr_id FROM prospects WHERE draft_year=?", (year,)
    ).fetchall()
    n = 0
    for r in rows:
        m = by_pfr.get(r["pfr_id"]) if r["pfr_id"] else None
        if m is None:
            m = by_name.get(norm_name(r["name"]))
        if not m:
            continue
        con.execute(
            "UPDATE prospects SET height=?, weight=?, forty=? WHERE id=?",
            (m.get("height"), m.get("weight"), m.get("forty"), r["id"]),
        )
        n += 1
    con.commit()
    con.close()
    return n


def measurement_coverage() -> dict:
    con = connect()
    total = con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    hw = con.execute(
        "SELECT COUNT(*) FROM prospects WHERE height IS NOT NULL AND weight IS NOT NULL"
    ).fetchone()[0]
    forty = con.execute("SELECT COUNT(*) FROM prospects WHERE forty IS NOT NULL").fetchone()[0]
    con.close()
    return {"total": total, "have_height_weight": hw, "have_forty": forty}


def record_daily_score(day: str, score: float, name: str | None = None,
                        user_id: int | None = None, sport: str = "nfl") -> None:
    con = connect()
    con.execute("INSERT INTO daily_scores (day, score, name, user_id, sport) VALUES (?,?,?,?,?)",
                (day, float(score), (name or "")[:24], user_id, sport))
    con.commit()
    con.close()


def user_daily_score(day: str, user_id: int, sport: str = "nfl"):
    """A logged-in user's recorded score for a day (or None) — enforces one
    submission per account per day (per sport) and lets us re-show their result."""
    con = connect()
    row = con.execute("SELECT score FROM daily_scores WHERE day=? AND user_id=? AND sport=? "
                      "ORDER BY id LIMIT 1", (day, user_id, sport)).fetchone()
    con.close()
    return row["score"] if row else None


def user_daily_streak(user_id: int, sport: str = "nfl") -> int:
    """Consecutive days (ending today or yesterday) the user has a daily result
    in this sport."""
    from datetime import date, timedelta
    con = connect()
    days = {r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM daily_scores WHERE user_id=? AND sport=?", (user_id, sport))}
    con.close()
    if not days:
        return 0
    today = date.today()
    start = today if today.isoformat() in days else today - timedelta(days=1)
    streak, d = 0, start
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


def daily_standing(day: str, score: float, sport: str = "nfl") -> dict:
    """Where a given score lands among everyone who has played that day's board
    (for this sport): field size, rank (1 = best), and percentile (top X%)."""
    con = connect()
    field = con.execute("SELECT COUNT(*) FROM daily_scores WHERE day=? AND sport=?",
                        (day, sport)).fetchone()[0]
    better = con.execute("SELECT COUNT(*) FROM daily_scores WHERE day=? AND sport=? AND score>?",
                         (day, sport, float(score))).fetchone()[0]
    best = con.execute("SELECT MAX(score) FROM daily_scores WHERE day=? AND sport=?",
                       (day, sport)).fetchone()[0]
    con.close()
    rank = better + 1
    pct = max(1, round(100 * rank / field)) if field else 100       # top X% (best = small)
    return {"field": field, "rank": rank, "percentile": pct, "best": best}


def pool_summary() -> dict:
    """Counts by slot and draft year — handy for verifying a scrape."""
    con = connect()
    by_slot = {r["slot"]: r["n"] for r in con.execute(
        "SELECT slot, COUNT(*) n FROM prospects GROUP BY slot")}
    by_year = {r["draft_year"]: r["n"] for r in con.execute(
        "SELECT draft_year, COUNT(*) n FROM prospects GROUP BY draft_year")}
    total = con.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
    con.close()
    return {"total": total, "by_slot": by_slot, "by_year": by_year}
