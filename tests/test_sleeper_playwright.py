from __future__ import annotations

import unittest

from draft_assistant.adapters.sleeper_playwright import (
    SleeperBrowserTarget,
    SleeperPlaywrightTransport,
    SleeperPlaywrightTransportError,
    _known_specialist_drafted,
    _resolve_board_player,
    _sleeper_abbreviation,
    _visible_specialist_names,
    observation_from_dom_snapshot,
)
from draft_assistant.board import load_default_board
from draft_assistant.interfaces.browser_observation import BrowserBlockerKind, PlayerRowAction


class _Button:
    def __init__(self) -> None:
        self.clicks = 0

    def is_visible(self) -> bool:
        return True

    def click(self, **kwargs) -> None:
        self.clicks += 1



class _Locator:
    def __init__(self, items: list[object]) -> None:
        self.items = items
        self.filled: list[str] = []

    def count(self) -> int:
        return len(self.items)

    def nth(self, index: int) -> object:
        return self.items[index]

    def fill(self, value: str, **kwargs) -> None:
        self.filled.append(value)

    def is_visible(self) -> bool:
        return len(self.items) == 1 and self.items[0].is_visible()

    def click(self, **kwargs) -> None:
        self.items[0].click(**kwargs)

    def wait_for(self, **kwargs) -> None:
        self.items[0].wait_for(**kwargs)


class _Row:
    def __init__(self, text: str, button: _Button) -> None:
        self.text = text
        self.button = button
        self.waits = 0

    def inner_text(self, **kwargs) -> str:
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

    def get_by_text(self, text: str, **kwargs) -> _Locator:
        self.last_text = (text, kwargs)
        return _Locator([self.auto_off])

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits = getattr(self, "waits", []) + [milliseconds]


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
            "playerCardOpen": False,
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

    def test_names_a_cookie_dialog_without_making_the_privacy_choice(self) -> None:
        observation = observation_from_dom_snapshot(
            self._raw(
                hasUnexpectedDialog=True,
                dialogTexts=["Cookies and Personal Information\nAllow All\nConfirm My Choices"],
            ),
            self.board,
            self.target,
        )

        self.assertIsNotNone(observation.blocker)
        self.assertIn("Cookie-consent", observation.blocker.summary)

    def test_names_a_player_card_for_safe_dismissal(self) -> None:
        observation = observation_from_dom_snapshot(
            self._raw(playerCardOpen=True), self.board, self.target
        )

        self.assertIsNotNone(observation.blocker)
        self.assertEqual(observation.blocker.kind, BrowserBlockerKind.PLAYER_CARD)

    def test_resolves_sleeper_abbreviations_without_brown_name_collision(self) -> None:
        self.assertEqual(_resolve_board_player("2.08\nA. Brown\nWR - NE", self.board), "AJ Brown")
        self.assertEqual(
            _resolve_board_player("2.03\nA. St. Brown\nWR - DET", self.board),
            "Amon-Ra St. Brown",
        )

    def test_does_not_mistake_a_compound_given_name_for_a_surname_particle(self) -> None:
        self.assertEqual(_sleeper_abbreviation("De'Von Achane"), "dachane")
        self.assertEqual(
            _resolve_board_player("2.02\nD. Achane\nRB - MIA", self.board),
            "De'Von Achane",
        )

    def test_resolves_a_unique_truncated_sleeper_draft_cell(self) -> None:
        self.assertEqual(_sleeper_abbreviation("Jaxon Smith-Njigba"), "jsmithnjigba")
        self.assertEqual(
            _resolve_board_player("7.07\nJ. Smith-N...\nWR - SEA", self.board),
            "Jaxon Smith-Njigba",
        )

    def test_extracts_specialists_only_from_rendered_sleeper_position_rows(self) -> None:
        rows = (
            {"text": "1\nC. Boswell\nK - PIT"},
            {"text": "2\nD. Carlson\nK - LV"},
            {"text": "3\nM. Nabers\nWR - NYG"},
            {"text": "4\nL. Chargers\nDEF - LAC"},
        )

        self.assertEqual(_visible_specialist_names(rows, "K"), ("C. Boswell", "D. Carlson"))
        self.assertEqual(_visible_specialist_names(rows, "DEF"), ("L. Chargers",))

    def test_retains_visible_specialist_draft_action_outside_analyst_board(self) -> None:
        observation = observation_from_dom_snapshot(
            self._raw(
                playerRows=[
                    {
                        "text": "190\nLos Angeles Chargers\nDEF\nLAC",
                        "hasDraftButton": True,
                        "hasQueueButton": True,
                        "hasDetailsControl": True,
                    }
                ]
            ),
            self.board,
            self.target,
        )

        self.assertEqual(
            observation.player_row_actions["Los Angeles Chargers"],
            frozenset({PlayerRowAction.DRAFT, PlayerRowAction.QUEUE, PlayerRowAction.DETAILS}),
        )

    def test_resolves_abbreviated_drafted_specialist_against_visible_rank_list(self) -> None:
        drafted = _known_specialist_drafted(
            ({"text": "17.5\nD. Lions\nDEF - DET", "classes": ["drafted"]},),
            {"DEF": ("Detroit Lions", "Dallas Cowboys")},
        )

        self.assertEqual(drafted, {"Detroit Lions"})

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

    def test_recognizes_a_full_18_pick_roster_as_draft_complete(self) -> None:
        observation = observation_from_dom_snapshot(
            self._raw(
                bodyText="North Redmond 40\nkenikh\nAll\n18/18",
                draftCells=self._raw()["draftCells"][:-1],
            ),
            self.board,
            self.target,
        )

        self.assertEqual(observation.draft_status, "completed")

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

    def test_transport_only_uses_the_exact_visible_auto_pick_off_control(self) -> None:
        button = _Button()
        page = _Page(_Row("Josh Allen\nQB BUF", _Button()), _Locator([object()]), button)
        transport = SleeperPlaywrightTransport(page, self.board, self.target)

        transport.set_auto_pick_off()

        self.assertEqual(button.clicks, 1)
        self.assertEqual(page.last_text, ("TURN OFF AUTO-PICK", {"exact": True}))


if __name__ == "__main__":
    unittest.main()
