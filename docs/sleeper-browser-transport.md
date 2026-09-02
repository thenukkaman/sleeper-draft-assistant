# Sleeper browser transport contract

`SleeperBrowserDraftRoom` is the only repository boundary permitted to request
a pick in Sleeper. A host-specific browser worker implements
`SleeperDomTransport`; neither the policy nor the persistent runner contains
selectors, screenshots, cookies, authentication, or browser-session state.

## One observation

`observe_visible_draft_room()` must produce one coherent `BrowserObservation`
from structured DOM/accessibility data and timestamp the completed read. It
must include:

- the validated run's league/account identity and Sleeper draft status;
- the live chronological pick label and its absolute pick number;
- the next `kenikh` pick, roster positions, drafted source-board players, and
  the currently visible available player rows;
- the countdown as milliseconds remaining, retaining Sleeper's display
  precision (currently one second); and
- the auto-pick state plus a `DRAFT`/`QUEUE`/`DETAILS` action set for every
  visible player row.

For the observed Sleeper board, `2.08` is chronological Round-2 pick eight
(absolute 20), regardless of its reversed visual column. The transport may
not infer state from whether a board cell is above or below the viewport.

## Actions

`click_exact_draft(name)` must resolve the current row for the supplied,
normalized player name and invoke its semantic `.draft-button` control. It
must not click the player card, the purple queue control, a stale coordinate,
or a row that was merely visible during a previous poll.

`set_auto_pick_off()` is one targeted action against Sleeper's visible
**TURN OFF AUTO-PICK** control. The transport has no operation that enables
auto-pick. It must immediately re-observe after the click so the runner can
record whether the same pick is still salvageable.

Any dialog, overlay, spinner, empty player list, unknown action, or DOM read
failure becomes a structured `BrowserBlocker`; it is never worked around with
coordinates or a nearby visible control.

## Timing

The worker calls `PersistentDraftRunner.poll_once` on a fixed cadence outside
the runner. For every call, it records poll start, completed observation, and
the prior completed poll. The runner records recommendation, selection,
publication, and auto-pick events. This creates a bounded detection interval
for a new clock or auto-pick incident without claiming that a rounded visual
timer is a server timestamp.
