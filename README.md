# WarRoom

A competitive **blind NFL scouting draft**. You draft a five-man roster (QB / RB / WR / WR / DEF)
from compact scouting cards — position, college conference, measurements, and college
statistical averages — with each prospect's identity hidden behind a codename until it's
drafted. Reading the tape (and recognizing who a stat line belongs to) is the whole skill.
The reveal shows who each card was and how their NFL career actually turned out.

Three ways to play, all on one draft engine:

- **Daily** — a global date-seeded board everyone gets that day; draft solo, ranked against
  everyone else who played the same board.
- **Online** — real-time multiplayer rooms (Socket.IO): create a room, share the code,
  snake-draft live against other people.
- **Pass & Play** — local hotseat on one screen.

## Stack

Flask + Flask-SocketIO + SQLite (single `index.html` front end). Mirrors the house style of
the sibling sports games (StatCheck / StatGolf).

## Run locally

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # (Linux/macOS: .venv/bin/...)
python app.py            # -> http://localhost:5053
```

## Scoring — ES / CES

A player's grade is their **Career Excel Score**, defined in [`es_scoring.py`](es_scoring.py)
(full rationale in that file's docstring):

- **ES (Excel Score)** grades a single season vs. the year's top producers at that position,
  stat by stat, plus bonuses for awards won (Pro Bowl / All-Pro / MVP / DPOY …). 50 = an
  average starter's season; it's uncapped, so all-time seasons run past 100.
- **CES** rolls a player's best seasons into tiers (50% / 30% / 20%), weighted by games
  played, rewarding sustained, high-level careers over one-year wonders.

CES is **precomputed offline** from cached PFR season pages and stored on each prospect, so
the running app never scrapes:

```bash
python es_scoring.py            # print Top-10 CES per position
python es_scoring.py --build    # compute CES for the pool -> warroom.db
```

A team's score at the reveal is the sum of its five picks' CES (positions are already
comparable, so no positional weighting).

## Deployment

- Configure via env vars — see [`.env.example`](.env.example) (`SECRET_KEY`, `WARROOM_HTTPS=1`,
  `WARROOM_ASYNC=eventlet`, `STATCHECK_USERS_DB`, `PORT`).
- Run under gunicorn with the eventlet worker: `gunicorn -c gunicorn.conf.py app:app`
  (see the `Procfile`). One worker — room state is in-process.
- **`warroom.db` is not in git** (it holds live daily scores) — ship the prebuilt DB to the
  server separately, and don't run the scrapers there.
```
