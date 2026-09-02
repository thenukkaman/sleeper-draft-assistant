"""Persistent, browser-neutral orchestration for an unattended draft session.

This module does not know Sleeper selectors, screenshots, or credentials.  A
browser adapter supplies ``DraftRoom`` observations and exact player-row
actions; the runner retains only the state needed to make the next clock edge
fast and auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable, Protocol

from .board import Board, normalize_name
from .interfaces.browser_observation import BrowserObservation
from .live_driver import ClockFirstDraftDriver, DraftAttempt, DraftRoom
from .lookahead import LookaheadPlanner, PreparedPick
from .policies.base import DraftPolicy
from .stress import PickTelemetry, TimingEvidence


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PickTelemetrySink(Protocol):
    """Durable runtime adapters implement this one-way event boundary."""

    def record(self, telemetry: PickTelemetry) -> None: ...


@dataclass(frozen=True)
class RunnerSnapshot:
    """Compact durable state after a poll, with no session or account secrets."""

    observed_at: datetime
    completed_at: datetime
    pick_label: str
    current_pick_number: int
    our_pick_number: int
    draft_status: str
    auto_pick_enabled: bool
    prepared_player: str | None
    action_outcome: str | None = None
    action_reason: str = ""
    event: str = "poll_complete"


class RunnerJournal(Protocol):
    """Append-only persistence boundary for the runner's latest known state."""

    def record(self, snapshot: RunnerSnapshot) -> None: ...


class JsonlRunnerJournal:
    """Small local journal suitable for the ignored ``runtime/`` directory."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, snapshot: RunnerSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(asdict(snapshot), default=str, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(serialized + "\n")
            journal.flush()


class JsonlPickTelemetrySink:
    """Append-only per-pick telemetry for one local mock or live session."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, telemetry: PickTelemetry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(asdict(telemetry), default=str, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(serialized + "\n")
            journal.flush()


@dataclass(frozen=True)
class PollCycle:
    """Result of one scheduling tick; no state is inferred from a UI repaint."""

    observation: BrowserObservation
    prepared_pick: PreparedPick | None
    attempt: DraftAttempt | None
    telemetry: PickTelemetry | None
    snapshot: RunnerSnapshot


