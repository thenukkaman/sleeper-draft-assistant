"""Thin command-line interface; it contains no draft-ranking logic."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .adapters.sleeper_readonly import SleeperReadOnlyClient
from .autonomy import AutonomousDraftGuard
from .board import load_default_board
from .models import DraftState, Recommendation
from .policies.sleeper_special_teams import SleeperRankedSpecialTeamsPolicy
from .policies.source_board import SourceBoardPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description="Source-board fantasy draft assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    recommend = commands.add_parser("recommend", help="Recommend from a local state JSON file")
    recommend.add_argument("--state", type=Path, required=True)
    recommend.add_argument("--json", action="store_true", dest="as_json")

    sleeper = commands.add_parser("sleeper", help="Read live Sleeper state, then recommend")
    sleeper.add_argument("--league-id", required=True)
    sleeper.add_argument("--username", required=True)
    sleeper.add_argument("--json", action="store_true", dest="as_json")
    sleeper.add_argument("--autonomous-check", action="store_true")

    args = parser.parse_args()
    board = load_default_board()
    policy = SleeperRankedSpecialTeamsPolicy(SourceBoardPolicy())
    if args.command == "recommend":
        state = DraftState.from_mapping(json.loads(args.state.read_text(encoding="utf-8")))
        recommendation = policy.recommend(board, state)
        _emit(recommendation, args.as_json)
        return 0

    snapshot = SleeperReadOnlyClient().snapshot_for_user(args.league_id, args.username)
    recommendation = policy.recommend(board, snapshot.state)
    if args.autonomous_check:
        gate = AutonomousDraftGuard(args.league_id, args.username).evaluate(snapshot.state, recommendation)
        _emit(recommendation, args.as_json, gate_allowed=gate.allowed, gate_reasons=gate.reasons)
    else:
        _emit(recommendation, args.as_json)
    return 0


def _emit(
    recommendation: Recommendation,
    as_json: bool,
    gate_allowed: bool | None = None,
    gate_reasons: tuple[str, ...] = (),
) -> None:
    if as_json:
        payload = asdict(recommendation)
        if gate_allowed is not None:
            payload["autonomous_gate"] = {"allowed": gate_allowed, "reasons": gate_reasons}
        print(json.dumps(payload, default=str, indent=2))
        return
    print(recommendation.directive)
    for index, candidate in enumerate(recommendation.candidates, start=1):
        print(f"{index}. {candidate.player.name} ({candidate.player.position}{candidate.player.rank}, {candidate.player.tag or 'UNTAGGED'})")
        print(f"   {candidate.reason}")
    for warning in recommendation.warnings:
        print(f"WARNING: {warning}")
    if gate_allowed is not None:
        print("AUTONOMOUS GATE: " + ("PASS" if gate_allowed else "BLOCKED"))
        for reason in gate_reasons:
            print(f"  - {reason}")


if __name__ == "__main__":
    raise SystemExit(main())
