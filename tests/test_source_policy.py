from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from draft_assistant.autonomy import AutonomousDraftGuard
from draft_assistant.board import load_default_board
from draft_assistant.interfaces.browser_observation import (
    BrowserBlocker,
    BrowserBlockerKind,
    BrowserObservation,
    PlayerRowAction,
)
from draft_assistant.interfaces.sleeper_draftboard import parse_visible_draftboard
from draft_assistant.live_driver import ClockFirstDraftDriver
from draft_assistant.lookahead import LookaheadPlanner, pick_label_for_number
from draft_assistant.models import DraftState, NewsDecision, NewsReview
from draft_assistant.policies.sleeper_special_teams import SleeperRankedSpecialTeamsPolicy
from draft_assistant.policies.source_board import SourceBoardPolicy


class SourceBoardPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = load_default_board()
        self.policy = SourceBoardPolicy()

    def test_opening_pick_uses_the_elite_qb_order(self) -> None:
        result = self.policy.recommend(self.board, DraftState(round_number=1, pick_label="1.05"))
        self.assertEqual(result.primary.player.name, "Josh Allen")
        self.assertEqual(result.candidates[1].player.name, "Lamar Jackson")

    def test_round_two_forces_a_viable_qb2_before_bowers(self) -> None:
        result = self.policy.recommend(
            self.board,
            DraftState(
                round_number=2,
                pick_label="2.08",
                roster_positions=("QB",),
                drafted_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye", "Jayden Daniels", "Jalen Hurts"}),
            ),
        )
        self.assertEqual(result.primary.player.name, "Justin Herbert")
        self.assertIn("QB2", result.directive)

    def test_avoid_stays_behind_an_ordinary_source_value_before_it_falls_far_enough(self) -> None:
        result = self.policy.recommend(
            self.board,
            DraftState(
                round_number=5,
                pick_label="5.05",
                roster_positions=("QB", "QB", "RB", "WR"),
                available_players=frozenset({"Jonathan Taylor", "Saquon Barkley"}),
            ),
        )
        self.assertEqual(result.primary.player.name, "Saquon Barkley")
        self.assertIn("Jonathan Taylor", [candidate.player.name for candidate in result.candidates])
        self.assertTrue(result.candidates[1].discounted_avoid)

    def test_avoid_can_be_selected_as_a_late_discounted_value(self) -> None:
        result = self.policy.recommend(
            self.board,
            DraftState(
                round_number=10,
                pick_label="10.08",
                roster_positions=("QB", "QB", "QB", "RB", "RB", "WR", "WR", "TE"),
                available_players=frozenset({"Rashee Rice"}),
            ),
        )
        self.assertEqual(result.primary.player.name, "Rashee Rice")
        self.assertTrue(result.primary.discounted_avoid)

    def test_unresolved_check_news_player_is_not_eligible(self) -> None:
        result = self.policy.recommend(
            self.board,
            DraftState(
                round_number=5,
                pick_label="5.05",
                roster_positions=("QB", "QB", "RB", "WR"),
                available_players=frozenset({"Josh Jacobs", "Rhamondre Stevenson"}),
            ),
        )
        self.assertEqual(result.primary.player.name, "Rhamondre Stevenson")
        self.assertNotIn("Josh Jacobs", [candidate.player.name for candidate in result.candidates])
        self.assertTrue(any("Josh Jacobs" in warning for warning in result.warnings))

    def test_check_news_requires_a_fresh_source_linked_clear_review(self) -> None:
        now = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)
        state = DraftState(
            round_number=5,
            pick_label="5.05",
            roster_positions=("QB", "QB", "RB", "WR"),
            available_players=frozenset({"Josh Jacobs"}),
            news_reviews={
                "Josh Jacobs": NewsReview(
                    decision=NewsDecision.CLEAR,
                    checked_at=now - timedelta(minutes=5),
                    source_url="https://www.nfl.com/news/example",
                    note="No restriction reported.",
                )
            },
            observed_at=now,
            league_id="1314724839730204672",
            username="kenikh",
            auto_pick_enabled=False,
            current_pick_number=53,
            our_pick_number=53,
        )

        result = self.policy.recommend(self.board, state)
        gate = AutonomousDraftGuard("1314724839730204672", "kenikh").evaluate(state, result)

        self.assertEqual(result.primary.player.name, "Josh Jacobs")
        self.assertTrue(gate.allowed)

    def test_rsp_overlay_breaks_a_late_upside_tie_without_rewriting_the_board(self) -> None:
        result = self.policy.recommend(
            self.board,
            DraftState(
                round_number=8,
                pick_label="8.08",
                roster_positions=("QB", "QB", "QB", "RB", "RB", "WR", "TE"),
                available_players=frozenset({"Carnell Tate", "Rome Odunze"}),
            ),
        )

        self.assertEqual(result.primary.player.name, "Carnell Tate")
        self.assertIn("RSP overlay", result.primary.reason)

    def test_special_teams_waits_until_round_17_then_uses_sleeper_rank(self) -> None:
        specialist_policy = SleeperRankedSpecialTeamsPolicy(self.policy)
        result = specialist_policy.recommend(
            self.board,
            DraftState(
                round_number=17,
                pick_label="17.05",
                roster_positions=("QB", "QB", "QB", "RB", "RB", "WR", "WR", "TE"),
                sleeper_ranked_specialists={"DEF": ("Denver Broncos", "Philadelphia Eagles")},
            ),
        )
        self.assertEqual(result.primary.player.name, "Denver Broncos")
        self.assertEqual(result.primary.player.position, "DEF")
        gate = AutonomousDraftGuard("1314724839730204672", "kenikh").evaluate(
            DraftState(
                round_number=17,
                pick_label="17.05",
                roster_positions=("QB", "QB", "QB", "RB", "RB", "WR", "WR", "TE"),
                sleeper_ranked_specialists={"DEF": ("Denver Broncos", "Philadelphia Eagles")},
                league_id="1314724839730204672",
                username="kenikh",
                draft_status="drafting",
                auto_pick_enabled=False,
                current_pick_number=188,
                our_pick_number=188,
            ),
            result,
        )
        self.assertTrue(gate.allowed)

    def test_autonomy_guard_blocks_another_team_pick(self) -> None:
        state = DraftState(
            round_number=1,
            pick_label="1.05",
            league_id="1314724839730204672",
            username="kenikh",
            draft_status="drafting",
            auto_pick_enabled=False,
            current_pick_number=4,
            our_pick_number=5,
        )
        result = self.policy.recommend(self.board, state)
        gate = AutonomousDraftGuard("1314724839730204672", "kenikh").evaluate(state, result)
        self.assertFalse(gate.allowed)
        self.assertIn("not the approved team's live pick", gate.reasons[0])

    def test_autonomy_guard_blocks_when_sleeper_auto_pick_is_on(self) -> None:
        state = DraftState(
            round_number=3,
            pick_label="3.05",
            roster_positions=("WR", "TE"),
            league_id="1314724839730204672",
            username="kenikh",
            draft_status="drafting",
            auto_pick_enabled=True,
            current_pick_number=29,
            our_pick_number=29,
        )
        result = self.policy.recommend(self.board, state)
        gate = AutonomousDraftGuard("1314724839730204672", "kenikh").evaluate(state, result)
        self.assertFalse(gate.allowed)
        self.assertIn("auto-pick", gate.reasons[0])

    def test_clock_driver_clicks_once_only_for_our_live_pick(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.picks = []

            def observe(self):
                return self.observation

            def draft(self, player_name):
                self.picks.append(player_name)
                self.observation = replace(
                    self.observation,
                    drafted_players=self.observation.drafted_players | frozenset({player_name}),
                    current_pick_number=self.observation.current_pick_number + 1,
                )

        observation = BrowserObservation(
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
            available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
        )
        room = Room(observation)
        driver = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        )

        attempt = driver.run_once(room)

        self.assertTrue(attempt.acted)
        self.assertTrue(attempt.confirmed)
        self.assertEqual(room.picks, ["Josh Allen"])

    def test_clock_driver_never_retries_an_unverified_click(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.picks = []

            def observe(self):
                return self.observation

            def draft(self, player_name):
                self.picks.append(player_name)

        observation = BrowserObservation(
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
            available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
        )
        room = Room(observation)
        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(room)

        self.assertTrue(attempt.acted)
        self.assertFalse(attempt.confirmed)
        self.assertEqual(room.picks, ["Josh Allen"])
        self.assertIn("do not retry", attempt.reason)

    def test_clock_driver_refuses_player_card_or_queue_actions(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.picks = []

            def observe(self):
                return self.observation

            def draft(self, player_name):
                self.picks.append(player_name)

        observation = BrowserObservation(
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
            available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
            player_row_actions={"Josh Allen": frozenset({PlayerRowAction.QUEUE, PlayerRowAction.DETAILS})},
        )

        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(Room(observation))

        self.assertFalse(attempt.acted)
        self.assertIn("semantic DRAFT control", attempt.reason)

    def test_clock_driver_fail_closes_on_a_browser_blocker(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.auto_pick_changes = []
                self.picks = []

            def observe(self):
                return self.observation

            def set_auto_pick(self, enabled):
                self.auto_pick_changes.append(enabled)

            def draft(self, player_name):
                self.picks.append(player_name)

        observation = BrowserObservation(
            league_id="league-1",
            username="kenikh",
            draft_status="drafting",
            auto_pick_enabled=True,
            round_number=1,
            pick_label="1.05",
            current_pick_number=5,
            our_pick_number=5,
            roster_positions=(),
            drafted_players=frozenset(),
            available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
            blocker=BrowserBlocker(
                BrowserBlockerKind.PENDING_REQUEST,
                "NEW MOCK DRAFT remained a spinner after the click.",
            ),
        )
        room = Room(observation)

        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(room)

        self.assertFalse(attempt.acted)
        self.assertFalse(attempt.confirmed)
        self.assertEqual(room.picks, [])
        self.assertEqual(room.auto_pick_changes, [])
        self.assertFalse(attempt.gate.allowed)
        self.assertIn("pending_request", attempt.gate.reasons[-1])
        self.assertIn("Browser UI is blocked", attempt.reason)

    def test_clock_driver_stops_if_mock_preflight_is_visible_before_commit(self) -> None:
        class Room:
            def __init__(self, first, blocked):
                self.observations = [first, blocked]
                self.picks = []

            def observe(self):
                return self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]

            def draft(self, player_name):
                self.picks.append(player_name)

        first = BrowserObservation(
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
            available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
        )
        blocked = replace(
            first,
            blocker=BrowserBlocker(
                BrowserBlockerKind.MOCK_DRAFT_READY,
                "Mock board is ready and exposes the START DRAFT control.",
                ("START DRAFT",),
            ),
        )
        room = Room(first, blocked)

        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(room)

        self.assertFalse(attempt.acted)
        self.assertEqual(room.picks, [])
        self.assertIn("mock_draft_ready", attempt.gate.reasons[-1])

    def test_lookahead_prepares_the_next_snake_pick_and_filters_newly_drafted_players(self) -> None:
        state = DraftState(
            round_number=1,
            pick_label="1.06",
            roster_positions=("QB",),
            drafted_players=frozenset({"Josh Allen", "Lamar Jackson", "Jahmyr Gibbs", "Bijan Robinson"}),
            available_players=frozenset({"Jayden Daniels", "Jalen Hurts", "Justin Herbert"}),
            current_pick_number=6,
            our_pick_number=20,
        )
        prepared = LookaheadPlanner().prepare_next(self.board, self.policy, state)

        self.assertIsNotNone(prepared)
        self.assertEqual((prepared.round_number, prepared.pick_label), (2, "2.08"))
        self.assertEqual(prepared.candidates[0].player.name, "Jayden Daniels")

        at_clock = replace(
            state,
            round_number=2,
            pick_label="2.08",
            current_pick_number=20,
            drafted_players=state.drafted_players | frozenset({"Jayden Daniels"}),
            available_players=frozenset({"Jalen Hurts", "Justin Herbert"}),
        )
        recommendation = prepared.recommendation_for(at_clock)

        self.assertIsNotNone(recommendation)
        self.assertEqual(recommendation.primary.player.name, "Jalen Hurts")

    def test_lookahead_never_applies_to_a_changed_roster_or_pick(self) -> None:
        state = DraftState(
            round_number=1,
            pick_label="1.06",
            roster_positions=("QB",),
            drafted_players=frozenset({"Josh Allen", "Lamar Jackson"}),
            current_pick_number=6,
            our_pick_number=20,
        )
        prepared = LookaheadPlanner().prepare_next(self.board, self.policy, state)

        self.assertIsNotNone(prepared)
        self.assertIsNone(prepared.recommendation_for(replace(state, roster_positions=("QB", "RB"))))
        self.assertEqual(pick_label_for_number(20), (2, "2.08"))
        self.assertEqual(pick_label_for_number(29), (3, "3.05"))

    def test_clock_driver_disables_auto_pick_then_salvages_the_live_pick(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.auto_pick_changes = []
                self.picks = []

            def observe(self):
                return self.observation

            def set_auto_pick(self, enabled):
                self.auto_pick_changes.append(enabled)
                self.observation = replace(self.observation, auto_pick_enabled=enabled)

            def draft(self, player_name):
                self.picks.append(player_name)
                self.observation = replace(
                    self.observation,
                    drafted_players=self.observation.drafted_players | frozenset({player_name}),
                    current_pick_number=self.observation.current_pick_number + 1,
                )

        room = Room(
            BrowserObservation(
                league_id="league-1",
                username="kenikh",
                draft_status="drafting",
                auto_pick_enabled=True,
                round_number=1,
                pick_label="1.05",
                current_pick_number=5,
                our_pick_number=5,
                roster_positions=(),
                drafted_players=frozenset(),
                available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
            )
        )

        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(room)

        self.assertTrue(attempt.acted)
        self.assertTrue(attempt.confirmed)
        self.assertEqual(room.auto_pick_changes, [False])
        self.assertEqual(room.picks, ["Josh Allen"])
        self.assertIsNotNone(attempt.auto_pick_recovery)
        self.assertTrue(attempt.auto_pick_recovery.recovered)
        self.assertFalse(attempt.auto_pick_recovery.pick_was_missed)

    def test_clock_driver_records_a_pick_already_lost_to_auto_pick(self) -> None:
        class Room:
            def __init__(self, observation):
                self.observation = observation
                self.picks = []

            def observe(self):
                return self.observation

            def set_auto_pick(self, enabled):
                self.observation = replace(
                    self.observation,
                    auto_pick_enabled=enabled,
                    current_pick_number=self.observation.current_pick_number + 1,
                    drafted_players=self.observation.drafted_players | frozenset({"Lamar Jackson"}),
                )

            def draft(self, player_name):
                self.picks.append(player_name)

        room = Room(
            BrowserObservation(
                league_id="league-1",
                username="kenikh",
                draft_status="drafting",
                auto_pick_enabled=True,
                round_number=1,
                pick_label="1.05",
                current_pick_number=5,
                our_pick_number=5,
                roster_positions=(),
                drafted_players=frozenset(),
                available_players=frozenset({"Josh Allen", "Lamar Jackson", "Drake Maye"}),
            )
        )

        attempt = ClockFirstDraftDriver(
            self.board,
            self.policy,
            AutonomousDraftGuard("league-1", "kenikh"),
        ).run_once(room)

        self.assertFalse(attempt.acted)
        self.assertFalse(attempt.confirmed)
        self.assertEqual(room.picks, [])
        self.assertIsNotNone(attempt.auto_pick_recovery)
        self.assertTrue(attempt.auto_pick_recovery.recovered)
        self.assertTrue(attempt.auto_pick_recovery.pick_was_missed)
        self.assertIn("already advanced", attempt.reason)

    def test_visible_draftboard_refreshes_roster_taken_players_and_clock(self) -> None:
        snapshot = '''
- heading "kenikh" [level=1]
- heading "kenikh" [level=1]
- generic: "1.5"
- generic: J. Hurts
- generic: QB - PHI
- generic: "2.8"
- generic: B. Bowers
- generic: TE - LV
- heading "Team 6" [level=1]
- generic: "1.6"
- generic: J. Herbert
- generic: QB - LAC
- generic: "3.5"
- generic: 01:14
'''
        parsed = parse_visible_draftboard(snapshot, self.board, "kenikh")

        self.assertEqual(parsed.roster_positions, ("QB", "TE"))
        self.assertIn("Jalen Hurts", parsed.drafted_players)
        self.assertIn("Justin Herbert", parsed.drafted_players)
        self.assertEqual(parsed.current_pick_label, "3.5")
        self.assertEqual(parsed.current_pick_number, 29)


if __name__ == "__main__":
    unittest.main()
