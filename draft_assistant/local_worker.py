"""Host-only entrypoint for an optional local Sleeper browser worker.

It intentionally has no remote auth flow.  A human-authenticated local browser
must expose a local CDP endpoint; this worker attaches, writes auditable local
runtime evidence, and disconnects when the session terminates.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

from .adapters.sleeper_browser import SleeperBrowserDraftRoom
from .adapters.sleeper_playwright import (
    SleeperBrowserTarget,
    connect_local_cdp,
    enter_live_draftroom_local_cdp,
)
from .autonomy import AutonomousDraftGuard
from .board import load_default_board
from .continuous_session import ContinuousDraftSession, PollingProfile, SessionTermination
from .live_driver import ClockFirstDraftDriver
from .persistent_runner import JsonlPickTelemetrySink, JsonlRunnerJournal, PersistentDraftRunner
from .policies.sleeper_special_teams import SleeperRankedSpecialTeamsPolicy
from .policies.source_board import SourceBoardPolicy
from .stress import MockTelemetry, read_pick_telemetry_jsonl, write_mock_telemetry


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Optional local CDP Sleeper draft worker")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9222")
    parser.add_argument("--league-id", required=True)
    parser.add_argument("--league-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--draft-slot", type=int, required=True)
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--mode", choices=("live", "attached-draft"), required=True)
    parser.add_argument("--max-cycles", type=int, help="Controlled rehearsal only; omit for a full session.")
    parser.add_argument(
        "--drafting-poll-ms",
        type=float,
        default=250.0,
        help="Poll interval while a draft clock is live (default: 250 ms).",
    )
    parser.add_argument(
        "--waiting-poll-ms",
        type=float,
        default=1_000.0,
        help="Poll interval before Sleeper opens a clock (default: 1000 ms; lower only for controlled mocks).",
    )
    args = parser.parse_args()

    target = SleeperBrowserTarget(
        league_id=args.league_id,
        league_name=args.league_name,
        username=args.username,
        draft_slot=args.draft_slot,
    )
    board = load_default_board()
    run_directory = args.runtime_dir / _utc_now().strftime("%Y%m%dT%H%M%SZ")
    run_directory.mkdir(parents=True, exist_ok=False)

    browser_session = (
        enter_live_draftroom_local_cdp(args.cdp_endpoint, board, target)
        if args.mode == "live"
        else connect_local_cdp(args.cdp_endpoint, board, target)
    )
    try:
        policy = SleeperRankedSpecialTeamsPolicy(SourceBoardPolicy())
        driver = ClockFirstDraftDriver(
            board,
            policy,
            AutonomousDraftGuard(target.league_id, target.username),
        )
        runner = PersistentDraftRunner(
            board,
            policy,
            driver,
            telemetry_sink=JsonlPickTelemetrySink(run_directory / "picks.jsonl"),
            journal=JsonlRunnerJournal(run_directory / "polls.jsonl"),
        )
        room = SleeperBrowserDraftRoom(browser_session.transport)
        report = ContinuousDraftSession(
            runner,
            expected_league_id=target.league_id,
            expected_username=target.username,
            profile=PollingProfile(
                drafting_interval_seconds=args.drafting_poll_ms / 1_000,
                waiting_interval_seconds=args.waiting_poll_ms / 1_000,
            ),
        ).run(room, max_cycles=args.max_cycles)
        _write_run_artifacts(run_directory, report, browser_session.transport.page.url)
    finally:
        browser_session.close()

    print(json.dumps(asdict(report), default=str, indent=2))
    return 0 if report.termination in {SessionTermination.DRAFT_COMPLETE, SessionTermination.CYCLE_LIMIT} else 2


def _write_run_artifacts(run_directory: Path, report, page_url: str) -> None:
    """Finish a local evidence bundle without exposing session/browser secrets."""

    (run_directory / "session.json").write_text(
        json.dumps(asdict(report), default=str, indent=2) + "\n", encoding="utf-8"
    )
    picks = read_pick_telemetry_jsonl(run_directory / "picks.jsonl")
    mock_id = page_url.rstrip("/").rsplit("/", maxsplit=1)[-1] or "sleeper-session"
    result = "completed" if report.termination == SessionTermination.DRAFT_COMPLETE else "terminated"
    write_mock_telemetry(
        run_directory / "mock.json",
        MockTelemetry(
            mock_id=mock_id,
            started_at=report.started_at,
            completed_at=report.completed_at,
            picks=picks,
            result=result,
            notes=(f"session_termination={report.termination}", report.reason),
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
