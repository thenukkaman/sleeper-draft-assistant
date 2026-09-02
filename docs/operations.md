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

## Required live-runner contract

Before a live browser executor is considered for an unattended draft, it must:

1. Run as a persistent process independent of conversational context.
2. Keep a compact, durable record of the latest observation and action result.
3. Obtain the active pick, timer, auto-pick state, available player row, and
   confirmation from structured DOM/accessibility data; viewport visibility is
   only a fallback.
4. Poll or subscribe quickly enough to make a decision and a verification well
   within the pick clock.
5. Stop, preserve state, and notify when it sees an unexpected dialog,
   auto-pick enabled, account/league mismatch, stale state, or an unverified
   click.
6. Run a fresh source-linked news check whenever a `CHECK_NEWS` player would
   otherwise become the recommendation.

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

## Stress-test acceptance criteria

Run 25 complete mocks before approving unattended execution. Capture a JSON
record for every one of the team's 18 picks, then calculate the report from
those records. The run is a pass only if:

- all 25 mocks complete;
- no team pick is missed;
- every submitted pick is confirmed as the expected player (or explicitly
  classified as a policy mismatch);
- no auto-pick state appears without a recorded detection and remediation;
- the measured selection and confirmation latencies are comfortably below the
  two-minute clock, including the slowest observed pick.

The post-mortem must list every exception with its raw reason, not merely an
aggregate count. A later approval threshold can be made stricter once enough
latency data exists.
