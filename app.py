"""
WarRoom — a blind scouting draft game, NFL and NBA.

Players draft a 5-man roster (NFL: QB/RB/WR/WR/DEF · NBA: PG/SG/SF/PF/C) from
compact scouting cards — position, league/conference, measurements, and the
prospect's final pre-pro stat line — with each identity hidden behind a codename
until it is drafted. The skill is recognizing who a stat line belongs to. The
reveal shows who each card was and how the roster scored.

One engine, two sports (see SPORTS): NFL cards/scores come from the SQLite pool
(models); NBA from data/nba_cards.json (a prebuilt pool — es_scoring_nba.py +
the scraper_nba_* tools — loaded into memory at startup).

Three ways to play, all sharing the engine:
  • Daily    — a global date-seeded board everyone gets that day; you draft solo
               and are ranked against everyone else who played the same board.
  • Online   — real-time multiplayer rooms (Socket.IO): create a room, share the
               code, snake-draft live against other people on their own devices.
  • Pass&Play— local hotseat on one screen.

Scoring: each player's Career Excel Score (es_scoring.py / es_scoring_nba.py) — a
season-by-season, award-aware grade of their pro career, comparable across
positions. A team's score is the sum of its five picks' CES.

Run:  python app.py   ->   http://localhost:5053
"""

from dotenv import load_dotenv
load_dotenv()

import logging
import os
import random
import re
import secrets
import string
import time
from datetime import date, timedelta

from flask import Flask, jsonify, redirect, render_template, request, session
from flask_socketio import SocketIO, join_room, emit

import models

app = Flask(__name__)
# A FIXED secret in prod keeps sessions valid across restarts; falls back to a
# random one for local dev (set SECRET_KEY in the environment on the server).
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,                       # JS can't read the cookie
    SESSION_COOKIE_SAMESITE="Lax",                      # CSRF mitigation
    # Only require HTTPS for the cookie in prod (would break plain-HTTP localhost).
    SESSION_COOKIE_SECURE=os.environ.get("WARROOM_HTTPS", "").lower() in ("1", "true", "yes"),
)
# threading locally (plays nice with `python app.py` on Windows); eventlet on the
# server to match the gunicorn eventlet worker (set WARROOM_ASYNC=eventlet there).
socketio = SocketIO(app, async_mode=os.environ.get("WARROOM_ASYNC", "threading"),
                    cors_allowed_origins="*")

# Log unhandled errors to a file instead of dying silently.
logging.basicConfig(
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "warroom.log"),
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@app.errorhandler(Exception)
def _log_unhandled(e):
    app.logger.exception("Unhandled error: %s", e)
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    return jsonify({"error": "Server error"}), 500

# ── DRAFT RULES ──────────────────────────────────────────────────────────────
MIN_PLAYERS, MAX_PLAYERS = 1, 6
POOL_CHOICE_BUFFER = 4
DAILY_BUFFER = 5                       # a slightly richer board for the daily solo run
# A prospect needs at least this many real college stats on his card to make the
# board (no blank cards). Defense gets a lower bar: pre-2005 college box scores
# only tracked interceptions, so recognizable era-limited defenders (Woodson,
# Wake…) carry just one stat — strict offense filtering would wrongly cut them.
MIN_CARD_STATS = 4
MIN_CARD_STATS_DEF = 1


def _min_card_stats(slot: str) -> int:
    return MIN_CARD_STATS_DEF if slot == "DEF" else MIN_CARD_STATS


# ── SPORTS ───────────────────────────────────────────────────────────────────
# WarRoom runs two games off one engine. Each sport owns its roster slots, slot
# labels, blind-card statline, and its own card/score/grade indexes. NFL cards +
# scores come from the SQLite pool (models); NBA from data/nba_cards.json — a
# small prebuilt pool (scoring via es_scoring_nba.py, cards via the scraper_nba_*
# tools) loaded into memory at startup, so the running app needs no basketball DB.
DEFAULT_SPORT = "nfl"
NBA_CARDS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nba_cards.json")

SPORTS: dict[str, dict] = {
    "nfl": {"label": "NFL",
            "slots": ["QB", "RB", "WR", "WR", "DEF"],
            "slot_labels": {"QB": "Quarterback", "RB": "Running Back", "WR": "Wide Receiver",
                            "TE": "Tight End", "DEF": "Defense"},
            "cards": {}, "scores": {}, "by_slot": {}},
    "nba": {"label": "NBA",
            "slots": ["PG", "SG", "SF", "PF", "C"],
            "slot_labels": {"PG": "Point Guard", "SG": "Shooting Guard", "SF": "Small Forward",
                            "PF": "Power Forward", "C": "Center"},
            "cards": {}, "scores": {}, "by_slot": {}},
}

