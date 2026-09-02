from __future__ import annotations

from dataclasses import replace
import unittest

from draft_assistant.adapters.sleeper_browser import SleeperBrowserDraftRoom
from draft_assistant.interfaces.browser_observation import BrowserObservation, PlayerRowAction


class _Transport:
    def __init__(self, observation: BrowserObservation) -> None:
        self.observation = observation
        self.draft_requests: list[str] = []
        self.row_reveal_requests: list[str] = []
        self.auto_pick_off_requests = 0

    def observe_visible_draft_room(self) -> BrowserObservation:
        return self.observation

    def click_exact_draft(self, player_name: str) -> None:
        self.draft_requests.append(player_name)

    def reveal_player_row(self, player_name: str) -> None:
        self.row_reveal_requests.append(player_name)

    def set_auto_pick_off(self) -> None:
        self.auto_pick_off_requests += 1


class SleeperBrowserDraftRoomTests(unittest.TestCase):
    @staticmethod
    def _observation(**changes) -> BrowserObservation:
        base = BrowserObservation(
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
            available_players=frozenset({"Josh Allen", "Lamar Jackson"}),
            player_row_actions={"Josh Allen": frozenset({PlayerRowAction.DRAFT})},
        )
        return replace(base, **changes)

    def test_passes_only_the_exact_visible_draft_action_to_the_transport(self) -> None:
        transport = _Transport(self._observation())
        room = SleeperBrowserDraftRoom(transport)

        room.observe()
        room.draft("Josh Allen")

        self.assertEqual(transport.draft_requests, ["Josh Allen"])

    def test_refuses_player_card_or_queue_controls(self) -> None:
        transport = _Transport(
            self._observation(
                player_row_actions={"Josh Allen": frozenset({PlayerRowAction.QUEUE, PlayerRowAction.DETAILS})}
            )
        )
        room = SleeperBrowserDraftRoom(transport)

        room.observe()
        with self.assertRaisesRegex(RuntimeError, "queue controls are prohibited"):
            room.draft("Josh Allen")

        self.assertEqual(transport.draft_requests, [])

    def test_auto_pick_can_only_be_disabled_after_a_visible_enabled_state(self) -> None:
        transport = _Transport(self._observation(auto_pick_enabled=True))
        room = SleeperBrowserDraftRoom(transport)

        room.observe()
        room.set_auto_pick(False)
        self.assertEqual(transport.auto_pick_off_requests, 1)
        with self.assertRaisesRegex(RuntimeError, "only turn auto-pick off"):
            room.set_auto_pick(True)

    def test_revealing_a_virtualized_player_row_is_not_a_draft_action(self) -> None:
        transport = _Transport(self._observation())
        room = SleeperBrowserDraftRoom(transport)

        observed = room.ensure_draft_action("Lamar Jackson")

        self.assertEqual(transport.row_reveal_requests, ["Lamar Jackson"])
        self.assertEqual(transport.draft_requests, [])
        self.assertEqual(observed, transport.observation)

    def test_requires_a_fresh_observation_before_any_browser_action(self) -> None:
        transport = _Transport(self._observation())
        room = SleeperBrowserDraftRoom(transport)

        with self.assertRaisesRegex(RuntimeError, "fresh Sleeper observation"):
            room.draft("Josh Allen")

        self.assertEqual(transport.draft_requests, [])


if __name__ == "__main__":
    unittest.main()
