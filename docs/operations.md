# Operations and Safety

## Current capability

The repository can:

- load a versioned source board;
- generate a recommendation from a local JSON fixture or read-only Sleeper
  snapshot;
- enforce Superflex, 1.5 TEP, upside, AVOID, CHECK_NEWS, and final-two-round
  specialist rules; and
- test a single guarded action transaction against a fake room.

It cannot yet autonomously draft in Sleeper. There is deliberately no live
pick-submission implementation in this repository.

## Host-execution gate

The browser worker must run as one continuous local desktop task, not as a
series of disconnected scheduled invocations. On the Windows host, Sleeper
must remain in the active desktop session, the machine must stay unlocked and
online, and no competing computer-use task may control the same browser. A
schedule may wake or start the task, but it is not proof that the browser
control remains live for an 18-round draft.

Before authorizing live drafting, run a single continuous timing rehearsal on
the intended host through the full mock lifecycle and prove that the local
append-only journal receives every poll and action event. If the host loses
foreground control, browser access, network, or process continuity, the
runner must fail closed and the draft is a no-go for unattended execution.

Use `ContinuousDraftSession` as the process-level loop for that rehearsal; it
is not a replacement for the host browser transport. Its default 250-ms
active-draft cadence is a starting measurement profile, not an assumed
performance result. Capture the actual observation cost before changing it.

## Required live-runner contract

Before a live browser executor is considered for an unattended draft, it must:

1. Run as a persistent process independent of conversational context.
2. Keep a compact, append-only record of the latest observation and action
   result. `JsonlRunnerJournal` is the approved local mechanism; place it
   under ignored `runtime/` and never include cookies, browser profiles,
   account tokens, or screenshots in the journal.
3. Obtain the active pick, timer, auto-pick state, available player row, and
   confirmation from structured DOM/accessibility data; viewport visibility is
   only a fallback.
   The adapter must distinguish a row's **DRAFT** action from its player-card
   and queue actions. The purple paper icon is a Sleeper queue action, not a
   pick submission; neither it nor a player-card click may be treated as a
   draft.
4. Poll or subscribe quickly enough to make a decision and a verification well
   within the pick clock.
5. Stop, preserve state, and notify when it sees an unexpected dialog,
   auto-pick enabled, account/league mismatch, stale state, or an unverified
   click.
6. Run a fresh source-linked news check whenever a `CHECK_NEWS` player would
   otherwise become the recommendation.

## Browser-blocker protocol

Every browser observation must include either a clear page or a structured
`BrowserBlocker`. The executor must fail closed—without clicking a player,
retrying a previous click, or changing auto-pick—on any of the following:

- a known preflight surface (record the exact control and its surrounding
  state): mock-only `START DRAFT`, or live-league `DRAFTROOM`;
- an unexpected visible dialog or overlay;
- a request that remains a spinner beyond the executor's bounded read/action
  deadline; or
- an unresponsive DOM/accessibility transport.

The mock-start path is a separate preflight capability, not part of the pick
transaction. Its observed sequence is: league pre-draft page -> `MOCK DRAFTS`
-> `NEW MOCK DRAFT` -> the mock board's `START DRAFT` control. It may only
activate that exact control in a mock run; it must never use this path for the
scheduled live league draft. Unknown dialogs are never auto-accepted. After
the mock starts, the executor must read a fresh, clear browser state before
entering the clock-first pick flow.

The scheduled live-draft preflight is separately constrained: on the approved
league's pre-draft page, click the visible `DRAFTROOM` control to enter the
room. That is navigation, not permission to start a draft. The runner then
waits for Sleeper to report `drafting`, confirms the league/account and
auto-pick state, and only then permits the normal per-pick transaction. It
must never use mock `START DRAFT` in the live league.

The canonical scheduled-run launch target is
`https://sleeper.com/leagues/1314724839730204672/predraft`. It avoids the
base site's Scores landing page. If that direct route fails or redirects, use
the recovery route: open the `FANTASY` menu, select the visible league entry
for **North Redmond 40**, then validate league ID `1314724839730204672` and
account `kenikh` on the resulting pre-draft page before using `DRAFTROOM`.
The display name is a navigation aid only; the ID and account checks remain
the authorization boundary.

