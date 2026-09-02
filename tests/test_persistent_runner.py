from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from draft_assistant.autonomy import AutonomousDraftGuard
from draft_assistant.board import load_default_board
from draft_assistant.interfaces.browser_observation import BrowserObservation
from draft_assistant.live_driver import ClockFirstDraftDriver
from draft_assistant.persistent_runner import JsonlRunnerJournal, PersistentDraftRunner
from draft_assistant.policies.source_board import SourceBoardPolicy


class _SteppingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        self.current += timedelta(milliseconds=10)
        return self.current


class _Room:
    def __init__(self, observation: BrowserObservation) -> None:
        self.observation = observation
        self.observe_count = 0
        self.picks: list[str] = []
        self.auto_pick_changes: list[bool] = []

    def observe(self) -> BrowserObservation:
        self.observe_count += 1
        return self.observation

    def draft(self, player_name: str) -> None:
        self.picks.append(player_name)
        self.observation = replace(
            self.observation,
            drafted_players=self.observation.drafted_players | frozenset({player_name}),
            available_players=(self.observation.available_players or frozenset()) - frozenset({player_name}),
            current_pick_number=self.observation.current_pick_number + 1,
            our_pick_number=29,
        )

    def set_auto_pick(self, enabled: bool) -> None:
        self.auto_pick_changes.append(enabled)
        self.observation = replace(self.observation, auto_pick_enabled=enabled)


class PersistentDraftRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = load_default_board()
        self.policy = SourceBoardPolicy()
        self.clock = _SteppingClock()
        self.driver = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
            now=self.clock.now,
        )
        self.runner = PersistentDraftRunner(
            self.board,
            self.policy,
            self.driver,
            now=self.clock.now,
        )

    @staticmethod
    def _observation(
        *,
        current_pick_number: int,
        our_pick_number: int,
        round_number: int,
        pick_label: str,
        roster_positions: tuple[str, ...] = ("QB",),
        drafted_players: frozenset[str] = frozenset(
            {"Josh Allen", "Lamar Jackson", "Jahmyr Gibbs", "Bijan Robinson"}
        ),
        available_players: frozenset[str] = frozenset({"Jayden Daniels", "Jalen Hurts", "Justin Herbert"}),
        auto_pick_enabled: bool = False,
    ) -> BrowserObservation:
        return BrowserObservation(
            league_id="league-1",
            username="kenikh",
            draft_status="drafting",
            auto_pick_enabled=auto_pick_enabled,
            round_number=round_number,
            pick_label=pick_label,
            current_pick_number=current_pick_number,
            our_pick_number=our_pick_number,
            roster_positions=roster_positions,
            drafted_players=drafted_players,
            available_players=available_players,
            clock_remaining_ms=119_000,
            clock_precision_ms=1_000,
        )

    def test_prepares_after_an_opponent_pick_then_uses_filtered_ladder_at_our_clock(self) -> None:
        room = _Room(
            self._observation(
                current_pick_number=6,
                our_pick_number=20,
                round_number=1,
                pick_label="1.06",
            )
        )

        prepared_cycle = self.runner.poll_once(room)

        self.assertIsNone(prepared_cycle.attempt)
        self.assertIsNotNone(prepared_cycle.prepared_pick)
        self.assertEqual(prepared_cycle.prepared_pick.candidates[0].player.name, "Jayden Daniels")
        self.assertEqual(room.observe_count, 1)

        room.observation = self._observation(
            current_pick_number=20,
            our_pick_number=20,
            round_number=2,
            pick_label="2.08",
            drafted_players=room.observation.drafted_players | frozenset({"Jayden Daniels"}),
            available_players=frozenset({"Jalen Hurts", "Justin Herbert"}),
        )
        action_cycle = self.runner.poll_once(room)

        self.assertTrue(action_cycle.attempt.confirmed)
        self.assertEqual(room.picks, ["Jalen Hurts"])
        self.assertEqual(action_cycle.telemetry.outcome, "confirmed")
        self.assertEqual(action_cycle.telemetry.expected_player, "Jalen Hurts")
        self.assertEqual(action_cycle.telemetry.selected_player, "Jalen Hurts")
        self.assertIsNotNone(action_cycle.telemetry.timing.recommendation_ready_at)
        self.assertIsNotNone(action_cycle.telemetry.selection_requested_at)
        self.assertIsNotNone(action_cycle.telemetry.confirmed_at)
        # One outer poll plus the commit and confirmation reads; no duplicate
        # initial observation is added by the clock-first driver.
        self.assertEqual(room.observe_count, 4)

    def test_rechecks_the_prepared_ladder_when_the_primary_disappears_before_commit(self) -> None:
        before_our_turn = self._observation(
            current_pick_number=6,
            our_pick_number=20,
            round_number=1,
            pick_label="1.06",
            available_players=frozenset({"Jayden Daniels", "Jalen Hurts", "Justin Herbert"}),
        )
        prepared = self.runner.planner.prepare_next(self.board, self.policy, before_our_turn.to_state())
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared.candidates[0].player.name, "Jayden Daniels")
        self.runner.prepared_pick = prepared

        first = self._observation(
            current_pick_number=20,
            our_pick_number=20,
            round_number=2,
            pick_label="2.08",
            available_players=frozenset({"Jayden Daniels", "Jalen Hurts", "Justin Herbert"}),
        )
        commit = self._observation(
            current_pick_number=20,
            our_pick_number=20,
            round_number=2,
            pick_label="2.08",
            drafted_players=first.drafted_players | frozenset({"Jayden Daniels"}),
            available_players=frozenset({"Jalen Hurts", "Justin Herbert"}),
        )

        class CommitChangeRoom(_Room):
            def __init__(self) -> None:
                super().__init__(first)
                self._commit = commit
                self._served_outer = False

            def observe(self) -> BrowserObservation:
                self.observe_count += 1
                if not self._served_outer:
                    self._served_outer = True
                    return self.observation
                if self._commit is not None:
                    self.observation = self._commit
                    self._commit = None
                return self.observation

        room = CommitChangeRoom()
        cycle = self.runner.poll_once(room)

        self.assertTrue(cycle.attempt.confirmed)
        self.assertEqual(room.picks, ["Jalen Hurts"])
        self.assertEqual(cycle.telemetry.prepared_player_before, "Jayden Daniels")
        self.assertEqual(cycle.telemetry.selected_player, "Jalen Hurts")
        self.assertEqual(cycle.telemetry.outcome, "confirmed")

    def test_recovers_auto_pick_even_before_our_turn_and_records_bounded_timing(self) -> None:
        room = _Room(
            self._observation(
                current_pick_number=6,
                our_pick_number=20,
                round_number=1,
                pick_label="1.06",
                auto_pick_enabled=True,
            )
        )
        # Establish a previous completed poll so the auto-pick detection metric
        # has a genuine upper bound rather than an invented exact onset.
        self.runner.poll_once(
            _Room(
                self._observation(
                    current_pick_number=5,
                    our_pick_number=20,
                    round_number=1,
                    pick_label="1.05",
                )
            )
        )

        cycle = self.runner.poll_once(room)

        self.assertFalse(cycle.attempt.acted)
        self.assertEqual(room.auto_pick_changes, [False])
        self.assertEqual(cycle.telemetry.auto_pick_before, True)
        self.assertEqual(cycle.telemetry.auto_pick_after, False)
        self.assertEqual(cycle.telemetry.auto_pick_recovery, "recovered")
        self.assertFalse(cycle.snapshot.auto_pick_enabled)
        self.assertIsNotNone(cycle.telemetry.timing.auto_pick_detected_at)
        self.assertIsNotNone(cycle.telemetry.timing.auto_pick_disable_requested_at)
        self.assertIsNotNone(cycle.telemetry.timing.auto_pick_disabled_at)
        self.assertIsNotNone(cycle.telemetry.auto_pick_detection_upper_bound_ms)

    def test_auto_pick_miss_persists_the_post_recovery_clock_not_the_stale_clock(self) -> None:
        initial = self._observation(
            current_pick_number=5,
            our_pick_number=5,
            round_number=1,
            pick_label="1.05",
            auto_pick_enabled=True,
        )

        class LostPickRoom(_Room):
            def set_auto_pick(self, enabled: bool) -> None:
                self.auto_pick_changes.append(enabled)
                self.observation = replace(
                    self.observation,
                    auto_pick_enabled=enabled,
                    current_pick_number=6,
                    our_pick_number=20,
                    drafted_players=self.observation.drafted_players | frozenset({"Lamar Jackson"}),
                    available_players=frozenset({"Jayden Daniels", "Jalen Hurts"}),
                )

        room = LostPickRoom(initial)
        cycle = self.runner.poll_once(room)

        self.assertEqual(cycle.telemetry.outcome, "missed")
        self.assertEqual(cycle.snapshot.current_pick_number, 6)
        self.assertEqual(cycle.snapshot.our_pick_number, 20)
        self.assertFalse(cycle.snapshot.auto_pick_enabled)
        self.assertEqual(cycle.prepared_pick.pick_number, 20)

    def test_journal_persists_a_compact_last_poll_without_browser_secrets(self) -> None:
        room = _Room(
            self._observation(
                current_pick_number=6,
                our_pick_number=20,
                round_number=1,
                pick_label="1.06",
            )
        )
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runner.jsonl"
            runner = PersistentDraftRunner(
                self.board,
                self.policy,
                self.driver,
                journal=JsonlRunnerJournal(path),
                now=self.clock.now,
            )
            cycle = runner.poll_once(room)
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pick_label"], "1.06")
        self.assertEqual(records[0]["prepared_player"], "Jayden Daniels")
        self.assertEqual(records[0]["action_outcome"], None)
        self.assertEqual(cycle.snapshot.prepared_player, "Jayden Daniels")
        self.assertNotIn("cookie", records[0])


if __name__ == "__main__":
    unittest.main()