_SCORING_NOTE = {
    "nfl": "Each pick is graded against every prospect at its position — A is elite for the slot, "
           "C is a middling pick. Your team score adds up how good all five NFL careers turned out; "
           "the highest total wins.",
    "nba": "Each pick is graded against every prospect at its position — A is elite for the slot, "
           "C is a middling pick. Your team score adds up how good all five NBA careers turned out; "
           "the highest total wins.",
}


def _valid_sport(s) -> str:
    return s if s in SPORTS else DEFAULT_SPORT


def _cards(sport): return SPORTS[sport]["cards"]
def _scores(sport): return SPORTS[sport]["scores"]
def _slots(sport): return SPORTS[sport]["slots"]
def _slot_labels(sport): return SPORTS[sport]["slot_labels"]
def _sport_of(game): return _valid_sport((game or {}).get("sport"))


# ── CARD + SCORE INDEXES ─────────────────────────────────────────────────────
def _num(v, nd=0):
    """Format a stat value; a real 0 shows as 0, a missing one as a dash."""
    return "—" if v is None else f"{v:,.{nd}f}"


def _pct(v):
    """A shooting rate stored 0–1 (e.g. .467) shown as a percentage (46.7)."""
    return "—" if v is None else f"{v * 100:.1f}"


def _statline(slot: str, cs: dict) -> list[dict]:
    """The NFL prospect's FINAL college season, shown as raw numbers (plus a couple
    of derived rates). college_stats holds that last season's totals (see
    scraper_collegestats); a missing stat renders as a dash, a real zero as 0."""
    def n(k): return cs.get(k)
    if slot == "QB":
        comp = (100 * n("pass_cmp") / n("pass_att")) if (n("pass_cmp") is not None and n("pass_att")) else None
        return [{"k": "Comp %", "v": _num(comp, 1)}, {"k": "Pass Yds", "v": _num(n("pass_yds"))},
                {"k": "Pass TD", "v": _num(n("pass_td"))}, {"k": "INT", "v": _num(n("pass_int"))},
                {"k": "Rush Yds", "v": _num(n("rush_yds"))}]
    if slot == "RB":
        ypc = (n("rush_yds") / n("rush_att")) if (n("rush_yds") is not None and n("rush_att")) else None
        return [{"k": "Rush Yds", "v": _num(n("rush_yds"))}, {"k": "Yds/Carry", "v": _num(ypc, 1)},
                {"k": "Rush TD", "v": _num(n("rush_td"))}, {"k": "Rec Yds", "v": _num(n("rec_yds"))}]
    if slot in ("WR", "TE"):
        ypr = (n("rec_yds") / n("rec")) if (n("rec_yds") is not None and n("rec")) else None
        return [{"k": "Rec", "v": _num(n("rec"))}, {"k": "Rec Yds", "v": _num(n("rec_yds"))},
                {"k": "Yds/Catch", "v": _num(ypr, 1)}, {"k": "Rec TD", "v": _num(n("rec_td"))}]
    return [{"k": "Tackles", "v": _num(n("tackles"))}, {"k": "Sacks", "v": _num(n("sacks"), 1)},
            {"k": "INT", "v": _num(n("def_int"))}, {"k": "TFL", "v": _num(n("tfl"), 1)}]


def _statline_nba(col: dict) -> list[dict]:
    """The NBA prospect's final pre-NBA season (college or RealGM international) —
    the five stats present across both sources, so every card reads the same."""
    def n(k): return col.get(k)
    return [{"k": "PPG", "v": _num(n("ppg"), 1)}, {"k": "RPG", "v": _num(n("rpg"), 1)},
            {"k": "APG", "v": _num(n("apg"), 1)}, {"k": "FG%", "v": _pct(n("fg_pct"))},
            {"k": "3P%", "v": _pct(n("fg3_pct"))}]


def _make_card(p: dict, cs: dict) -> dict:
    return {"id": p["id"], "slot": p["slot"], "position": p["position"],
            "height": p["height"], "weight": p["weight"], "forty": p["forty"],
            "conference": cs.get("conference") or "—", "stats": _statline(p["slot"], cs),
            "last_year": cs.get("last_year"),
            "name": p["name"], "school": p["school"] or cs.get("school"),
            "draft_year": p["draft_year"], "draft_round": p["draft_round"], "is_starter": p["is_starter"]}


