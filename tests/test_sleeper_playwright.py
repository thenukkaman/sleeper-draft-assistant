from __future__ import annotations

import unittest

from draft_assistant.adapters.sleeper_playwright import (
    SleeperBrowserTarget,
    SleeperPlaywrightTransport,
    SleeperPlaywrightTransportError,
    observation_from_dom_snapshot,
)
from draft_assistant.board import load_default_board
from draft_assistant.interfaces.browser_observation import BrowserBlockerKind, PlayerRowAction


class _Button:
    def __init__(self) -> None:
        self.clicks = 0

    def is_visible(self) -> bool:
        return True

    def click(self) -> None:
        self.clicks += 1


class _Locator:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.filled: list[str] = []

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> object:
        return self.items[index]

    def fill(self, value: str) -> None:
        self.filled.append(value)

    def is_visible(self) -> bool:
        return len(self.items) == 1 and self.items[0].is_visible()

    def click(self) -> None:
        self.items[0].click()


class _Row:
    def __init__(self, text: str, button: _Button) -> None:
        self.text = text
        self.button = button
        self.waits = 0

    def inner_text(self) -> str:
        return self.text

    def locator(self, selector: str) -> _Locator:
        if selector == ".draft-button":
            return _Locator([self.button])
        raise AssertionError(f"Unexpected row selector: {selector}")

    def wait_for(self, **kwargs) -> None:
        self.waits += 1
        self.wait_kwargs = kwargs


class _Page:
    def __init__(self, row: _Row, search: _Locator, auto_off: _Button) -> None:
        self.row = row
        self.search = search
        self.auto_off = auto_off

    def locator(self, selector: str) -> _Locator:
        if selector.startswith("input["):
            return self.search
        if selector == ".player-rank-item2":
            return _Locator([self.row])
        raise AssertionError(f"Unexpected page selector: {selector}")

    def get_by_role(self, role: str, **kwargs) -> _Locator:
        self.last_role = (role, kwargs)
        return _Locator([self.auto_off])


class SleeperPlaywrightParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.board = load_default_board()
        self.target = SleeperBrowserTarget(
            league_id="league-1",
            league_name="North Redmond 40",
            username="kenikh",
            draft_slot=5,
        )

    def _raw(self, **changes):
        raw = {
            "url": "https://sleeper.com/draft/nfl/mock-1",
            "bodyText": "North Redmond 40\nkenikh\nYou are on auto-pick.",
            "draftCells": [
                {"id": "draft-cell-5", "text": "1.05\nJosh Allen\nQB BUF", "classes": ["cell", "drafted"]},
                {"id": "draft-cell-7", "text": "1.07\nJalen Hurts\nQB PHI", "classes": ["cell", "drafted"]},
                {"id": "draft-cell-20", "text": "2.08\n01:37", "classes": ["cell"]},
            ],
            "playerRows": [
                {
                    "text": "1\nJalen Hurts\nQB PHI",
                    "hasDraftButton": True,
                    "hasQueueButton": True,
                    "hasDetailsControl": True,
                },
                {
                    "text": "2\nBrock Bowers\nTE LV",
                    "hasDraftButton": False,
                    "hasQueueButton": True,
                    "hasDetailsControl": True,
                },
            ],
            "hasUnexpectedDialog": False,
        }
        raw.update(changes)
        return raw

    def test_normalizes_visible_clock_roster_actions_and_auto_pick(self) -> None:
        observation = observation_from_dom_snapshot(self._raw(), self.board, self.target)

        self.assertEqual(observation.pick_label, "2.08")
        self.assertEqual(observation.current_pick_number, 20)
        self.assertEqual(observation.our_pick_number, 20)
        self.assertEqual(observation.clock_remaining_ms, 97_000)
        self.assertTrue(observation.auto_pick_enabled)
        self.assertEqual(observation.roster_positions, ("QB",))
        self.assertEqual(observation.drafted_players, frozenset({"Josh Allen", "Jalen Hurts"}))
        self.assertEqual(
            observation.player_row_actions["Jalen Hurts"],
            frozenset({PlayerRowAction.DRAFT, PlayerRowAction.QUEUE, PlayerRowAction.DETAILS}),
        )
        self.assertEqual(
            observation.player_row_actions["Brock Bowers"],
            frozenset({PlayerRowAction.QUEUE, PlayerRowAction.DETAILS}),
        )

    def test_refuses_a_page_without_visible_expected_identity(self) -> None:
        observation = observation_from_dom_snapshot(
            self._raw(bodyText="North Redmond 40\nnot-kenikh"), self.board, self.target
        )

        self.assertIsNotNone(observation.blocker)
        self.assertEqual(observation.blocker.kind, BrowserBlockerKind.IDENTITY_MISMATCH)

    def test_refuses_a_draft_room_without_one_active_countdown_cell(self) -> None:
        raw = self._raw(draftCells=[])
        observation = observation_from_dom_snapshot(raw, self.board, self.target)

        self.assertIsNotNone(observation.blocker)
        self.assertEqual(observation.blocker.kind, BrowserBlockerKind.PENDING_REQUEST)

    def test_waits_safely_in_an_initialized_room_before_sleeper_opens_the_clock(self) -> None:
        cells = self._raw()["draftCells"][:-1]
        observation = observation_from_dom_snapshot(self._raw(draftCells=cells), self.board, self.target)

        self.assertIsNone(observation.blocker)
        self.assertEqual(observation.draft_status, "waiting")
        self.assertEqual(observation.our_pick_number, 5)

    def test_transport_searches_the_exact_row_before_clicking_its_draft_button(self) -> None:
        button = _Button()
        row = _Row("Josh Allen\nQB BUF", button)
        search = _Locator([object()])
        auto_off = _Button()
        transport = SleeperPlaywrightTransport(_Page(row, search, auto_off), self.board, self.target)

        transport.click_exact_draft("Josh Allen")

        self.assertEqual(search.filled, ["Josh Allen"])
        self.assertEqual(row.waits, 1)
        self.assertEqual(button.clicks, 1)

    def test_transport_never_substitutes_a_nonmatching_row(self) -> None:
        button = _Button()
        row = _Row("Lamar Jackson\nQB BAL", button)
        transport = SleeperPlaywrightTransport(
            _Page(row, _Locator([object()]), _Button()), self.board, self.target
        )

        with self.assertRaisesRegex(SleeperPlaywrightTransportError, "exactly one"):
            transport.click_exact_draft("Josh Allen")

        self.assertEqual(button.clicks, 0)

    def test_transport_only_uses_the_semantic_auto_pick_off_button(self) -> None:
        button = _Button()
        page = _Page(_Row("Josh Allen\nQB BUF", _Button()), _Locator([object()]), button)
        transport = SleeperPlaywrightTransport(page, self.board, self.target)

        transport.set_auto_pick_off()

        self.assertEqual(button.clicks, 1)
        self.assertEqual(page.last_role, ("button", {"name": "TURN OFF AUTO-PICK", "exact": True}))


if __name__ == "__main__":
    unittest.main()
