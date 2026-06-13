"""
WarRoom — NFL blind scouting draft game.

Players draft a 5-man roster (QB/RB/WR/WR/DEF) from compact scouting cards —
position, college conference, measurements, and the prospect's final college stat
line — with each prospect's identity hidden behind a codename until it is drafted.
The skill is recognizing who a stat line belongs to. The reveal shows who each card
was and how the roster scored.

Three ways to play, all sharing one draft engine:
  • Daily    — a global date-seeded board everyone gets that day; you draft solo
               and are ranked against everyone else who played the same board.
  • Online   — real-time multiplayer rooms (Socket.IO): create a room, share the
               code, snake-draft live against other people on their own devices.
  • Pass&Play— local hotseat on one screen.

Scoring: each player's Career Excel Score (es_scoring.py) — a season-by-season,
award-aware grade of their NFL career, comparable across positions. A team's score
is the sum of its five picks' CES.

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
from datetime import date

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
SLOT_SEQUENCE = ["QB", "RB", "WR", "WR", "DEF"]
SLOT_LABELS = {"QB": "Quarterback", "RB": "Running Back", "WR": "Wide Receiver",
               "TE": "Tight End", "DEF": "Defense"}
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
# Player grade = Career Excel Score (es_scoring.py), already cross-position
# comparable, so a team's rating is just the sum of its picks' CES — no
# position weighting needed (the old POSITION_WEIGHTS are retired).


# ── CARD + SCORE INDEXES ─────────────────────────────────────────────────────
CARDS: dict[int, dict] = {}
SCORES: dict[int, dict] = {}


def _num(v, nd=0):
    """Format a stat value; a real 0 shows as 0, a missing one as a dash."""
    return "—" if v is None else f"{v:,.{nd}f}"


def _statline(slot: str, cs: dict) -> list[dict]:
    """The prospect's FINAL college season, shown as raw numbers (plus a couple of
    derived rates). college_stats holds that last season's totals (see
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


def build_indexes() -> None:
    """Precompute the card + score lookups for the pool. A player's score is the
    Career Excel Score (computed offline by es_scoring.py and stored on the row),
    so this is a cheap read with no dependency on the season-page cache. Prospects
    whose card has too few real college stats (see _min_card_stats) are left out —
    a near-blank card is no fun to scout and unfair to score."""
    global CARDS, SCORES
    cstats = models.college_stats_map()
    pool = models.get_prospects()
    CARDS, SCORES = {}, {}
    for p in pool:
        card = _make_card(p, cstats.get((models.norm_name(p["name"]), p["draft_year"]), {}))
        if _card_stat_count(card) < _min_card_stats(p["slot"]):
            continue
        CARDS[p["id"]] = card
        ces = p["ces"] if p.get("ces") is not None else 0.0
        SCORES[p["id"]] = {"score": round(ces, 1)}                   # Career Excel Score


# ── DRAFT ENGINE (mode-agnostic; operates on a plain game dict) ───────────────

def _build_pick_plan(n_players: int) -> list[dict]:
    base, plan = list(range(n_players)), []
    for rnd, slot in enumerate(SLOT_SEQUENCE):
        for team in (base if rnd % 2 == 0 else base[::-1]):
            plan.append({"round": rnd, "slot": slot, "team": team})
    return plan


def _build_pool(n_players: int, rng: random.Random, buffer: int = POOL_CHOICE_BUFFER):
    """Sample a per-game board (one bucket per slot) + per-slot codenames. `rng`
    makes it reproducible — the Daily board passes a date-seeded RNG so everyone
    gets the identical board."""
    if not CARDS:
        build_indexes()
    chosen, codenames = [], {}
    for slot in dict.fromkeys(SLOT_SEQUENCE):          # stable, de-duped slot set
        ids = [p["id"] for p in models.get_prospects(slot) if p["id"] in CARDS]   # card-eligible only
        need = SLOT_SEQUENCE.count(slot) * n_players
        if len(ids) < need:
            raise ValueError(f"need {need} {slot}, have {len(ids)}")
        sample = rng.sample(ids, min(need + buffer, len(ids)))
        for i, pid in enumerate(sample, 1):
            codenames[str(pid)] = f"{slot}{i}"
        chosen.extend(sample)
    return chosen, codenames


