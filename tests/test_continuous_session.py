from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from draft_assistant.continuous_session import ContinuousDraftSession, SessionTermination
from draft_assistant.interfaces.browser_observation import (
    BrowserBlocker,
    BrowserBlockerKind,
    BrowserObservation,
)


@dataclass
class _Cycle:
    observation: BrowserObservation
    snapshot: object
    attempt: object | None = None


class _Poller:
    def __init__(self, cycles: list[_Cycle]) -> None:
        self.cycles = cycles
        self.calls = 0

    def poll_once(self, room: object) -> _Cycle:
        self.calls += 1
        return self.cycles.pop(0)


class ContinuousDraftSessionTests(unittest.TestCase):
    @staticmethod
    def _observation(**changes) -> BrowserObservation:
        values = dict(
            league_id="league-1",
            username="kenikh",
            draft_status="drafting",
            auto_pick_enabled=False,
            round_number=1,
            pick_label="1.05",
            current_pick_number=5,
            our_pick_number=5,
            roster_positions=(),
            drafted_players=frozenset(),
            available_players=frozenset({"Josh Allen"}),
        )
        values.update(changes)
        return BrowserObservation(**values)

    @staticmethod
    def _snapshot(observation: BrowserObservation) -> object:
        return SimpleNamespace(draft_status=observation.draft_status, pick_label=observation.pick_label)

    def _session(self, poller: _Poller, pauses: list[float]) -> ContinuousDraftSession:
        return ContinuousDraftSession(
            poller,
            expected_league_id="league-1",
            expected_username="kenikh",
            pause=pauses.append,
            now=lambda: datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc),
        )

    def test_stops_immediately_on_browser_blocker(self) -> None:
        blocked = self._observation(
            blocker=BrowserBlocker(BrowserBlockerKind.UNEXPECTED_DIALOG, "Confirmation dialog was visible."),
        )
        poller = _Poller([_Cycle(blocked, self._snapshot(blocked))])
        pauses: list[float] = []

        report = self._session(poller, pauses).run(object())

        self.assertEqual(report.termination, SessionTermination.BROWSER_BLOCKER)
        self.assertEqual(report.cycles_completed, 1)
        self.assertEqual(pauses, [])

    def test_continues_after_a_recorded_lost_pick_then_stops_at_draft_completion(self) -> None:
        before = self._observation(auto_pick_enabled=True)
        after_miss = self._observation(
            current_pick_number=6,
            our_pick_number=20,
            pick_label="1.06",
            auto_pick_enabled=False,
            drafted_players=frozenset({"Lamar Jackson"}),
        )
        missed_attempt = SimpleNamespace(
            acted=False,
            confirmed=False,
            reason="Auto-pick was disabled, but Sleeper had already advanced the clock.",
            auto_pick_recovery=SimpleNamespace(recovered=True, pick_was_missed=True),
            final_observation=after_miss,
        )
        complete = self._observation(
            draft_status="completed",
            current_pick_number=216,
            our_pick_number=216,
            pick_label="18.12",
        )
        poller = _Poller(
            [
                _Cycle(before, self._snapshot(after_miss), missed_attempt),
                _Cycle(complete, self._snapshot(complete)),
            ]
        )
        pauses: list[float] = []

        report = self._session(poller, pauses).run(object())

        self.assertEqual(report.termination, SessionTermination.DRAFT_COMPLETE)
        self.assertEqual(report.cycles_completed, 2)
        self.assertEqual(pauses, [0.25])

    def test_continues_observing_after_an_unverified_player_action(self) -> None:
        observation = self._observation()
        unverified = SimpleNamespace(
            acted=True,
            confirmed=False,
            reason="Sleeper did not publish the named player; do not retry automatically.",
            auto_pick_recovery=None,
            final_observation=observation,
        )
        complete = self._observation(
            draft_status="completed",
            current_pick_number=216,
            our_pick_number=216,
            pick_label="18.12",
        )
        poller = _Poller(
            [
                _Cycle(observation, self._snapshot(observation), unverified),
                _Cycle(complete, self._snapshot(complete)),
            ]
        )
        pauses: list[float] = []

        report = self._session(poller, pauses).run(object())

        self.assertEqual(report.termination, SessionTermination.DRAFT_COMPLETE)
        self.assertEqual(report.cycles_completed, 2)
        self.assertEqual(pauses, [0.25])

    def test_stops_after_failed_auto_pick_remediation(self) -> None:
        observation = self._observation(auto_pick_enabled=True)
        failed_recovery = SimpleNamespace(
            acted=False,
            confirmed=False,
            reason="Auto-pick remained enabled after the targeted disable action.",
            auto_pick_recovery=SimpleNamespace(recovered=False, pick_was_missed=False),
            final_observation=observation,
        )
        poller = _Poller([_Cycle(observation, self._snapshot(observation), failed_recovery)])
        pauses: list[float] = []

        report = self._session(poller, pauses).run(object())

        self.assertEqual(report.termination, SessionTermination.AUTO_PICK_RECOVERY_FAILED)
        self.assertEqual(pauses, [])

    def test_continues_observing_when_a_live_clock_has_no_safe_action(self) -> None:
        observation = self._observation()
        blocked = SimpleNamespace(
            acted=False,
            confirmed=False,
            reason="Sleeper did not expose an exact semantic DRAFT control.",
            auto_pick_recovery=None,
            final_observation=observation,
        )
        complete = self._observation(
            draft_status="completed",
            current_pick_number=216,
            our_pick_number=216,
            pick_label="18.12",
        )
        poller = _Poller(
            [
                _Cycle(observation, self._snapshot(observation), blocked),
                _Cycle(complete, self._snapshot(complete)),
            ]
        )
        pauses: list[float] = []

        report = self._session(poller, pauses).run(object())

        self.assertEqual(report.termination, SessionTermination.DRAFT_COMPLETE)
        self.assertEqual(report.cycles_completed, 2)
        self.assertEqual(pauses, [0.25])


if __name__ == "__main__":
    unittest.main()