def _card_stat_count(card: dict) -> int:
    """How many of a card's college stats are actually known (not a dash)."""
    return sum(1 for s in card["stats"] if s["v"] != "—")


# Letter grade for a pick = where the player ranks among everyone at his position
# (a draft report card: A = elite for the slot, C ≈ a middling pick, F = a bust).
# Percentile-based because raw CES skews low across the pool (median ~16), so a
# fixed 50=C scale would brand most real players an F.
_GRADE_TABLE = [(.96, "A+"), (.88, "A"), (.78, "A-"), (.66, "B+"), (.54, "B"),
                (.42, "B-"), (.30, "C+"), (.20, "C"), (.12, "C-"), (.06, "D")]


def _grade_from_pct(pct: float) -> str:
    for thr, g in _GRADE_TABLE:
        if pct >= thr:
            return g
    return "F"


def _grade_and_index(sp: dict) -> None:
    """Bucket a sport's cards by slot and assign each a letter grade by its
    percentile within that slot (worst → best)."""
    by_slot: dict[str, list] = {}
    for pid, c in sp["cards"].items():
        by_slot.setdefault(c["slot"], []).append(pid)
    for pids in by_slot.values():
        pids.sort(key=lambda pid: sp["scores"][pid]["score"])
        m = len(pids)
        for rank, pid in enumerate(pids):
            sp["scores"][pid]["grade"] = _grade_from_pct((rank + 0.5) / m)
    sp["by_slot"] = by_slot


def _build_nfl_indexes() -> None:
    """NFL card + score lookups from the SQLite pool. A player's score is the
    Career Excel Score (computed offline by es_scoring.py and stored on the row).
    Cards with too few real college stats (see _min_card_stats) are dropped — a
    near-blank card is no fun to scout and unfair to score."""
    sp = SPORTS["nfl"]
    cstats = models.college_stats_map()
    sp["cards"], sp["scores"] = {}, {}
    for p in models.get_prospects():
        card = _make_card(p, cstats.get((models.norm_name(p["name"]), p["draft_year"]), {}))
        if _card_stat_count(card) < _min_card_stats(p["slot"]):
            continue
        sp["cards"][p["id"]] = card
        sp["scores"][p["id"]] = {"score": round(p["ces"] if p.get("ces") is not None else 0.0, 1)}
    _grade_and_index(sp)


