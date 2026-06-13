# Developer Proposal: New Daily Multiplayer Draft Game

## Your Role

You are a high-level web developer working for a large sports media company. The company is building a brand-new competitive multiplayer web game and you are responsible for architecting and building it end to end. You have full latitude to recommend the technology stack, data architecture, and implementation approach you believe is best. Before writing any code, read this entire document. If anything is unspecified or ambiguous, ask clarifying questions rather than making assumptions. Several parts of the scoring formula are intentionally left as placeholders for the product owner to fill in — do not invent values for these.

---

## 1. Concept Summary

This is a competitive, multiplayer NFL "blind scouting draft" game. Two to five players compete in a snake draft. The twist: players are not shown who they are drafting. Each draftable player is presented only through their real pre-draft scouting profile (measurements and written scouting report), with identifying information hidden. Players draft a roster based purely on their ability to read scouting reports, then a backend formula scores each roster against the players' real NFL career production and ranks the competing teams.

The skill being tested is scouting judgment: can you read a pre-draft evaluation and predict who actually became a productive NFL player, better than your opponents can?

---

## 2. Core Game Flow

1. **Lobby:** 2 to 5 players join a game session.
2. **The draft pool is assembled:** The system selects a set of real NFL prospects (across many draft years) whose scouting profiles will be offered in this game. All prospects in a single game are at positions that fit the required roster (see Roster Structure). The same position is represented by the same set of reports for all drafters in a game — every drafter is choosing from the same shared, depleting pool.
3. **Snake draft:** Players draft in snake order (e.g., with order 1-2-3, the next round reverses to 3-2-1, then 1-2-3 again, and so on). Each player picks one prospect per turn from the shared remaining pool. Once a prospect is drafted, they are removed from the pool for everyone.
4. **Blind information only:** During the draft, each draftable prospect card shows ONLY their scouting information (measurements + written scouting report). Identifying details are hidden (see Hidden Information).
5. **Discussion phase:** After all rosters are complete, before the reveal, players get a discussion period to argue who drafted the best team.
6. **Reveal + scoring:** The system reveals the true identities of all drafted players and computes each team's backend score. Teams are ranked into an ordered list (1st, 2nd, etc.). The reveal should explain the scoring well enough that players understand why they placed where they did.
7. **Post-game loops:** Support a rematch option, and the ability to surface/share the results (the discrepancies and rankings are intended to drive discussion and replay).

---

## 3. Roster Structure

Each player drafts a fixed roster of exactly 5 players:

- 1 QB
- 1 RB
- 2 WR
- 1 Defensive player

IMPORTANT CONSTRAINT: Within a single game, every scouting report offered should be at the positions needed to fill this roster. The product owner has specified that "position should be the same for all reports in a game" at the slot level — i.e., the pool is structured so drafters are choosing among comparable position groups for each roster slot. Confirm the exact intended interpretation with the product owner before building the pool-assembly logic (see Open Questions).

---

## 4. Hidden vs. Shown Information

**Shown on each draftable card (the blind prompt):**
- Physical measurements (height, weight, and combine measurables where available — e.g., 40-yard dash)
- The written scouting report text

**Hidden during the draft:**
- Player name
- College / school (explicitly hidden — the product owner has determined college gives away too much)
- Draft year
- Any other identifying metadata

The goal is to keep the prospect "fuzzy": a knowledgeable player might recognize some entries, but should not be able to reliably identify all of them. The cross-year nature of the pool (prospects drawn from many different draft classes) is a deliberate feature that supports this fuzziness.

**Revealed after the draft:** Full identity and the career-production data behind the scoring.

---

## 5. Data Sources

Three scouting-report sources are to be used for the blind prompt data. Build an ingestion pipeline that scrapes/parses and normalizes prospect profiles from:

1. **WalterFootball** — historical backbone; static HTML prospect profiles organized by position, with broad year coverage.
2. **NFL.com (Lance Zierlein profiles)** — modern, structured profiles that include written reports, measurables, and a numeric prospect grade.
3. **Tony Pauline profiles** — deep-volume scouting reports (note: Pauline's archive is spread across multiple sites by era; the pipeline may need to target more than one domain to assemble his historical reports).

The career-production ("outcome") data used for scoring comes from **Pro Football Reference (PFR)**, which provides per-player career statistics. The product owner has already scraped the direct PFR stats to be used (see Scoring). The system must join each scouting profile to the correct PFR player record. Expect to need fuzzy name matching plus disambiguation on attributes such as position and draft year, since names alone will not uniquely or cleanly match across sources.

Respect each source's access constraints and rate limits. Confirm with the product owner which exact fields are available from the already-scraped PFR dataset before designing the scoring module.

---

## 6. Scoring System

The scoring is the heart of the game. The product owner controls the football logic. Build the system to the following specification, and leave the football-specific weights and formula details as configurable placeholders the product owner will set.

**6.1 Outcome stats used:** Scoring is NOT based on Approximate Value (AV). It is based on the direct per-player statistics scraped from PFR. Each player's outcome is computed primarily from their **career averages**, but with their **top single season weighted up**, so that a player with one or more excellent seasons is pulled above a player with a merely average career across the board. The exact "top-season weighting" formula is a placeholder — `[PRODUCT OWNER TO DEFINE: how heavily the best season is weighted relative to the career average]`.

**6.2 Per-player score (0–100):** Each drafted player is scored on a 0–100 scale based on their position-relevant outcome stats. A player is scored relative to others at their own position (a QB's passing production is only meaningfully comparable to other QBs, etc.). The exact mapping from raw stats to the 0–100 value is a placeholder — `[PRODUCT OWNER TO DEFINE: which stats feed each position's score and how they map to 0–100]`.

**6.3 Position weighting:** Positions are NOT weighted equally. After each player receives their 0–100 score, that score is multiplied by a position weight (so, e.g., a QB may count for more than a WR toward the final team total). The specific position weights are a placeholder — `[PRODUCT OWNER TO DEFINE: weight per position — QB, RB, WR, Defensive player]`.

**6.4 Team total:** Each team's weighted player scores are combined into a single backend number representing the team's total. The combination method (sum is the assumed default) is a placeholder if the product owner wants something other than a straight weighted sum — `[PRODUCT OWNER TO CONFIRM: combination method]`.

**6.5 Ranking:** All teams' backend numbers are compared and the teams are placed into an ordered ranked list (1st through last). This ranked list is the game result shown at the reveal.

**6.6 Scoring philosophy (design intent, for context):** The product owner intends for the scoring to be transparent enough to defend but to produce results that are debatable at the margins — close or surprising outcomes are a desired feature that drives post-game discussion, not a bug. Build the reveal to expose enough of the scoring breakdown that players can see and argue about why a team ranked where it did. Do not introduce randomness into scoring; the "debatability" should come from the principled weighting choices, not noise.

---

## 7. Technical Direction

- **Stack:** You may recommend and select the stack you believe is best suited for a real-time multiplayer web game with a data-ingestion pipeline and a scoring backend. Propose your recommendation (frontend, backend, real-time layer, database, hosting) with brief rationale before building.
- **Real-time multiplayer:** The draft is synchronous across 2–5 players, so real-time turn state, pick broadcasting, and the shared depleting pool must stay consistent across all clients.
- **Data pipeline:** Build the scraping/ingestion and the scouting-to-PFR join as a separate, re-runnable pipeline that produces a clean, queryable player dataset. The game reads from this dataset; it should not scrape live during gameplay.
- **Watchability:** The product owner intends to pair this game with a content/streaming launch, so the draft, the discussion phase, and especially the reveal should be visually clear and engaging on screen (readable for a streaming/recording audience). Factor this into the UI.

---

## 8. Open Questions to Resolve Before Building

Please get answers to these from the product owner rather than assuming:

1. **Pool composition:** How many prospects should be in the draftable pool per game (relative to 5 roster slots × up to 5 players = up to 25 picks)? How much "bench"/surplus beyond the minimum?
2. **Position-group interpretation:** Confirm the exact meaning of "position should be the same for all reports in a game" — does each roster slot draw from a single shared position group of reports, and how should the 2 WR slots be handled?
3. **Scoring placeholders:** All four placeholders in Section 6 (top-season weighting, stat-to-100 mapping per position, position weights, combination method).
4. **PFR fields:** Exactly which PFR stat fields have already been scraped and are available per position?
5. **Tie-breaking:** How should ties in the team ranking be resolved?
6. **Draft order:** How is the initial draft order (seed of the snake) determined — random, or some other method?
7. **Eligibility cutoffs:** Should there be a minimum career length / games played for a prospect to be eligible (to ensure every drafted player has enough PFR data to score fairly)? How are players with almost no NFL career handled?
8. **Discussion phase:** Is the discussion phase timed, free-form chat, structured, or voice (for the content pairing)?
9. **Single vs. multi-source per prospect:** When a prospect has reports from more than one of the three sources, should the game show one source's report, a chosen primary, or blend them?

---

## 9. Summary of What's Locked vs. Open

**Locked:**
- 2–5 players, snake draft, shared depleting pool
- Blind drafting on scouting reports only; name, college, year hidden
- Cross-year prospect pool
- Roster: 1 QB, 1 RB, 2 WR, 1 Defensive player
- Three scouting sources: WalterFootball, NFL.com (Zierlein), Tony Pauline
- Outcome data from PFR direct stats (NOT AV)
- Scoring: career averages with top season weighted up → 0–100 per player → position-weighted → summed to a backend team number → teams ranked into a list
- Discussion phase before reveal; rematch and shareable results
- Developer recommends the stack

**Open (do not assume — see Section 8):**
- All specific scoring weights and formulas (product owner will define)
- Pool size, position-group handling, eligibility cutoffs, tie-breaks, draft-order seeding, discussion-phase format, multi-source handling
