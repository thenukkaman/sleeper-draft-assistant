"""One uninterrupted, browser-neutral session around the clock-first runner.

This module owns cadence and stop policy; it owns neither Sleeper selectors nor
player rankings.  A host browser worker supplies a ``DraftRoom`` and runs one
``ContinuousDraftSession`` for the whole draft.  It is deliberately not a
collection of independent scheduled invocations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from time import sleep
from typing import Callable, Protocol

from .live_driver import DraftAttempt, DraftRoom
from .persistent_runner import PollCycle


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DraftPoller(Protocol):
    """The small persistent-runner surface required by the session loop."""

    def poll_once(self, room: DraftRoom) -> PollCycle: ...


class SessionTermination(StrEnum):
    """Why a continuous browser session stopped or deliberately yielded."""

    DRAFT_COMPLETE = "draft_complete"
    BROWSER_BLOCKER = "browser_blocker"
    IDENTITY_MISMATCH = "identity_mismatch"
    AUTO_PICK_RECOVERY_FAILED = "auto_pick_recovery_failed"
    UNVERIFIED_ACTION = "unverified_action"
    ACTION_BLOCKED = "action_blocked"
    EXTERNAL_STOP = "external_stop"
    CYCLE_LIMIT = "cycle_limit"
    TRANSPORT_ERROR = "transport_error"


@dataclass(frozen=True)
class PollingProfile:
    """Cadence values for a real host process, kept outside policy logic."""

    drafting_interval_seconds: float = 0.25
    waiting_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.drafting_interval_seconds <= 0 or self.waiting_interval_seconds <= 0:
            raise ValueError("Polling intervals must be positive.")

    def interval_after(self, cycle: PollCycle) -> float:
        return (
            self.drafting_interval_seconds
            if cycle.snapshot.draft_status == "drafting"
            else self.waiting_interval_seconds
        )


@dataclass(frozen=True)
class SessionReport:
    """Compact completion record; raw poll/action evidence remains in the journal."""

    started_at: datetime
    completed_at: datetime
    termination: SessionTermination
    cycles_completed: int
    reason: str
    last_pick_label: str | None = None


class ContinuousDraftSession:
    """Run the persistent engine until completion or a fail-closed condition.

    A confirmed action and a recovered-but-missed pick both continue to the
    next cycle.  A selector/modal/security failure never does.  This prevents
    the dangerous behavior of silently retrying a click, while avoiding a
    second failure caused by abandoning every later pick after one lost clock.
    """

    def __init__(
        self,
        runner: DraftPoller,
        expected_league_id: str,
        expected_username: str,
        profile: PollingProfile | None = None,
        pause: Callable[[float], None] = sleep,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.runner = runner
        self.expected_league_id = expected_league_id
        self.expected_username = expected_username
        self.profile = profile or PollingProfile()
        self.pause = pause
        self.now = now

    def run(
        self,
        room: DraftRoom,
        stop_requested: Callable[[], bool] = lambda: False,
        max_cycles: int | None = None,
    ) -> SessionReport:
        """Keep one host worker alive until an explicit stop condition occurs.

        ``max_cycles`` is solely a controlled-rehearsal harness.  A live host
        should omit it and terminate only through the browser's draft-complete
        state, external operator stop, or a fail-closed condition.
        """

        if max_cycles is not None and max_cycles < 1:
            raise ValueError("max_cycles must be at least one when provided.")
        started_at = self.now()
        cycles_completed = 0
        last_cycle: PollCycle | None = None

        while True:
            if stop_requested():
                return self._report(
                    started_at,
                    cycles_completed,
                    SessionTermination.EXTERNAL_STOP,
                    "An external stop was requested.",
                    last_cycle,
                )
            try:
                cycle = self.runner.poll_once(room)
            except Exception as error:  # Browser transport exceptions are fail-closed.
                return self._report(
                    started_at,
                    cycles_completed,
                    SessionTermination.TRANSPORT_ERROR,
                    f"Browser transport raised {type(error).__name__}: {error}",
                    last_cycle,
                )

            cycles_completed += 1
            last_cycle = cycle
            termination = self._termination_for(cycle)
            if termination is not None:
                return self._report(
                    started_at,
                    cycles_completed,
                    termination,
                    self._reason_for(cycle, termination),
                    cycle,
                )
            if max_cycles is not None and cycles_completed >= max_cycles:
                return self._report(
                    started_at,
                    cycles_completed,
                    SessionTermination.CYCLE_LIMIT,
                    "Controlled rehearsal cycle limit reached.",
                    cycle,
                )
            self.pause(self.profile.interval_after(cycle))

    def _termination_for(self, cycle: PollCycle) -> SessionTermination | None:
        observation = cycle.attempt.final_observation if cycle.attempt and cycle.attempt.final_observation else cycle.observation
        attempt = cycle.attempt

        if (
            observation.league_id != self.expected_league_id
            or observation.username.casefold() != self.expected_username.casefold()
        ):
            return SessionTermination.IDENTITY_MISMATCH
        if observation.blocker is not None:
            return SessionTermination.BROWSER_BLOCKER
        if observation.draft_status in {"complete", "completed"}:
            return SessionTermination.DRAFT_COMPLETE
        if attempt is None:
            return None

        recovery = attempt.auto_pick_recovery
        if recovery is not None and not recovery.recovered:
            return SessionTermination.AUTO_PICK_RECOVERY_FAILED
        if attempt.acted and not attempt.confirmed:
            return SessionTermination.UNVERIFIED_ACTION

        # An unsuccessful transaction is actionable only if the latest
        # observation still says our active drafting clock is live.  If it
        # advanced, the incident is captured and the next future pick remains
        # worth protecting.
        still_our_pick = observation.current_pick_number == observation.our_pick_number
        if not attempt.acted and observation.draft_status == "drafting" and still_our_pick:
            return SessionTermination.ACTION_BLOCKED
        return None

    @staticmethod
    def _reason_for(cycle: PollCycle, termination: SessionTermination) -> str:
        observation = cycle.attempt.final_observation if cycle.attempt and cycle.attempt.final_observation else cycle.observation
        attempt: DraftAttempt | None = cycle.attempt
        if termination == SessionTermination.IDENTITY_MISMATCH:
            return "Visible league/account did not match the approved Sleeper draft target."
        if termination == SessionTermination.BROWSER_BLOCKER:
            return observation.blocker.reason() if observation.blocker else "Browser blocker was not described."
        if termination == SessionTermination.DRAFT_COMPLETE:
            return "Sleeper reported the draft complete."
        if attempt is not None:
            return attempt.reason
        return "Session stopped without an action attempt."

    def _report(
        self,
        started_at: datetime,
        cycles_completed: int,
        termination: SessionTermination,
        reason: str,
        last_cycle: PollCycle | None,
    ) -> SessionReport:
        return SessionReport(
            started_at=started_at,
            completed_at=self.now(),
            termination=termination,
            cycles_completed=cycles_completed,
            reason=reason,
            last_pick_label=last_cycle.snapshot.pick_label if last_cycle is not None else None,
        )