def _build_nba_indexes() -> None:
    """NBA card + score lookups from data/nba_cards.json (a prebuilt, curated
    pool — every player is intentionally recognizable). Each player gets a
    synthetic int id (offset to never collide with NFL ids). Measurables-only
    cards (no pre-NBA stat line, e.g. Giannis/Jalen Green) stay on the board —
    their stats render as dashes."""
    import json
    sp = SPORTS["nba"]
    sp["cards"], sp["scores"] = {}, {}
    try:
        raw = json.load(open(NBA_CARDS_JSON, encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        sp["by_slot"] = {}
        return
    for i, (_pid, c) in enumerate(sorted(raw.items())):
        col = c.get("college")
        gid, slot = 2_000_000 + i, c["pos"]
        sp["cards"][gid] = {"id": gid, "slot": slot, "position": slot,
                            "height": c.get("height"), "weight": c.get("weight"), "forty": None,
                            "conference": c.get("conference") or "—", "stats": _statline_nba(col or {}),
                            "last_year": (col or {}).get("last_year"), "name": c["name"],
                            "school": c.get("conference") or "—",
                            "draft_year": c.get("draft_year"), "draft_round": None,
                            "draft_pick": c.get("pick"), "is_starter": 0}
        sp["scores"][gid] = {"score": round(c.get("ces") or 0.0, 1)}
    _grade_and_index(sp)


def build_indexes() -> None:
    """Refresh NFL indexes (cheap DB read) and build NBA once (static JSON)."""
    _build_nfl_indexes()
    if not SPORTS["nba"]["cards"]:
        _build_nba_indexes()


def _ensure(sport: str) -> None:
    if not SPORTS[sport]["cards"]:
        build_indexes()


# ── DRAFT ENGINE (mode-agnostic; operates on a plain game dict) ───────────────

def _build_pick_plan(n_players: int, sport: str) -> list[dict]:
    base, plan = list(range(n_players)), []
    for rnd, slot in enumerate(_slots(sport)):
        for team in (base if rnd % 2 == 0 else base[::-1]):
            plan.append({"round": rnd, "slot": slot, "team": team})
    return plan


def _build_pool(n_players: int, rng: random.Random, buffer: int, sport: str):
    """Sample a per-game board (one bucket per slot) + per-slot codenames. `rng`
    makes it reproducible — the Daily board passes a date-seeded RNG so everyone
    gets the identical board."""
    _ensure(sport)
    slots, by_slot = _slots(sport), SPORTS[sport]["by_slot"]
    chosen, codenames = [], {}
    for slot in dict.fromkeys(slots):                  # stable, de-duped slot set
        ids = list(by_slot.get(slot, []))             # card-eligible ids at this slot
        need = slots.count(slot) * n_players
        if len(ids) < need:
            raise ValueError(f"need {need} {slot}, have {len(ids)}")
        sample = rng.sample(ids, min(need + buffer, len(ids)))
        for i, pid in enumerate(sample, 1):
            codenames[str(pid)] = f"{slot}{i}"
        chosen.extend(sample)
    return chosen, codenames


def _new_game(names, rng=None, buffer=POOL_CHOICE_BUFFER, mode="local", day=None, sport=DEFAULT_SPORT):
    rng = rng or random
    sport = _valid_sport(sport)
    pool, codenames = _build_pool(len(names), rng, buffer, sport)
    return {"players": names, "plan": _build_pick_plan(len(names), sport), "pick_index": 0,
            "picks": {str(t): [] for t in range(len(names))}, "pool": pool,
            "codenames": codenames, "last_pick": None, "mode": mode, "day": day, "sport": sport}


def _taken(game): return {pid for ps in game["picks"].values() for pid in ps}


def _blind_card(pid, codename, sport):
    c = _cards(sport)[pid]
    return {"id": pid, "codename": codename, "slot": c["slot"], "position": c["position"],
            "height": c["height"], "weight": c["weight"], "forty": c["forty"],
            "conference": c["conference"], "last_year": c["last_year"], "stats": c["stats"]}


def _apply_pick(game, pid: int) -> str | None:
    """Validate + apply a pick to a game. Returns an error string or None."""
    cards = _cards(_sport_of(game))
    plan, idx = game["plan"], game["pick_index"]
    if idx >= len(plan):
        return "Draft complete"
    cur, pool = plan[idx], set(game.get("pool") or [])
    if pid not in pool or pid in _taken(game) or cards.get(pid, {}).get("slot") != cur["slot"]:
        return "Card not available for this pick"
    game["picks"][str(cur["team"])].append(pid)
    game["pick_index"] += 1
    game["last_pick"] = pid
    return None


def _serialize(game: dict) -> dict:
    sport = _sport_of(game)
    cards, scores = _cards(sport), _scores(sport)
    players, plan, idx = game["players"], game["plan"], game["pick_index"]
    cn, taken = game.get("codenames") or {}, _taken(game)
    rosters = []
    for t, name in enumerate(players):
        picks = [{"id": pid, "slot": cards[pid]["slot"], "codename": cn.get(str(pid), cards[pid]["slot"]),
                  "name": cards[pid]["name"], "grade": scores.get(pid, {}).get("grade", "—")}
                 for pid in game["picks"][str(t)] if pid in cards]
        rosters.append({"team": t, "name": name, "picks": picks})
    lp = game.get("last_pick")
    last_pick = None
    if lp in cards:
        lp_team = next((t for t in range(len(players)) if lp in game["picks"][str(t)]), 0)
        last_pick = {"codename": cn.get(str(lp), cards[lp]["slot"]), "name": cards[lp]["name"], "team": lp_team}

    base = {"mode": game.get("mode"), "sport": sport, "roster": _slots(sport), "players": players}
    if idx >= len(plan):
        return {**base, "phase": "reveal", "rosters": rosters, "reveal": _reveal(game),
                "last_pick": last_pick, "pickNumber": len(plan), "totalPicks": len(plan),
                "current": None, "available": []}
    cur = plan[idx]
    pool = set(game.get("pool") or [])
    avail = [_blind_card(pid, cn.get(str(pid), cards[pid]["slot"]), sport)
             for pid in pool if pid in cards and cards[pid]["slot"] == cur["slot"] and pid not in taken]
    avail.sort(key=lambda c: int(re.search(r"\d+", c["codename"]).group()) if re.search(r"\d+", c["codename"]) else 0)
    return {**base, "phase": "draft", "pickNumber": idx + 1, "totalPicks": len(plan),
            "current": {"team": cur["team"], "name": players[cur["team"]], "round": cur["round"] + 1,
                        "slot": cur["slot"], "slotLabel": _slot_labels(sport).get(cur["slot"], cur["slot"])},
            "available": avail, "rosters": rosters, "last_pick": last_pick, "reveal": None}


def _reveal(game: dict) -> dict:
    sport = _sport_of(game)
    cards, scores = _cards(sport), _scores(sport)
    teams = []
    for t, name in enumerate(game["players"]):
        picks, total = [], 0.0
        for pid in game["picks"][str(t)]:
            c, sc = cards.get(pid), scores.get(pid, {"score": 0})
            if not c:
                continue
            total += sc["score"]                        # Career Excel Score drives the team total
            picks.append({"slot": c["slot"], "name": c["name"], "position": c["position"], "school": c["school"],
                          "draft_year": c["draft_year"], "draft_round": c["draft_round"],
                          "grade": sc.get("grade", "—")})
        teams.append({"team": t, "name": name, "picks": picks, "total": round(total, 1)})
    teams.sort(key=lambda x: x["total"], reverse=True)
    for rank, team in enumerate(teams, 1):
        team["rank"] = rank
    return {"teams": teams, "scoring_note": _SCORING_NOTE[sport]}


# ── LOCAL (pass & play) ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


# ── ACCOUNTS ─────────────────────────────────────────────────────────────────
# Admins (e.g. Kelp) get the past-boards archive without the once-per-day lock.
ADMIN_USERS = {u.strip().lower() for u in os.environ.get("WARROOM_ADMINS", "kelp").split(",") if u.strip()}
_login_fails: dict[str, list] = {}          # key -> [timestamps] (brute-force throttle)
_FAIL_WINDOW, _FAIL_MAX = 600, 8             # >8 fails / 10 min -> temporary lockout


def _is_admin(username: str | None) -> bool:
    return bool(username) and username.lower() in ADMIN_USERS


def _rate_limited(key: str) -> bool:
    now = time.time()
    hits = [t for t in _login_fails.get(key, []) if now - t < _FAIL_WINDOW]
    _login_fails[key] = hits
    return len(hits) >= _FAIL_MAX


def _note_fail(key: str):
    _login_fails.setdefault(key, []).append(time.time())


def _current_user():
    if session.get("user_id"):
        name = session.get("username")
        return {"user_id": session["user_id"], "username": name, "admin": _is_admin(name)}
    return None


@app.route("/api/me")
def api_me():
    return jsonify(_current_user() or {"user_id": None})


@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.get_json(silent=True) or {}
    username, password = (d.get("username") or "").strip(), d.get("password") or ""
    if not models.valid_username(username):
        return jsonify({"error": "Username: 2–20 letters, numbers or underscore"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    user = models.create_user(username, password)
    if not user:
        return jsonify({"error": "Username already taken"}), 409
    session["user_id"], session["username"] = user["id"], user["username"]
    return jsonify({"user_id": user["id"], "username": user["username"], "admin": _is_admin(user["username"])})


@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    username, password = (d.get("username") or "").strip(), d.get("password") or ""
    key = (request.remote_addr or "?") + "|" + username.lower()
    if _rate_limited(key):
        return jsonify({"error": "Too many attempts — try again in a few minutes"}), 429
    user = models.get_user(username)
    if not user or not models.verify_password(user["password_hash"], password):
        _note_fail(key)
        return jsonify({"error": "Invalid username or password"}), 401   # generic on purpose
    session["user_id"], session["username"] = user["id"], user["username"]
    return jsonify({"user_id": user["id"], "username": user["username"], "admin": _is_admin(user["username"])})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    session.pop("username", None)
    session.modified = True
    return jsonify({"ok": True})


@app.route("/api/account/delete", methods=["POST"])
def api_account_delete():
    u = _current_user()
    if not u:
        return jsonify({"error": "Not logged in"}), 401
    models.delete_user(u["user_id"])
    session.clear()
    return jsonify({"ok": True})


def _req_sport() -> str:
    """The sport for this request — from the query string or JSON body."""
    s = request.args.get("sport") or (request.get_json(silent=True) or {}).get("sport")
    return _valid_sport(s)


def _eligible_summary(sport: str) -> dict:
    """Board counts by slot over the card-eligible pool (what's actually draftable)."""
    _ensure(sport)
    by_slot: dict[str, int] = {}
    for c in _cards(sport).values():
        by_slot[c["slot"]] = by_slot.get(c["slot"], 0) + 1
    return {"total": len(_cards(sport)), "by_slot": by_slot}


def _lobby_state(sport: str):
    return {"phase": "lobby", "sport": sport, "minPlayers": MIN_PLAYERS, "maxPlayers": MAX_PLAYERS,
            "roster": _slots(sport), "pool": _eligible_summary(sport)}


@app.route("/api/state")
def api_state():
    sport = _req_sport()
    _ensure(sport)
    game = session.get(f"game_{sport}")
    return jsonify(_serialize(game) if game else _lobby_state(sport))


@app.route("/api/new", methods=["POST"])
def api_new():
    d = request.get_json(silent=True) or {}
    sport = _valid_sport(d.get("sport"))
    names = [str(n).strip()[:24] for n in d.get("players", []) if str(n).strip()]
    if not (MIN_PLAYERS <= len(names) <= MAX_PLAYERS):
        return jsonify({"error": f"Need {MIN_PLAYERS}-{MAX_PLAYERS} players"}), 400
    _ensure(sport)
    try:
        game = _new_game(names, sport=sport)
    except ValueError as e:
        return jsonify({"error": f"Pool too small: {e}."}), 400
    session[f"game_{sport}"] = game
    session.modified = True
    return jsonify(_serialize(game))


@app.route("/api/pick", methods=["POST"])
def api_pick():
    sport = _req_sport()
    game = session.get(f"game_{sport}")
    if not game:
        return jsonify({"error": "No active game"}), 400
    try:
        pid = int((request.get_json(silent=True) or {}).get("prospect_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "prospect_id required"}), 400
    err = _apply_pick(game, pid)
    if err:
        return jsonify({"error": err}), 400
    session[f"game_{sport}"] = game
    session.modified = True
    return jsonify(_serialize(game))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    sport = _req_sport()
    session.pop(f"game_{sport}", None)
    session.modified = True
    return jsonify(_lobby_state(sport))


# ── DAILY ────────────────────────────────────────────────────────────────────
DAILY_WINDOW = 30                      # how many past days are in the playable archive


def _today():
    return date.today().isoformat()


def _daily_seed(sport: str, day: str) -> int:
    return int.from_bytes(f"{sport}:{day}".encode(), "big") % (2**32)


def _valid_day(day: str, admin: bool = False) -> str | None:
    """A playable board date: a real date, not in the future, and (for non-admins)
    within the archive window. Returns the normalized ISO day or None."""
    try:
        d = date.fromisoformat((day or "").strip())
    except ValueError:
        return None
    today = date.today()
    if d > today or (not admin and (today - d).days > DAILY_WINDOW):
        return None
    return d.isoformat()


def _serialize_daily(game: dict, record: bool = False) -> dict:
    sport = _sport_of(game)
    u = _current_user()
    # Keep a signed-in player's roster/result under their account name even if the
    # board was started anonymously earlier in the session.
    if u and game.get("players") and game["players"][0] != u["username"]:
        game["players"][0] = u["username"]
    state = _serialize(game)
    state["day"] = game["day"]
    if state["phase"] == "reveal":
        total = state["reveal"]["teams"][0]["total"]
        # Admins play the archive as practice — their runs are never recorded.
        if record and not game.get("recorded") and not (u and u["admin"]):
            already = models.user_daily_score(game["day"], u["user_id"], sport) if u else None
            if already is None:                 # per-account lock: once per account per day per sport
                models.record_daily_score(game["day"], total, game["players"][0],
                                          user_id=u["user_id"] if u else None, sport=sport)
            game["recorded"] = True
        state["reveal"]["standing"] = models.daily_standing(game["day"], total, sport)
        state["reveal"]["yourTotal"] = total
        if u:
            state["reveal"]["streak"] = models.user_daily_streak(u["user_id"], sport)
    return state


@app.route("/api/daily/state")
def api_daily_state():
    sport = _req_sport()
    _ensure(sport)
    day = _today()
    game = session.get(f"daily_{sport}")
    if game and game.get("day") == day:
        return jsonify(_serialize_daily(game))
    return jsonify({"phase": "daily_lobby", "sport": sport, "day": day, "roster": _slots(sport),
                    "field": models.daily_standing(day, -1, sport)["field"]})


@app.route("/api/daily/days")
def api_daily_days():
    """The playable archive: recent days with field size + whether you've played."""
    sport = _req_sport()
    u = _current_user()
    today = date.today()
    out = []
    for i in range(DAILY_WINDOW + 1):
        d = (today - timedelta(days=i)).isoformat()
        out.append({"day": d, "field": models.daily_standing(d, -1, sport)["field"],
                    "played": bool(u and models.user_daily_score(d, u["user_id"], sport) is not None)})
    return jsonify({"days": out})


@app.route("/api/daily/new", methods=["POST"])
def api_daily_new():
    req = request.get_json(silent=True) or {}
    sport = _valid_sport(req.get("sport"))
    _ensure(sport)
    u = _current_user()
    day = _valid_day(str(req.get("day") or ""), admin=bool(u and u["admin"])) or _today()
    client_name = str(req.get("name", "")).strip()[:24]
    name = (u["username"] if u else client_name) or "You"   # logged-in players use their account name
    game = session.get(f"daily_{sport}")
    if not (game and game.get("day") == day):          # one board per day per session per sport
        game = _new_game([name], rng=random.Random(_daily_seed(sport, day)), buffer=DAILY_BUFFER,
                         mode="daily", day=day, sport=sport)
    session[f"daily_{sport}"] = game
    session.modified = True
    return jsonify(_serialize_daily(game))


@app.route("/api/daily/pick", methods=["POST"])
def api_daily_pick():
    sport = _req_sport()
    game = session.get(f"daily_{sport}")
    if not game:
        return jsonify({"error": "No daily game"}), 400
    try:
        pid = int((request.get_json(silent=True) or {}).get("prospect_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "prospect_id required"}), 400
    err = _apply_pick(game, pid)
    if err:
        return jsonify({"error": err}), 400
    session[f"daily_{sport}"] = game
    session.modified = True
    return jsonify(_serialize_daily(game, record=True))


# ── ONLINE (real-time rooms, Socket.IO) ──────────────────────────────────────
# Each seat carries a stable `token` (a secret the client stores) so a player who
# drops can reclaim their exact seat on reconnect — sids change, tokens don't.
# Host is tracked by token (`host_token`) for the same reason.
ROOMS: dict[str, dict] = {}            # code -> {seats:[{name,sid,token}], game, phase, host_token}
SID_ROOM: dict[str, str] = {}          # sid -> room code
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_code() -> str:
    while True:
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(4))
        if code not in ROOMS:
            return code


def _seat_by_sid(room: dict, sid: str) -> dict | None:
    return next((s for s in room["seats"] if s.get("sid") == sid), None)


def _is_host(room: dict, sid: str) -> bool:
    seat = _seat_by_sid(room, sid)
    return bool(seat) and seat.get("token") == room.get("host_token")


def _room_lobby(room: dict, code: str) -> dict:
    sport = _valid_sport(room.get("sport"))
    return {"phase": "room_lobby", "code": code, "sport": sport,
            "players": [s["name"] for s in room["seats"]],
            "minPlayers": MIN_PLAYERS, "maxPlayers": MAX_PLAYERS, "roster": _slots(sport)}


def _emit_room(code: str):
    room = ROOMS.get(code)
    if not room:
        return
    if room["phase"] == "lobby":
        socketio.emit("lobby_update", _room_lobby(room, code), room=code)
    else:
        state = _serialize(room["game"])
        state["code"] = code
        socketio.emit("game_update", state, room=code)


@socketio.on("create_room")
def on_create_room(data):
    sport = _valid_sport((data or {}).get("sport"))
    _ensure(sport)
    name = (str((data or {}).get("name", "")).strip()[:24]) or "War Room 1"
    code = _gen_code()
    token = secrets.token_hex(8)
    ROOMS[code] = {"seats": [{"name": name, "sid": request.sid, "token": token}], "game": None,
                   "phase": "lobby", "host_token": token, "sport": sport}
    SID_ROOM[request.sid] = code
    join_room(code)
    emit("joined", {"code": code, "team": 0, "host": True, "token": token})
    _emit_room(code)


@socketio.on("join_room")
def on_join_room(data):
    code = str((data or {}).get("code", "")).strip().upper()
    name = (str((data or {}).get("name", "")).strip()[:24]) or f"War Room"
    room = ROOMS.get(code)
    if not room:
        emit("room_error", {"message": f'Room "{code}" not found.'}); return
    if room["phase"] != "lobby":
        emit("room_error", {"message": "That draft has already started."}); return
    if len(room["seats"]) >= MAX_PLAYERS:
        emit("room_error", {"message": "That room is full."}); return
    token = secrets.token_hex(8)
    room["seats"].append({"name": name, "sid": request.sid, "token": token})
    SID_ROOM[request.sid] = code
    join_room(code)
    emit("joined", {"code": code, "team": len(room["seats"]) - 1, "host": False, "token": token})
    _emit_room(code)


@socketio.on("start_draft")
def on_start_draft(data):
    code = str((data or {}).get("code", "")).strip().upper()
    room = ROOMS.get(code)
    if not room or not _is_host(room, request.sid) or room["phase"] != "lobby":
        return
    names = [s["name"] for s in room["seats"]]
    if not (MIN_PLAYERS <= len(names) <= MAX_PLAYERS):
        emit("room_error", {"message": "Need at least one war room to start."}); return
    try:
        room["game"] = _new_game(names, mode="online", sport=_valid_sport(room.get("sport")))
    except ValueError as e:
        emit("room_error", {"message": f"Pool too small: {e}."}); return
    room["phase"] = "draft"
    _emit_room(code)


@socketio.on("make_pick")
def on_make_pick(data):
    code = str((data or {}).get("code", "")).strip().upper()
    room = ROOMS.get(code)
    if not room or room["phase"] != "draft":
        return
    game = room["game"]
    cur = game["plan"][game["pick_index"]]
    if room["seats"][cur["team"]]["sid"] != request.sid:
        emit("room_error", {"message": "It's not your pick."}); return
    try:
        pid = int((data or {}).get("prospect_id"))
    except (TypeError, ValueError):
        return
    err = _apply_pick(game, pid)
    if err:
        emit("room_error", {"message": err}); return
    if game["pick_index"] >= len(game["plan"]):
        room["phase"] = "reveal"
    _emit_room(code)


@socketio.on("restart_room")
def on_restart_room(data):
    code = str((data or {}).get("code", "")).strip().upper()
    room = ROOMS.get(code)
    if not room or not _is_host(room, request.sid):
        return
    room["game"], room["phase"] = None, "lobby"
    _emit_room(code)


def _drop_sid(sid, permanent=False):
    """Handle a socket going away. In the LOBBY the seat is removed (a returning
    player re-joins by typing the code). DURING A GAME the seat is held open (sid
    cleared) so team indices and reconnect survive a blip — and if that leaves only
    one player in an active draft, the draft ends with a disconnection notice."""
    code = SID_ROOM.pop(sid, None)
    room = ROOMS.get(code) if code else None
    if not room:
        return
    if room["phase"] == "lobby":
        was_host = any(s["sid"] == sid and s.get("token") == room.get("host_token")
                       for s in room["seats"])
        room["seats"] = [s for s in room["seats"] if s["sid"] != sid]
        if not room["seats"]:
            ROOMS.pop(code, None); return
        if was_host:
            room["host_token"] = room["seats"][0]["token"]
        _emit_room(code)
        return
    # In a game: hold the seat (preserve indices + allow reconnect).
    for s in room["seats"]:
        if s["sid"] == sid:
            s["sid"] = None
    if room["phase"] == "draft" and len([s for s in room["seats"] if s["sid"]]) <= 1:
        socketio.emit("game_over_disconnect", {}, room=code)   # everyone else is gone
        for s in room["seats"]:
            SID_ROOM.pop(s["sid"], None)
        ROOMS.pop(code, None)
        return
    socketio.emit("opponent_left", {}, room=code)
    _emit_room(code)


@socketio.on("rejoin_room")
def on_rejoin_room(data):
    """Reclaim a seat after a disconnect/reload using the seat's stored token."""
    code = str((data or {}).get("code", "")).strip().upper()
    token = str((data or {}).get("token", "") or "")
    room = ROOMS.get(code)
    idx = (next((i for i, s in enumerate(room["seats"]) if s.get("token") == token), None)
           if room and token else None)
    if idx is None:
        emit("rejoin_failed", {}); return
    seat = room["seats"][idx]
    seat["sid"] = request.sid
    SID_ROOM[request.sid] = code
    join_room(code)
    emit("joined", {"code": code, "team": idx,
                    "host": seat["token"] == room.get("host_token"), "token": token})
    _emit_room(code)                            # resync this client (and confirm to the room)


@socketio.on("leave_room")
def on_leave_room(data):
    _drop_sid(request.sid, permanent=True)      # explicit leave drops the seat for good


@socketio.on("disconnect")
def on_disconnect():
    _drop_sid(request.sid)


models.init_db()
build_indexes()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5053, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