class PersistentDraftRunner:
    """Keep a prepared ladder warm and execute only a fresh, guarded clock edge.

    ``poll_once`` deliberately does not sleep. The concrete browser process
    schedules it at the chosen cadence, which keeps polling policy separate
    from drafting logic and makes timer/detection measurements testable.
    """

    def __init__(
        self,
        board: Board,
        policy: DraftPolicy,
        driver: ClockFirstDraftDriver,
        planner: LookaheadPlanner | None = None,
        telemetry_sink: PickTelemetrySink | None = None,
        journal: RunnerJournal | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.board = board
        self.policy = policy
        self.driver = driver
        self.planner = planner or LookaheadPlanner()
        self.telemetry_sink = telemetry_sink
        self.journal = journal
        self.now = now
        self.prepared_pick: PreparedPick | None = None
        self._last_poll_completed_at: datetime | None = None
        self._quarantined_pick_number: int | None = None

    def poll_once(self, room: DraftRoom) -> PollCycle:
        """Observe once, prepare ahead, or execute the already-ready decision."""

        poll_started_at = self.now()
        observation = room.observe()
        observed_at = self.now()
        previous_poll_completed_at = self._last_poll_completed_at

        if self._quarantined_pick_number != observation.current_pick_number:
            self._quarantined_pick_number = None
        if self._quarantined_pick_number == observation.current_pick_number:
            # A prior player action on this exact clock was unsuccessful or
            # ambiguous.  Keep emitting fresh state snapshots, but do not
            # generate a second player click.  When Sleeper advances, the
            # quarantine clears automatically and normal preplanning resumes.
            self._prepare_from(observation)
            completed_at = self.now()
            snapshot = self._snapshot(observation, observed_at, completed_at, None)
            self._record_snapshot(snapshot)
            self._last_poll_completed_at = completed_at
            return PollCycle(observation, self.prepared_pick, None, None, snapshot)

        if not self._requires_transaction(observation):
            self._prepare_from(observation)
            completed_at = self.now()
            snapshot = self._snapshot(observation, observed_at, completed_at, None)
            self._record_snapshot(snapshot)
            self._last_poll_completed_at = completed_at
            return PollCycle(observation, self.prepared_pick, None, None, snapshot)

        if (
            observation.draft_status == "drafting"
            and observation.current_pick_number == observation.our_pick_number
        ):
            # Flush the active-clock detection before any recommendation,
            # virtual-row lookup, or browser action can delay the completion
            # snapshot. This is the durable notification timestamp used to
            # measure detection latency independently from pick latency.
            self._record_snapshot(
                self._snapshot(
                    observation,
                    observed_at,
                    observed_at,
                    None,
                    event="live_clock_observed",
                )
            )

        events: dict[str, datetime] = {}

        def record_event(name: str, at: datetime) -> None:
            # Repeated events (for example a post-recovery recommendation) keep
            # the final time; it is the one that immediately precedes dispatch.
            events[name] = at

        try:
            attempt = self.driver.run_once(
                room,
                prepared=self.prepared_pick,
                initial_observation=observation,
                record_event=record_event,
            )
        except Exception as error:
            # Preserve the monitor loop when a browser action path fails.  The
            # fresh read is evidence only; it is never used to retry a player
            # action on this clock.
            try:
                final_observation = room.observe()
            except Exception:
                final_observation = observation
            recommendation = self.policy.recommend(self.board, final_observation.to_state())
            gate = self.driver.guard.evaluate(final_observation.to_state(), recommendation)
            attempt = DraftAttempt(
                False,
                False,
                None,
                recommendation,
                gate,
                f"Draft action path raised {type(error).__name__}; monitor this clock but do not retry a player action.",
                final_observation=final_observation,
                quarantined=True,
            )
        telemetry = self._to_telemetry(
            observation=observation,
            attempt=attempt,
            poll_started_at=poll_started_at,
            observed_at=observed_at,
            prior_poll_completed_at=previous_poll_completed_at,
            events=events,
            prepared_pick=self.prepared_pick,
        )
        if self.telemetry_sink is not None:
            self.telemetry_sink.record(telemetry)

        # Use the latest post-transaction observation to warm the next ladder
        # immediately; no future clock waits for the ordinary polling cadence.
        # This also prevents an auto-pick recovery or stale-clock branch from
        # persisting the pre-recovery observation as if it were current.
        final_observation = attempt.final_observation or observation
        if (
            observation.current_pick_number == observation.our_pick_number
            and observation.draft_status == "drafting"
            and not attempt.confirmed
        ):
            self._quarantined_pick_number = observation.current_pick_number
        self._prepare_from(final_observation)
        completed_at = self.now()
        snapshot = self._snapshot(
            final_observation,
            observed_at,
            completed_at,
            attempt,
            event="action_complete",
        )
        self._record_snapshot(snapshot)
        self._last_poll_completed_at = completed_at
        return PollCycle(observation, self.prepared_pick, attempt, telemetry, snapshot)

    @staticmethod
    def _requires_transaction(observation: BrowserObservation) -> bool:
        is_our_pick = (
            observation.current_pick_number is not None
            and observation.current_pick_number == observation.our_pick_number
        )
        # Auto-pick must be remediated even while an opponent is on the clock.
        return observation.blocker is not None or observation.auto_pick_enabled is True or is_our_pick

    def _prepare_from(self, observation: BrowserObservation | None) -> None:
        if observation is None or observation.blocker is not None:
            self.prepared_pick = None
            return
        self.prepared_pick = self.planner.prepare_next(self.board, self.policy, observation.to_state())

    def _snapshot(
        self,
        observation: BrowserObservation,
        observed_at: datetime,
        completed_at: datetime,
        attempt: DraftAttempt | None,
        event: str = "poll_complete",
    ) -> RunnerSnapshot:
        prepared_primary = self.prepared_pick.candidates[0].player.name if self.prepared_pick and self.prepared_pick.candidates else None
        return RunnerSnapshot(
            observed_at=observed_at,
            completed_at=completed_at,
            pick_label=observation.pick_label,
            current_pick_number=observation.current_pick_number,
            our_pick_number=observation.our_pick_number,
            draft_status=observation.draft_status,
            auto_pick_enabled=observation.auto_pick_enabled,
            prepared_player=prepared_primary,
            action_outcome=self._outcome(attempt) if attempt else None,
            action_reason=attempt.reason if attempt else "",
            event=event,
        )

    def _record_snapshot(self, snapshot: RunnerSnapshot) -> None:
        if self.journal is not None:
            self.journal.record(snapshot)

    @staticmethod
    def _selected_player(
        initial: BrowserObservation,
        attempt: DraftAttempt,
    ) -> str | None:
        if attempt.confirmed:
            return attempt.player_name
        if attempt.final_observation is None:
            return None
        before = {normalize_name(name): name for name in initial.drafted_players}
        after = {normalize_name(name): name for name in attempt.final_observation.drafted_players}
        new_names = tuple(name for normalized, name in after.items() if normalized not in before)
        return new_names[0] if len(new_names) == 1 else None

    @staticmethod
    def _outcome(attempt: DraftAttempt) -> str:
        if attempt.confirmed:
            return "confirmed"
        if attempt.quarantined:
            return "quarantined"
        if attempt.auto_pick_recovery and attempt.auto_pick_recovery.pick_was_missed:
            return "missed"
        if attempt.acted:
            return "unverified"
        return "blocked"

    def _to_telemetry(
        self,
        observation: BrowserObservation,
        attempt: DraftAttempt,
        poll_started_at: datetime,
        observed_at: datetime,
        prior_poll_completed_at: datetime | None,
        events: dict[str, datetime],
        prepared_pick: PreparedPick | None,
    ) -> PickTelemetry:
        recommendation = attempt.recommendation
        primary = recommendation.primary
        final_observation = attempt.final_observation
        recovery = attempt.auto_pick_recovery
        is_our_pick = observation.current_pick_number == observation.our_pick_number
        timing = TimingEvidence(
            poll_started_at=poll_started_at,
            prior_poll_completed_at=prior_poll_completed_at,
            observed_live_at=observed_at if is_our_pick else None,
            browser_clock_remaining_ms=observation.clock_remaining_ms,
            browser_clock_precision_ms=observation.clock_precision_ms,
            recommendation_ready_at=events.get("recommendation_ready"),
            auto_pick_detected_at=events.get("auto_pick_detected"),
            auto_pick_disable_requested_at=events.get("auto_pick_disable_requested"),
            auto_pick_disabled_at=events.get("auto_pick_disabled_observed"),
        )
        return PickTelemetry(
            pick_label=observation.pick_label,
            decision_started_at=observed_at,
            selection_requested_at=events.get("selection_requested"),
            confirmed_at=events.get("published_observation") if attempt.confirmed else None,
            expected_player=primary.player.name if primary else None,
            selected_player=self._selected_player(observation, attempt),
            current_pick_number=observation.current_pick_number,
            auto_pick_before=observation.auto_pick_enabled,
            auto_pick_after=(
                final_observation.auto_pick_enabled
                if final_observation is not None
                else False
                if recovery and recovery.recovered
                else observation.auto_pick_enabled
            ),
            outcome=self._outcome(attempt),
            auto_pick_recovery=(
                "missed_before_recovery"
                if recovery and recovery.pick_was_missed
                else "recovered"
                if recovery and recovery.recovered
                else "failed"
                if recovery and not recovery.recovered
                else "not_needed"
            ),
            reason=attempt.reason,
            observed_candidates=tuple(candidate.player.name for candidate in recommendation.candidates[:5]),
            prepared_player_before=(
                prepared_pick.candidates[0].player.name
                if prepared_pick is not None and prepared_pick.candidates
                else None
            ),
            timing=timing,
        )