def _new_game(names, rng=None, buffer=POOL_CHOICE_BUFFER, mode="local", day=None):
    rng = rng or random
    pool, codenames = _build_pool(len(names), rng, buffer)
    return {"players": names, "plan": _build_pick_plan(len(names)), "pick_index": 0,
            "picks": {str(t): [] for t in range(len(names))}, "pool": pool,
            "codenames": codenames, "last_pick": None, "mode": mode, "day": day}


def _taken(game): return {pid for ps in game["picks"].values() for pid in ps}


def _blind_card(pid, codename):
    c = CARDS[pid]
    return {"id": pid, "codename": codename, "slot": c["slot"], "position": c["position"],
            "height": c["height"], "weight": c["weight"], "forty": c["forty"],
            "conference": c["conference"], "last_year": c["last_year"], "stats": c["stats"]}


def _apply_pick(game, pid: int) -> str | None:
    """Validate + apply a pick to a game. Returns an error string or None."""
    plan, idx = game["plan"], game["pick_index"]
    if idx >= len(plan):
        return "Draft complete"
    cur, pool = plan[idx], set(game.get("pool") or [])
    if pid not in pool or pid in _taken(game) or CARDS.get(pid, {}).get("slot") != cur["slot"]:
        return "Card not available for this pick"
    game["picks"][str(cur["team"])].append(pid)
    game["pick_index"] += 1
    game["last_pick"] = pid
    return None


def _serialize(game: dict) -> dict:
    players, plan, idx = game["players"], game["plan"], game["pick_index"]
    cn, taken = game.get("codenames") or {}, _taken(game)
    rosters = []
    for t, name in enumerate(players):
        picks = [{"id": pid, "slot": CARDS[pid]["slot"], "codename": cn.get(str(pid), CARDS[pid]["slot"]),
                  "name": CARDS[pid]["name"]} for pid in game["picks"][str(t)] if pid in CARDS]
        rosters.append({"team": t, "name": name, "picks": picks})
    lp = game.get("last_pick")
    last_pick = None
    if lp in CARDS:
        lp_team = next((t for t in range(len(players)) if lp in game["picks"][str(t)]), 0)
        last_pick = {"codename": cn.get(str(lp), CARDS[lp]["slot"]), "name": CARDS[lp]["name"], "team": lp_team}

    if idx >= len(plan):
        return {"phase": "reveal", "mode": game.get("mode"), "players": players, "rosters": rosters,
                "reveal": _reveal(game), "last_pick": last_pick, "pickNumber": len(plan),
                "totalPicks": len(plan), "current": None, "available": []}
    cur = plan[idx]
    pool = set(game.get("pool") or [])
    avail = [_blind_card(p["id"], cn.get(str(p["id"]), p["slot"]))
             for p in models.get_prospects(cur["slot"]) if p["id"] in pool and p["id"] not in taken]
    avail.sort(key=lambda c: int(re.search(r"\d+", c["codename"]).group()) if re.search(r"\d+", c["codename"]) else 0)
    return {"phase": "draft", "mode": game.get("mode"), "players": players,
            "pickNumber": idx + 1, "totalPicks": len(plan),
            "current": {"team": cur["team"], "name": players[cur["team"]], "round": cur["round"] + 1,
                        "slot": cur["slot"], "slotLabel": SLOT_LABELS.get(cur["slot"], cur["slot"])},
            "available": avail, "rosters": rosters, "last_pick": last_pick, "reveal": None}


def _reveal(game: dict) -> dict:
    teams = []
    for t, name in enumerate(game["players"]):
        picks, total = [], 0.0
        for pid in game["picks"][str(t)]:
            c, sc = CARDS.get(pid), SCORES.get(pid, {"score": 0})
            if not c:
                continue
            score = sc["score"]                         # Career Excel Score (comparable across positions)
            total += score
            picks.append({"slot": c["slot"], "name": c["name"], "position": c["position"], "school": c["school"],
                          "draft_year": c["draft_year"], "draft_round": c["draft_round"], "is_starter": c["is_starter"],
                          "score": score, "weight": 1.0, "weighted": score})
        teams.append({"team": t, "name": name, "picks": picks, "total": round(total, 1)})
    teams.sort(key=lambda x: x["total"], reverse=True)
    for rank, team in enumerate(teams, 1):
        team["rank"] = rank
    return {"teams": teams,
            "scoring_note": "Each pick's grade is its Career Excel Score — how that NFL career "
                            "actually turned out (50 = an average starter's career, 90+ = all-time great). "
                            "Your team score is the sum of your five picks."}


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
_login_fails: dict[str, list] = {}          # key -> [timestamps] (brute-force throttle)
_FAIL_WINDOW, _FAIL_MAX = 600, 8             # >8 fails / 10 min -> temporary lockout


def _rate_limited(key: str) -> bool:
    now = time.time()
    hits = [t for t in _login_fails.get(key, []) if now - t < _FAIL_WINDOW]
    _login_fails[key] = hits
    return len(hits) >= _FAIL_MAX


def _note_fail(key: str):
    _login_fails.setdefault(key, []).append(time.time())


def _current_user():
    if session.get("user_id"):
        return {"user_id": session["user_id"], "username": session.get("username")}
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
    return jsonify({"user_id": user["id"], "username": user["username"]})


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
    return jsonify({"user_id": user["id"], "username": user["username"]})


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


def _eligible_summary() -> dict:
    """Board counts by slot over the card-eligible pool (what's actually draftable)."""
    if not CARDS:
        build_indexes()
    by_slot: dict[str, int] = {}
    for c in CARDS.values():
        by_slot[c["slot"]] = by_slot.get(c["slot"], 0) + 1
    return {"total": len(CARDS), "by_slot": by_slot}


def _lobby_state():
    return {"phase": "lobby", "minPlayers": MIN_PLAYERS, "maxPlayers": MAX_PLAYERS,
            "roster": SLOT_SEQUENCE, "pool": _eligible_summary()}


@app.route("/api/state")
def api_state():
    if not CARDS:
        build_indexes()
    game = session.get("game")
    return jsonify(_serialize(game) if game else _lobby_state())


@app.route("/api/new", methods=["POST"])
def api_new():
    names = [str(n).strip()[:24] for n in (request.get_json(silent=True) or {}).get("players", []) if str(n).strip()]
    if not (MIN_PLAYERS <= len(names) <= MAX_PLAYERS):
        return jsonify({"error": f"Need {MIN_PLAYERS}-{MAX_PLAYERS} players"}), 400
    build_indexes()
    try:
        game = _new_game(names)
    except ValueError as e:
        return jsonify({"error": f"Pool too small: {e}."}), 400
    session["game"] = game
    session.modified = True
    return jsonify(_serialize(game))


