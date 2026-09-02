# Architecture

## Purpose

This package produces an auditable, source-board recommendation for a Sleeper
fantasy-football draft. It is designed so player opinions can be refreshed for
a new season without reworking the decision engine or browser layer.

## Dependency direction

```text
board.json -> board.py -> policies/ -> Recommendation
                                     |
Browser/API adapter -> BrowserObservation -> PersistentDraftRunner -> ClockFirstDraftDriver
                                     |                    |                       |
                                     +----------------> DraftState <---------- autonomy.py
```

The arrows are intentional:

- `board.json` owns player-specific ranks, tags, explanations, and research
  overlays.
- `policies/` owns draft strategy. A policy receives a board and a neutral
  `DraftState`; it has no network or browser-control dependency.
- `interfaces/` define neutral contracts for browser observations, draftboard
  parsing, and live-news resolution.
- `adapters/` translate an outside service into those contracts. The current
  Sleeper adapter is public-API read-only by design.
- `autonomy.py` is a fail-closed action gate. It may approve an executor's
  proposed selection, but it never controls a browser itself.
- `persistent_runner.py` retains only the latest completed poll and prepared
  candidate ladder. It accepts browser observations and emits timing evidence;
  it does not implement selectors, credentials, sleeps, or a scheduler.

## Execution boundary

The persistent runner's concrete browser process calls `poll_once` on its
chosen cadence. It prepares a ladder after every opponent observation and
hands the observed live clock directly to `ClockFirstDraftDriver`, avoiding a
redundant initial browser read. The driver then performs the required
transaction:

1. Observe the draft room.
2. Form a recommendation.
3. Re-observe, re-filter the prepared ladder against the latest availability,
   and validate that the precise pick is still live.
4. Submit exactly one named player action.
5. Re-observe and verify Sleeper recorded that player and advanced the clock.

An ambiguous click is never retried automatically. A live runner must retain
its own durable state and use structured DOM/accessibility data rather than
screen position as its primary signal. The mock-draft test showed why this is a
non-negotiable production constraint.

Every transaction emits raw timing events: poll start, first live observation,
recommendation ready, selection request, publication observation, and—in the
auto-pick path—detection, toggle request, and confirmed toggle-off. The
telemetry layer derives its clock-edge estimate from Sleeper's displayed
countdown and preserves its precision rather than treating it as a server-side
timestamp.

Mock setup is intentionally outside that transaction. A mock-only preflight
adapter may navigate from the league pre-draft page through `MOCK DRAFTS` and
`NEW MOCK DRAFT` to the mock board's exact `START DRAFT` control. It must
produce a fresh browser observation after that transition; the live runner
never treats a preflight control as a player selection.

The live preflight is intentionally different: the approved league pre-draft
page exposes `DRAFTROOM`. The runner may use that control only to enter the
scheduled league room, then waits for Sleeper's active-draft state. It has no
authority to start the league draft through a mock control.

## Lookahead boundary

After every opponent pick, the runner prepares a complete candidate ladder for
the next `kenikh` pick using the policy and the latest known roster. At the
clock it may discard players opponents selected and use that ladder only when
the target pick label, roster, and known drafted set still match. The final
browser observation, autonomous gate, exact `DRAFT` control, and Sleeper
publication check remain mandatory; lookahead reduces deliberation latency but
never bypasses an execution safeguard.

The action transaction filters that ladder again at commit time. If its
prepared primary disappeared between the prior opponent observation and the
final pre-click observation, it promotes the next eligible candidate and
records both the prepared primary and selected player in telemetry. It does
not click the stale primary or stop at a preventable stale-state block.

## Annual refresh

For a new season, start by replacing `draft_assistant/board.json`. Update
format-specific policy rules only when the league format changes. The stable
models, data adapters, action gate, and eventual browser executor should not
need player-by-player edits.
