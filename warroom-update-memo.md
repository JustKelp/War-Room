# MEMO: Project Update — "WarRoom" (formerly the Blind Scouting Draft Game)

**To:** Claude Code
**From:** Product Owner (Xavier)
**Re:** Major design changes to the previously delivered developer proposal. Read this entire memo and the original proposal document before writing any code. Where this memo conflicts with the original proposal, this memo wins. **Do not make any assumptions beyond what is stated here. A list of open questions is at the end — ask them (and anything else that is ambiguous) before implementation.**

---

## 1. What Changed and Why

The original concept — players reading full written pre-draft scouting reports before each pick — has been rejected as too slow and too read-heavy for a wide audience. Players are here to play, not read paragraphs. The written scouting report is **removed entirely** from gameplay.

The evaluation skill (judging which prospect became the better pro, identity hidden) remains the core of the game. Only the *input format* changes.

## 2. The Draft Card (replaces the scouting report)

Each draftable prospect is presented as a compact **draft card** readable at a glance. The card shows:

- Physical measurements
- College conference
- College statistical averages

Identifying information remains hidden (the prospect's name and identity are not shown until the reveal). The college conference IS shown; this is a deliberate change — it rewards ball knowledge while letting casual fans pick off the stats alone.

All data needed for these cards is already scraped and available. Do not build new scrapers without asking first.

## 3. Game Modes

**Multiplayer (retained).** The game remains multiplayer. This is a deliberate differentiator from solo score-chasing games like 82-0.

**Daily mode (new).** A solo-playable daily mode is added with the following shape:

- Every player worldwide receives the **same card pool** for a given day (daily seed).
- Each player completes their own draft from that pool.
- The backend **compares all players' scores for that day** against each other.
- The player is presented with their **performance relative to everyone else who played that day** (not just a raw score).

The framing matters: the daily mode is asynchronous competition against today's player base, never presented as a solo "high score" exercise.

## 4. The Reveal

The reveal — flipping the cards to show who each drafted prospect actually was, and how their real career scored — is the core payoff moment of the game and should be treated as a first-class feature, not an afterthought screen.

## 5. Positioning Note (context, not a feature)

A viral game called 82-0 (NBA all-time draft) rewards *recall* — recognizing known players. WarRoom rewards *projection* — judging unknown prospects from raw inputs. Hidden identity and the reveal moment are what keep WarRoom from converging on that game. Any design decision that would surface prospect identities during the draft, or reduce the game to picking players you recognize, is wrong.

---

## Open Questions — Answer Required Before Implementation

**Roster & draft structure**

1. Does the original roster structure (1 QB, 1 RB, 2 WR, 1 defensive player) still apply? Does the same roster apply to the daily mode?
2. Is the snake draft format retained for multiplayer with the new cards? Is the pre-reveal discussion phase still in?
3. In daily mode, what is the draft structure — how many cards in the pool, how many picks, and is it a draft against bots/the pool itself or a free selection?

**The card**

4. Exactly which physical measurements appear on the card? (e.g., height, weight, 40 time, others?)
5. Which college statistical averages appear, and do they differ by position?
6. Is anything else on the card (e.g., draft year/era, a position label)? Is the prospect's position shown explicitly?

**Scoring & results**

7. Does the original scoring approach (real career stats from PFR, position-weighted 0–100, career averages weighted toward peak seasons, formula placeholders to be filled by the product owner) still stand unchanged?
8. For daily mode, what exact form does the relative-performance presentation take (percentile, rank, leaderboard, share string, some combination)?

**Scope & accounts**

9. Do both modes ship in v1, or does one launch first?
10. Does daily mode require accounts, or can anonymous players participate in the daily comparison? Are streaks/history in scope?
11. What era range of prospects is in the card pool, and is that constrained by what has already been scraped?
12. Is "WarRoom" the final name for branding/domain purposes, or a working title?

Do not proceed past architecture planning until these are answered.