@app.route("/api/pick", methods=["POST"])
def api_pick():
    game = session.get("game")
    if not game:
        return jsonify({"error": "No active game"}), 400
    try:
        pid = int((request.get_json(silent=True) or {}).get("prospect_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "prospect_id required"}), 400
    err = _apply_pick(game, pid)
    if err:
        return jsonify({"error": err}), 400
    session["game"] = game
    session.modified = True
    return jsonify(_serialize(game))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    session.pop("game", None)
    session.modified = True
    return jsonify(_lobby_state())


# ── DAILY ────────────────────────────────────────────────────────────────────

def _today():
    return date.today().isoformat()


def _daily_seed(day: str) -> int:
    return int.from_bytes(day.encode(), "big") % (2**32)


def _serialize_daily(game: dict, record: bool = False) -> dict:
    state = _serialize(game)
    state["day"] = game["day"]
    u = _current_user()
    if state["phase"] == "reveal":
        total = state["reveal"]["teams"][0]["total"]
        if record and not game.get("recorded"):
            # per-account lock: record at most once per account per day
            already = models.user_daily_score(game["day"], u["user_id"]) if u else None
            if already is None:
                models.record_daily_score(game["day"], total, game["players"][0],
                                          user_id=u["user_id"] if u else None)
            game["recorded"] = True
        state["reveal"]["standing"] = models.daily_standing(game["day"], total)
        state["reveal"]["yourTotal"] = total
        if u:
            state["reveal"]["streak"] = models.user_daily_streak(u["user_id"])
    return state


@app.route("/api/daily/state")
def api_daily_state():
    if not CARDS:
        build_indexes()
    day = _today()
    game = session.get("daily")
    if game and game.get("day") == day:
        return jsonify(_serialize_daily(game))
    return jsonify({"phase": "daily_lobby", "day": day, "roster": SLOT_SEQUENCE,
                    "field": models.daily_standing(day, -1)["field"]})


@app.route("/api/daily/new", methods=["POST"])
def api_daily_new():
    build_indexes()
    day = _today()
    name = (str((request.get_json(silent=True) or {}).get("name", "")).strip()[:24]) or "You"
    game = session.get("daily")
    if not (game and game.get("day") == day):          # one board per day per session
        game = _new_game([name], rng=random.Random(_daily_seed(day)), buffer=DAILY_BUFFER,
                         mode="daily", day=day)
    session["daily"] = game
    session.modified = True
    return jsonify(_serialize_daily(game))


@app.route("/api/daily/pick", methods=["POST"])
def api_daily_pick():
    game = session.get("daily")
    if not game:
        return jsonify({"error": "No daily game"}), 400
    try:
        pid = int((request.get_json(silent=True) or {}).get("prospect_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "prospect_id required"}), 400
    err = _apply_pick(game, pid)
    if err:
        return jsonify({"error": err}), 400
    session["daily"] = game
    session.modified = True
    return jsonify(_serialize_daily(game, record=True))


# ── ONLINE (real-time rooms, Socket.IO) ──────────────────────────────────────
ROOMS: dict[str, dict] = {}            # code -> {seats:[{name,sid}], game, phase, host_sid}
SID_ROOM: dict[str, str] = {}          # sid -> room code
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_code() -> str:
    while True:
        code = "".join(random.choice(_CODE_ALPHABET) for _ in range(4))
        if code not in ROOMS:
            return code


def _room_lobby(room: dict, code: str) -> dict:
    return {"phase": "room_lobby", "code": code,
            "players": [s["name"] for s in room["seats"]],
            "minPlayers": MIN_PLAYERS, "maxPlayers": MAX_PLAYERS, "roster": SLOT_SEQUENCE}


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
    if not CARDS:
        build_indexes()
    name = (str((data or {}).get("name", "")).strip()[:24]) or "War Room 1"
    code = _gen_code()
    ROOMS[code] = {"seats": [{"name": name, "sid": request.sid}], "game": None,
                   "phase": "lobby", "host_sid": request.sid}
    SID_ROOM[request.sid] = code
    join_room(code)
    emit("joined", {"code": code, "team": 0, "host": True})
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
    room["seats"].append({"name": name, "sid": request.sid})
    SID_ROOM[request.sid] = code
    join_room(code)
    emit("joined", {"code": code, "team": len(room["seats"]) - 1, "host": False})
    _emit_room(code)


@socketio.on("start_draft")
def on_start_draft(data):
    code = str((data or {}).get("code", "")).strip().upper()
    room = ROOMS.get(code)
    if not room or request.sid != room["host_sid"] or room["phase"] != "lobby":
        return
    names = [s["name"] for s in room["seats"]]
    if not (MIN_PLAYERS <= len(names) <= MAX_PLAYERS):
        emit("room_error", {"message": "Need at least one war room to start."}); return
    try:
        room["game"] = _new_game(names, mode="online")
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
    if not room or request.sid != room["host_sid"]:
        return
    room["game"], room["phase"] = None, "lobby"
    _emit_room(code)


def _drop_sid(sid):
    code = SID_ROOM.pop(sid, None)
    room = ROOMS.get(code) if code else None
    if not room:
        return
    if room["phase"] == "lobby":
        room["seats"] = [s for s in room["seats"] if s["sid"] != sid]
        if not room["seats"]:
            ROOMS.pop(code, None); return
        if sid == room["host_sid"]:
            room["host_sid"] = room["seats"][0]["sid"]
        _emit_room(code)
    else:
        for s in room["seats"]:
            if s["sid"] == sid:
                s["sid"] = None                 # seat stays so turn order holds
        socketio.emit("opponent_left", {}, room=code)


@socketio.on("leave_room")
def on_leave_room(data):
    _drop_sid(request.sid)


@socketio.on("disconnect")
def on_disconnect():
    _drop_sid(request.sid)


models.init_db()
build_indexes()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5053, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
