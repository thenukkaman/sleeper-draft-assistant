"""Structured telemetry and deterministic analysis for Sleeper mock-draft runs.

The browser runner owns timestamps and raw UI observations.  This module keeps
that test evidence independent from the runner so it can be analyzed locally
and preserved after the browser session ends.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable
import json


@dataclass(frozen=True)
class TimingEvidence:
    """Raw clock-edge evidence captured by the persistent browser runner.

    Sleeper exposes a rounded-down countdown, so ``estimated_pick_opened_at``
    is explicitly an estimate—not an invented server timestamp.  The paired
    clock precision and prior completed poll make the uncertainty auditable.
    """

    poll_started_at: datetime | None = None
    prior_poll_completed_at: datetime | None = None
    observed_live_at: datetime | None = None
    browser_clock_remaining_ms: int | None = None
    browser_clock_precision_ms: int | None = None
    pick_clock_duration_ms: int = 120_000
    recommendation_ready_at: datetime | None = None
    auto_pick_detected_at: datetime | None = None
    auto_pick_disable_requested_at: datetime | None = None
    auto_pick_disabled_at: datetime | None = None


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
    prepared_player_before: str | None = None
    timing: TimingEvidence = field(default_factory=TimingEvidence)

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

    @property
    def first_live_observed_at(self) -> datetime:
        """Use the precise browser timestamp when captured, else legacy data."""

        return self.timing.observed_live_at or self.decision_started_at

    @property
    def estimated_pick_opened_at(self) -> datetime | None:
        """Estimate the clock edge from the observed, rounded Sleeper timer."""

        observed_at = self.timing.observed_live_at
        remaining_ms = self.timing.browser_clock_remaining_ms
        if observed_at is None or remaining_ms is None:
            return None
        elapsed_ms = self.timing.pick_clock_duration_ms - remaining_ms
        if elapsed_ms < 0:
            return None
        return observed_at - timedelta(milliseconds=elapsed_ms)

    @staticmethod
    def _duration_ms(later: datetime | None, earlier: datetime | None) -> int | None:
        if later is None or earlier is None:
            return None
        return round((later - earlier).total_seconds() * 1_000)

    @property
    def clock_detection_latency_ms(self) -> int | None:
        return self._duration_ms(self.first_live_observed_at, self.estimated_pick_opened_at)

    @property
    def observation_latency_ms(self) -> int | None:
        return self._duration_ms(self.first_live_observed_at, self.timing.poll_started_at)

    @property
    def recommendation_latency_ms(self) -> int | None:
        return self._duration_ms(self.timing.recommendation_ready_at, self.first_live_observed_at)

    @property
    def dispatch_latency_ms(self) -> int | None:
        return self._duration_ms(self.selection_requested_at, self.timing.recommendation_ready_at)

    @property
    def confirmation_after_selection_ms(self) -> int | None:
        return self._duration_ms(self.confirmed_at, self.selection_requested_at)

    @property
    def clock_to_selection_ms(self) -> int | None:
        return self._duration_ms(self.selection_requested_at, self.estimated_pick_opened_at)

    @property
    def auto_pick_detection_upper_bound_ms(self) -> int | None:
        """Bound auto-pick detection by the last clear poll, never call it exact."""

        return self._duration_ms(self.timing.auto_pick_detected_at, self.timing.prior_poll_completed_at)

    @property
    def auto_pick_disable_latency_ms(self) -> int | None:
        return self._duration_ms(self.timing.auto_pick_disabled_at, self.timing.auto_pick_detected_at)

    @property
    def auto_pick_disable_request_latency_ms(self) -> int | None:
        return self._duration_ms(self.timing.auto_pick_disable_requested_at, self.timing.auto_pick_detected_at)

    @property
    def auto_pick_toggle_verification_latency_ms(self) -> int | None:
        return self._duration_ms(
            self.timing.auto_pick_disabled_at,
            self.timing.auto_pick_disable_requested_at,
        )

    @property
    def auto_pick_salvage_latency_ms(self) -> int | None:
        return self._duration_ms(self.selection_requested_at, self.timing.auto_pick_detected_at)


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
        picks=tuple(_read_pick_telemetry(pick) for pick in raw["picks"]),
        result=str(raw["result"]),
        notes=tuple(raw.get("notes", ())),
    )


def read_pick_telemetry_jsonl(path: Path) -> tuple[PickTelemetry, ...]:
    """Load one worker's append-only per-pick evidence without inventing rows."""

    if not path.exists():
        return ()
    return tuple(
        _read_pick_telemetry(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
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
        "prepared_ladder_fallbacks": sum(
            pick.prepared_player_before is not None
            and pick.selected_player is not None
            and pick.prepared_player_before.casefold() != pick.selected_player.casefold()
            for pick in picks
        ),
        "auto_pick_incidents": sum(
            pick.auto_pick_before is True or pick.auto_pick_after is True for pick in picks
        ),
        "auto_pick_recoveries": sum(pick.auto_pick_recovery == "recovered" for pick in picks),
        "auto_pick_recovery_failures": sum(
            pick.auto_pick_recovery in {"failed", "missed_before_recovery"} for pick in picks
        ),
        "auto_pick_incidents_missing_timing": sum(
            (pick.auto_pick_before is True or pick.auto_pick_after is True)
            and pick.timing.auto_pick_detected_at is None
            for pick in picks
        ),
        "selection_latency_ms": _latency_summary(selection_latencies),
        "confirmation_latency_ms": _latency_summary(confirmation_latencies),
        "clock_detection_latency_ms": _latency_summary(
            [pick.clock_detection_latency_ms for pick in picks if pick.clock_detection_latency_ms is not None]
        ),
        "observation_latency_ms": _latency_summary(
            [pick.observation_latency_ms for pick in picks if pick.observation_latency_ms is not None]
        ),
        "recommendation_latency_ms": _latency_summary(
            [pick.recommendation_latency_ms for pick in picks if pick.recommendation_latency_ms is not None]
        ),
        "dispatch_latency_ms": _latency_summary(
            [pick.dispatch_latency_ms for pick in picks if pick.dispatch_latency_ms is not None]
        ),
        "confirmation_after_selection_ms": _latency_summary(
            [pick.confirmation_after_selection_ms for pick in picks if pick.confirmation_after_selection_ms is not None]
        ),
        "clock_to_selection_ms": _latency_summary(
            [pick.clock_to_selection_ms for pick in picks if pick.clock_to_selection_ms is not None]
        ),
        "auto_pick_detection_upper_bound_ms": _latency_summary(
            [
                pick.auto_pick_detection_upper_bound_ms
                for pick in picks
                if pick.auto_pick_detection_upper_bound_ms is not None
            ]
        ),
        "auto_pick_disable_latency_ms": _latency_summary(
            [pick.auto_pick_disable_latency_ms for pick in picks if pick.auto_pick_disable_latency_ms is not None]
        ),
        "auto_pick_disable_request_latency_ms": _latency_summary(
            [
                pick.auto_pick_disable_request_latency_ms
                for pick in picks
                if pick.auto_pick_disable_request_latency_ms is not None
            ]
        ),
        "auto_pick_toggle_verification_latency_ms": _latency_summary(
            [
                pick.auto_pick_toggle_verification_latency_ms
                for pick in picks
                if pick.auto_pick_toggle_verification_latency_ms is not None
            ]
        ),
        "auto_pick_salvage_latency_ms": _latency_summary(
            [pick.auto_pick_salvage_latency_ms for pick in picks if pick.auto_pick_salvage_latency_ms is not None]
        ),
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
                "prepared_player_before": pick.prepared_player_before,
                "selected_player": pick.selected_player,
            }
            for run in runs
            for pick in run.picks
            if pick.outcome not in {"confirmed"}
        ],
    }


def _read_timing_evidence(raw: dict[str, Any]) -> TimingEvidence:
    """Read optional timing fields while preserving older telemetry files."""

    timestamps = {
        key: datetime.fromisoformat(raw[key]) if raw.get(key) else None
        for key in (
            "poll_started_at",
            "prior_poll_completed_at",
            "observed_live_at",
            "recommendation_ready_at",
            "auto_pick_detected_at",
            "auto_pick_disable_requested_at",
            "auto_pick_disabled_at",
        )
    }
    return TimingEvidence(
        **timestamps,
        browser_clock_remaining_ms=raw.get("browser_clock_remaining_ms"),
        browser_clock_precision_ms=raw.get("browser_clock_precision_ms"),
        pick_clock_duration_ms=int(raw.get("pick_clock_duration_ms", 120_000)),
    )


def _read_pick_telemetry(pick: dict[str, Any]) -> PickTelemetry:
    return PickTelemetry(
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
        prepared_player_before=pick.get("prepared_player_before"),
        timing=_read_timing_evidence(pick.get("timing", {})),
    )


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