This distinction matters in the observed Sleeper mock-creation failure: the
`NEW MOCK DRAFT` control became a spinner and the page stayed on the lobby;
the runner never reached the mock board's `START DRAFT` control. Treating that
as a confirmation and clicking arbitrary controls would be unsafe and would
hide the actual request failure.

## Local workflow

```powershell
python -m unittest discover -s tests -v
python -m draft_assistant.cli recommend --state fixtures\opening_state.json
python -m draft_assistant.cli sleeper --league-id 1314724839730204672 --username kenikh --autonomous-check
```

The final command is read-only. Its autonomous gate should normally block
because a public API response cannot confirm the browser-only safeguards.

## Secrets and runtime records

Do not commit credentials, cookies, browser profiles, or draft transcripts
that identify accounts. Put any future local runtime state under `runtime/`,
which is ignored by Git.

## Timing rehearsal before the 25-mock stress test

Do not begin the 25-mock stress test until a persistent browser executor has
completed a timing rehearsal. The rehearsal must use structured DOM or
accessibility state, not a screenshot or viewport position, and record a raw
timestamp for each of these events:

1. poll start and prior completed poll;
2. first observation that the current pick belongs to `kenikh`, plus Sleeper's
   displayed countdown and its rounding precision;
3. recommendation ready, exact `DRAFT` request, and published confirmation;
4. auto-pick first seen, targeted toggle request, and verified toggle-off;
   and
5. the exact player, pick label, candidates, browser state, and outcome.

For a prepared-ladder path, preserve the prepared primary and the final
selected player separately. A changed pair is expected only when the commit
observation proves the prepared primary became unavailable; it is evidence of
successful real-time contingency handling, not a policy mismatch.

The countdown produces an *estimated* pick-open timestamp: it is derived from
the observed remaining time and explicitly retains its one-second display
precision. Auto-pick detection is likewise an upper bound from the last clear
poll, never a claimed exact onset time.

The rehearsal passes only when the instrumentation can produce these fields
for each exercised path: normal pick, rapid opponent advance, an unavailable
prepared candidate, browser blocker, and auto-pick recovery. Establish the
observed baseline first; do not set a performance pass/fail threshold from a
chat-driven manual loop.

## Stress-test acceptance criteria

After the timing rehearsal passes, run 25 complete mocks. Capture a JSON
record for every one of the team's 18 picks, then calculate the report from
those records. The run is a pass only if:

- all 25 mocks complete;
- no team pick is missed;
- every submitted pick is confirmed as the expected player (or explicitly
  classified as a policy mismatch);
- no auto-pick state appears without a recorded detection and remediation;
- the measured clock-detection, observation, recommendation, dispatch,
  selection-to-confirmation, and total clock-to-selection latencies are
  comfortably below the two-minute clock, including the slowest observed
  pick; and
- every auto-pick incident has a bounded detection interval and measured
  disable/recovery timing, rather than only a narrative note.

The post-mortem must list every exception with its raw reason, not merely an
aggregate count. A later approval threshold can be made stricter once enough
latency data exists.

## Fast-mock profile

The CPU mock is the latency worst case, not a simulation of the live room:
opponents can resolve almost immediately, while live human turns may last one
second to two minutes. Every action cycle must therefore re-observe the draft,
derive availability, submit through the semantic DRAFT control, and verify the
published pick fast enough for the mock. That same ceiling covers live-human
timing without assuming that opponents will consume their full clocks.

## Auto-pick incident response

Auto-pick is a high-priority recovery path, not a terminal no-op. On every
poll, the runner must verify that it is off. If it is on, the executor must
disable the exact Sleeper toggle, re-observe the state, and only resume the
selection flow if the same live pick remains. If the clock advanced during that
operation, the pick is already lost: record the player, timestamps, before and
after auto-pick states, and reason; do not issue a duplicate selection. A
failed toggle or unknown state is fail-closed and must immediately be surfaced
as a runner failure.
