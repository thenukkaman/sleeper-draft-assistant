"""Structured telemetry and deterministic analysis for Sleeper mock-draft runs.

The browser runner owns timestamps and raw UI observations.  This module keeps
that test evidence independent from the runner so it can be analyzed locally
and preserved after the browser session ends.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
import json


@dataclass(frozen=True)
class PickTelemetry:
    """One attempted selection, measured from our active-pick observation."""

    pick_label: str
    decision_started_at: datetime
    selection_requested_at: datetime | None
    confirmed_at: datetime | None
    expected_player: str | None
    selected_player: str | None
    current_pick_number: int
    auto_pick_before: bool | None
    auto_pick_after: bool | None
    outcome: str
    auto_pick_recovery: str = "not_needed"
    reason: str = ""
    observed_candidates: tuple[str, ...] = ()

    @property
    def selection_latency_ms(self) -> int | None:
        if self.selection_requested_at is None:
            return None
        return round((self.selection_requested_at - self.decision_started_at).total_seconds() * 1_000)

    @property
    def confirmation_latency_ms(self) -> int | None:
        if self.confirmed_at is None:
            return None
        return round((self.confirmed_at - self.decision_started_at).total_seconds() * 1_000)


@dataclass(frozen=True)
class MockTelemetry:
    """One complete Sleeper mock. ``result`` must be set only after it ends."""

    mock_id: str
    started_at: datetime
    completed_at: datetime | None
    picks: tuple[PickTelemetry, ...]
    result: str
    notes: tuple[str, ...] = ()

    @property
    def missed_picks(self) -> tuple[PickTelemetry, ...]:
        return tuple(pick for pick in self.picks if pick.outcome == "missed")

    @property
    def unverified_actions(self) -> tuple[PickTelemetry, ...]:
        return tuple(pick for pick in self.picks if pick.outcome == "unverified")

    @property
    def policy_mismatches(self) -> tuple[PickTelemetry, ...]:
        return tuple(pick for pick in self.picks if pick.outcome == "policy_mismatch")


def write_mock_telemetry(path: Path, telemetry: MockTelemetry) -> None:
    """Persist a mock result as portable JSON without mutating its content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(telemetry), default=str, indent=2) + "\n", encoding="utf-8")


def read_mock_telemetry(path: Path) -> MockTelemetry:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return MockTelemetry(
        mock_id=str(raw["mock_id"]),
        started_at=datetime.fromisoformat(raw["started_at"]),
        completed_at=datetime.fromisoformat(raw["completed_at"]) if raw.get("completed_at") else None,
        picks=tuple(
            PickTelemetry(
                pick_label=str(pick["pick_label"]),
                decision_started_at=datetime.fromisoformat(pick["decision_started_at"]),
                selection_requested_at=(
                    datetime.fromisoformat(pick["selection_requested_at"])
                    if pick.get("selection_requested_at")
                    else None
                ),
                confirmed_at=datetime.fromisoformat(pick["confirmed_at"]) if pick.get("confirmed_at") else None,
                expected_player=pick.get("expected_player"),
                selected_player=pick.get("selected_player"),
                current_pick_number=int(pick["current_pick_number"]),
                auto_pick_before=pick.get("auto_pick_before"),
                auto_pick_after=pick.get("auto_pick_after"),
                auto_pick_recovery=str(pick.get("auto_pick_recovery", "not_recorded")),
                outcome=str(pick["outcome"]),
                reason=str(pick.get("reason", "")),
                observed_candidates=tuple(pick.get("observed_candidates", ())),
            )
            for pick in raw["picks"]
        ),
        result=str(raw["result"]),
        notes=tuple(raw.get("notes", ())),
    )


def summarize(mocks: Iterable[MockTelemetry]) -> dict[str, Any]:
    """Return post-mortem metrics only from captured per-pick evidence."""

    runs = tuple(mocks)
    picks = tuple(pick for run in runs for pick in run.picks)
    selection_latencies = [pick.selection_latency_ms for pick in picks if pick.selection_latency_ms is not None]
    confirmation_latencies = [pick.confirmation_latency_ms for pick in picks if pick.confirmation_latency_ms is not None]
    outcomes = Counter(pick.outcome for pick in picks)
    return {
        "mocks_completed": sum(run.result == "completed" for run in runs),
        "mocks_total": len(runs),
        "picks_observed": len(picks),
        "picks_confirmed": outcomes["confirmed"],
        "picks_missed": outcomes["missed"],
        "unverified_actions": outcomes["unverified"],
        "policy_mismatches": outcomes["policy_mismatch"],
        "auto_pick_incidents": sum(
            pick.auto_pick_before is True or pick.auto_pick_after is True for pick in picks
        ),
        "auto_pick_recoveries": sum(pick.auto_pick_recovery == "recovered" for pick in picks),
        "auto_pick_recovery_failures": sum(
            pick.auto_pick_recovery in {"failed", "missed_before_recovery"} for pick in picks
        ),
        "selection_latency_ms": _latency_summary(selection_latencies),
        "confirmation_latency_ms": _latency_summary(confirmation_latencies),
        "outcomes": dict(sorted(outcomes.items())),
        "failures": [
            {
                "mock_id": run.mock_id,
                "pick_label": pick.pick_label,
                "outcome": pick.outcome,
                "reason": pick.reason,
                "auto_pick_before": pick.auto_pick_before,
                "auto_pick_after": pick.auto_pick_after,
                "auto_pick_recovery": pick.auto_pick_recovery,
            }
            for run in runs
            for pick in run.picks
            if pick.outcome not in {"confirmed"}
        ],
    }


def _latency_summary(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "mean": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = max(0, round((len(ordered) - 1) * 0.95))
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": median(ordered),
        "mean": round(mean(ordered), 1),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }
