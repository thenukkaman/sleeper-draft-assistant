# Kenikh Sleeper Draft Assistant

Local Python project for an auditable, source-board-driven Sleeper draft
assistant for the 2026 12-team Superflex PPR, 1.5-TEP league. It does not use
Sleeper ADP, projections, or platform recommendations to choose a QB/RB/WR/TE.

> Status: decision-engine prototype. The package has no production Sleeper
> pick-submission code. The live mock established that an unattended drafter
> must be a persistent browser worker, not a chat-driven loop.

## Repository map

```text
draft_assistant/          Installable Python package
  board.json              Annual player data and draft-sheet annotations
  policies/               Swappable recommendation strategies
  interfaces/             Neutral browser/news/draftboard contracts
  adapters/               External-service translation (read-only today)
tests/                    Fast deterministic unit tests
fixtures/                 Reproducible local draft states
docs/architecture.md      Component boundaries and dependency direction
docs/operations.md        Safety rules and live-runner acceptance criteria
```

## Quick start

```powershell
python -m unittest discover -s tests -v
python -m draft_assistant.cli recommend --state fixtures\opening_state.json
```

See [architecture](docs/architecture.md) for the component model and
[operations](docs/operations.md) for the live-execution boundary.

## Architecture

```text
board.json                 Approved player ranks, tags, rookie overlays, roster rules, pick script
policies/source_board.py   Replaceable decision policy: returns ranked candidate choices
models.py                  Stable data types: player/tag/news-review/draft-state contracts
adapters/sleeper_readonly  Read-only live-state adapter; it never submits a selection
autonomy.py                Fail-closed gate before any browser executor takes action
interfaces/browser_observation.py  Normalizes the visible Sleeper draft-room state for the engine
interfaces/live_news.py            Pluggable real-time research contract for CHECK_NEWS players
cli.py                     Thin presentation layer for local JSON state or Sleeper state
```

The policy has no network or browser imports. A future policy can replace `SourceBoardPolicy` without changing the data adapter or the interface. The browser side supplies only a `BrowserObservation`; it cannot change how players are ranked.

## Local test

From this folder:

```powershell
python -m unittest discover -s tests -v
python -m draft_assistant.cli recommend --state fixtures\opening_state.json
```

The opening fixture should produce Josh Allen, Lamar Jackson, and Drake Maye in that order.

## Read-only Sleeper state

```powershell
python -m draft_assistant.cli sleeper --league-id 1314724839730204672 --username kenikh --autonomous-check
```

Sleeper's public API is read-only. The adapter matches either Sleeper's `username` or its displayed account name, because this league exposes `kenikh` as the latter. The browser executor is intentionally outside this codebase and must pass the autonomous gate immediately before it searches and clicks a player in Sleeper.

## Autonomous-action contract

The browser executor may act only when all of these are true:

1. League ID is `1314724839730204672` and username is `kenikh`.
2. Sleeper reports `drafting`, auto-pick is visibly off, and the live pick number is exactly the next `kenikh` pick.
3. The policy has returned one named player; that exact normalized name is still available in the Sleeper UI.
4. An `AVOID` player is marked by the policy as a genuine falling-price value; it is never an automatic ban or an automatic pick.
5. A `CHECK_NEWS` player has a fresh (15 minutes or less), source-linked, `CLEAR` live-news review. A missing, stale, or blocked review fails closed.
6. In Round 17, the selected DST is present in the currently visible Sleeper-ranked DST list; in Round 18, the same is true for the kicker list.

The source board governs all QB/RB/WR/TE choices. Per the approved exception, a separate plug-in uses the visible Sleeper rank only for DST in Round 17 and K in Round 18. No K/DST is selected earlier. A read-only API snapshot can never pass the final autonomous gate by itself: the browser adapter must observe that auto-pick is off.

## Annual refresh boundary

Player-specific material belongs in `board.json`: ranks, labels, written rationale, round instructions, and modest research overlays such as the RSP adjustments. Draft-format and execution behavior belongs in policy and interface modules. For a new season, replace the board data and revise only the format rules that actually changed; keep the state parser, clock-first executor, and autonomous safety gate intact.

`CHECK_NEWS` is similarly separated: a live executor implements `LiveNewsResolver`, attaches a source URL, timestamp, disposition, and concise note to the observed state, and the policy merely admits or excludes the player. The resolver must search current reporting every time the player would otherwise become the recommended pick; it cannot reuse a pre-draft opinion.
