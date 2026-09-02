"""Optional local-CDP transport for Sleeper's visible browser DOM.

The core package does not require Playwright.  When installed as the optional
``browser`` extra, a host process may attach to a browser the user already
opened and authenticated locally.  This adapter never launches a browser,
reads cookies, stores a profile, calls Sleeper's private APIs, or clicks a
coordinate.  It works only through visible DOM text and semantic controls.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import re
from typing import Any, Mapping, Sequence

from ..board import Board, normalize_name
from ..interfaces.browser_observation import (
    BrowserBlocker,
    BrowserBlockerKind,
    BrowserObservation,
    PlayerRowAction,
)


_DRAFT_ROUTE = "/draft/nfl/"
_CELL_ID = re.compile(r"^draft-cell-(?P<pick>\d+)$")
_CLOCK = re.compile(r"(?<!\d)(?P<minutes>\d{1,2}):(?P<seconds>\d{2})(?!\d)")
_POSITION = re.compile(r"\b(?P<position>QB|RB|WR|TE|K|DEF)\b", re.IGNORECASE)

# Deliberately modest selectors, all verified again through visible text.  A
# missing selector returns a blocker; it is never replaced with a coordinate.
_PLAYER_ROW = ".player-rank-item2"
_DRAFT_BUTTON = ".draft-button"
_SEARCH_INPUT = 'input[placeholder^="Find player"]'
_AUTO_PICK_OFF = "TURN OFF AUTO-PICK"


class SleeperPlaywrightTransportError(RuntimeError):
    """A local DOM/CDP failure that the host session must treat as fail-closed."""


@dataclass(frozen=True)
class SleeperBrowserTarget:
    """Stable run configuration, distinct from player board data and policy."""

    league_id: str
    league_name: str
    username: str
    draft_slot: int
    teams: int = 12

    def __post_init__(self) -> None:
        if self.teams < 2:
            raise ValueError("teams must be at least two")
        if not 1 <= self.draft_slot <= self.teams:
            raise ValueError("draft_slot must be within the draft order")


@dataclass
class SleeperPlaywrightSession:
    """Keep a CDP attachment alive without taking ownership of the browser."""

    transport: "SleeperPlaywrightTransport"
    _playwright: Any

    def close(self) -> None:
        """Disconnect the local client; never close the user's browser instance."""

        self._playwright.stop()


def connect_local_cdp(
    endpoint: str,
    board: Board,
    target: SleeperBrowserTarget,
) -> SleeperPlaywrightSession:
    """Attach only to an existing local browser debugging endpoint.

    The caller, not this package, is responsible for manually opening an
    authenticated local browser with CDP enabled.  A strict Sleeper draft-room
    tab is required; a pre-draft tab is intentionally not enough.
    """

    playwright, browser = _connect_browser(endpoint)
    try:
        pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if "sleeper.com" in page.url and _DRAFT_ROUTE in page.url
        ]
        if len(pages) != 1:
            raise SleeperPlaywrightTransportError(
                "Expected exactly one open Sleeper draft-room tab at the local CDP endpoint; "
                f"found {len(pages)}."
            )
        return SleeperPlaywrightSession(
            transport=SleeperPlaywrightTransport(pages[0], board, target),
            _playwright=playwright,
        )
    except Exception:
        # We deliberately do not call browser.close(): this client must never
        # close a manually controlled browser it attached to through CDP.
        playwright.stop()
        raise


def enter_live_draftroom_local_cdp(
    endpoint: str,
    board: Board,
    target: SleeperBrowserTarget,
    timeout_ms: int = 15_000,
) -> SleeperPlaywrightSession:
    """Click only approved live-league DRAFTROOM, then attach to the room.

    This is a distinct preflight action, never a mock ``START DRAFT`` action.
    It first proves the exact league's pre-draft route and visible account/name,
    then waits for Sleeper's draft-room URL.  The returned transport will sit
    safely in ``waiting`` until Sleeper opens an actual pick clock.
    """

    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    playwright, browser = _connect_browser(endpoint)
    try:
        expected_route = f"/leagues/{target.league_id}/predraft"
        pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if "sleeper.com" in page.url and expected_route in page.url
        ]
        if len(pages) != 1:
            raise SleeperPlaywrightTransportError(
                "Expected exactly one approved Sleeper pre-draft tab before live DRAFTROOM navigation; "
                f"found {len(pages)}."
            )
        page = pages[0]
        try:
            body_text = page.locator("body").inner_text(timeout=timeout_ms)
        except Exception as error:
            raise SleeperPlaywrightTransportError(
                f"Could not read the approved Sleeper pre-draft page: {type(error).__name__}."
            ) from error
        if target.league_name not in body_text or not _contains_exact_visible_text(body_text, target.username):
            raise SleeperPlaywrightTransportError(
                "Visible Sleeper league or account did not match the approved live-draft target."
            )
        room_control = page.get_by_text("DRAFTROOM", exact=True)
        if room_control.count() != 1 or not room_control.is_visible():
            raise SleeperPlaywrightTransportError("Visible DRAFTROOM control was not uniquely available.")
        room_control.click()
        page.wait_for_url("**/draft/nfl/**", timeout=timeout_ms)
        return SleeperPlaywrightSession(
            transport=SleeperPlaywrightTransport(page, board, target),
            _playwright=playwright,
        )
    except Exception:
        playwright.stop()
        raise


def _connect_browser(endpoint: str) -> tuple[Any, Any]:
    try:
        sync_api = importlib.import_module("playwright.sync_api")
    except ModuleNotFoundError as error:
        raise SleeperPlaywrightTransportError(
            "Playwright is not installed. Install this package with its optional browser extra on the dedicated host."
        ) from error
    playwright = sync_api.sync_playwright().start()
    try:
        return playwright, playwright.chromium.connect_over_cdp(endpoint)
    except Exception:
        playwright.stop()
        raise


class SleeperPlaywrightTransport:
    """Implement the exact browser transport contract with CDP/visible DOM.

    ``page`` is intentionally duck-typed so parser and action safeguards are
    fully testable without Playwright installed.  Production supplies a
    Playwright sync ``Page`` instance through ``connect_local_cdp``.
    """

    def __init__(self, page: Any, board: Board, target: SleeperBrowserTarget) -> None:
        self.page = page
        self.board = board
        self.target = target

    def observe_visible_draft_room(self) -> BrowserObservation:
        """Read one coherent visible-DOM snapshot and normalize it fail-closed."""

        try:
            raw = self.page.evaluate(_DOM_SNAPSHOT_SCRIPT)
        except Exception as error:
            return self._blocked_observation(
                BrowserBlockerKind.UNRESPONSIVE,
                f"Sleeper DOM read failed: {type(error).__name__}.",
            )
        if not isinstance(raw, Mapping):
            return self._blocked_observation(
                BrowserBlockerKind.UNRESPONSIVE,
                "Sleeper DOM read returned no structured snapshot.",
            )
        return observation_from_dom_snapshot(raw, self.board, self.target)

    def reveal_player_row(self, player_name: str) -> None:
        """Use Sleeper's named player search; this never submits a draft pick."""

        try:
            search = self.page.locator(_SEARCH_INPUT)
            if search.count() != 1:
                raise SleeperPlaywrightTransportError("Sleeper player search control was not uniquely visible.")
            search.fill(player_name)
            row = self._named_row(player_name)
            row.wait_for(state="visible", timeout=2_000)
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not reveal the exact Sleeper row for {player_name!r}: {type(error).__name__}."
            ) from error

    def click_exact_draft(self, player_name: str) -> None:
        """Click only one live row's semantic Sleeper DRAFT control."""

        self.reveal_player_row(player_name)
        try:
            row = self._named_row(player_name)
            button = row.locator(_DRAFT_BUTTON)
            if button.count() != 1 or not button.is_visible():
                raise SleeperPlaywrightTransportError(
                    f"Sleeper did not expose exactly one visible DRAFT button for {player_name!r}."
                )
            button.click()
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not click Sleeper's exact DRAFT button for {player_name!r}: {type(error).__name__}."
            ) from error

    def set_auto_pick_off(self) -> None:
        """Issue the one permitted auto-pick action: visible TURN OFF control."""

        try:
            button = self.page.get_by_role("button", name=_AUTO_PICK_OFF, exact=True)
            if button.count() != 1 or not button.is_visible():
                raise SleeperPlaywrightTransportError("Visible TURN OFF AUTO-PICK control was not unique.")
            button.click()
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not issue the targeted Sleeper auto-pick disable: {type(error).__name__}."
            ) from error

    def _named_row(self, player_name: str) -> Any:
        """Return one row only after text confirms the intended player name."""

        expected = normalize_name(player_name)
        rows = self.page.locator(_PLAYER_ROW)
        matching: list[Any] = []
        for index in range(rows.count()):
            row = rows.nth(index)
            text = row.inner_text()
            resolved = _resolve_board_player(text, self.board)
            if resolved is not None and normalize_name(resolved) == expected:
                matching.append(row)
        if len(matching) != 1:
            raise SleeperPlaywrightTransportError(
                f"Expected exactly one currently rendered Sleeper row for {player_name!r}; found {len(matching)}."
            )
        return matching[0]

    def _blocked_observation(self, kind: BrowserBlockerKind, summary: str) -> BrowserObservation:
        return BrowserObservation(
            league_id=self.target.league_id,
            username=self.target.username,
            draft_status="unknown",
            auto_pick_enabled=False,
            round_number=0,
            pick_label="0.00",
            current_pick_number=0,
            our_pick_number=0,
            roster_positions=(),
            drafted_players=frozenset(),
            blocker=BrowserBlocker(kind, summary),
        )


def observation_from_dom_snapshot(
    raw: Mapping[str, Any], board: Board, target: SleeperBrowserTarget
) -> BrowserObservation:
    """Pure parser for the strictly visible DOM payload used by the transport."""

    body_text = str(raw.get("bodyText", ""))
    url = str(raw.get("url", ""))
    cells = _as_mappings(raw.get("draftCells"))
    rows = _as_mappings(raw.get("playerRows"))

    if target.league_name not in body_text or not _contains_exact_visible_text(body_text, target.username):
        return _blocked(target, BrowserBlockerKind.IDENTITY_MISMATCH, "Visible Sleeper league or account did not match the approved target.")
    if _DRAFT_ROUTE not in url:
        if "DRAFTROOM" in body_text:
            return _blocked(
                target,
                BrowserBlockerKind.LIVE_DRAFTROOM_READY,
                "Approved pre-draft page exposes DRAFTROOM; navigation is required before drafting can begin.",
                ("DRAFTROOM",),
            )
        return _blocked(target, BrowserBlockerKind.UNEXPECTED_DIALOG, "Sleeper is not on the approved draft-room route.")
    if _has_unknown_overlay(raw, body_text):
        return _blocked(target, BrowserBlockerKind.UNEXPECTED_DIALOG, "Sleeper reported an unexpected visible dialog or overlay.")

    current_pick, clock_remaining_ms = _current_pick_and_clock(cells)
    if current_pick is None or clock_remaining_ms is None:
        if "Draft Complete" in body_text or "DRAFT COMPLETE" in body_text:
            return _observation(
                target,
                draft_status="completed",
                current_pick_number=target.teams * 18,
                clock_remaining_ms=None,
                auto_pick_enabled=_auto_pick_is_enabled(body_text),
                drafted_players=_drafted_source_players(cells, board),
                roster_positions=_roster_positions(cells, board, target),
                row_actions=_row_actions(rows, board),
            )
        if cells:
            # A live room is allowed to exist before Sleeper opens the first
            # clock.  The continuous worker waits at its slower cadence; it
            # does not mistake an expected pre-clock board for an actionable
            # pick or an arbitrary pending request.
            return _observation(
                target,
                draft_status="waiting",
                current_pick_number=0,
                clock_remaining_ms=None,
                auto_pick_enabled=_auto_pick_is_enabled(body_text),
                drafted_players=_drafted_source_players(cells, board),
                roster_positions=_roster_positions(cells, board, target),
                row_actions=_row_actions(rows, board),
            )
        return _blocked(target, BrowserBlockerKind.PENDING_REQUEST, "No active draft-cell countdown was exposed by Sleeper.")

    drafted = _drafted_source_players(cells, board)
    roster = _roster_positions(cells, board, target)
    return _observation(
        target,
        draft_status="drafting",
        current_pick_number=current_pick,
        clock_remaining_ms=clock_remaining_ms,
        auto_pick_enabled=_auto_pick_is_enabled(body_text),
        drafted_players=drafted,
        roster_positions=roster,
        row_actions=_row_actions(rows, board),
    )


def _observation(
    target: SleeperBrowserTarget,
    *,
    draft_status: str,
    current_pick_number: int,
    clock_remaining_ms: int | None,
    auto_pick_enabled: bool,
    drafted_players: frozenset[str],
    roster_positions: tuple[str, ...],
    row_actions: dict[str, frozenset[PlayerRowAction]],
) -> BrowserObservation:
    round_number = max(1, (max(1, current_pick_number) - 1) // target.teams + 1)
    pick_in_round = (max(1, current_pick_number) - 1) % target.teams + 1
    return BrowserObservation(
        league_id=target.league_id,
        username=target.username,
        draft_status=draft_status,
        auto_pick_enabled=auto_pick_enabled,
        round_number=round_number,
        pick_label=f"{round_number}.{pick_in_round:02d}",
        current_pick_number=current_pick_number,
        our_pick_number=_next_our_pick(current_pick_number, target),
        roster_positions=roster_positions,
        drafted_players=drafted_players,
        clock_remaining_ms=clock_remaining_ms,
        clock_precision_ms=1_000 if clock_remaining_ms is not None else None,
        available_players=None,
        player_row_actions=row_actions,
    )


def _blocked(
    target: SleeperBrowserTarget,
    kind: BrowserBlockerKind,
    summary: str,
    action_labels: tuple[str, ...] = (),
) -> BrowserObservation:
    return BrowserObservation(
        league_id=target.league_id,
        username=target.username,
        draft_status="unknown",
        auto_pick_enabled=False,
        round_number=0,
        pick_label="0.00",
        current_pick_number=0,
        our_pick_number=0,
        roster_positions=(),
        drafted_players=frozenset(),
        blocker=BrowserBlocker(kind, summary, action_labels),
    )


def _as_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _contains_exact_visible_text(text: str, value: str) -> bool:
    return any(line.strip().casefold() == value.casefold() for line in text.splitlines())


def _has_unknown_overlay(raw: Mapping[str, Any], body_text: str) -> bool:
    if bool(raw.get("hasUnexpectedDialog")):
        return True
    return "NEW MOCK DRAFT" in body_text and "START DRAFT" not in body_text


def _auto_pick_is_enabled(body_text: str) -> bool:
    return "You are on auto-pick." in body_text or _AUTO_PICK_OFF in body_text


def _current_pick_and_clock(cells: Sequence[Mapping[str, Any]]) -> tuple[int | None, int | None]:
    matches: list[tuple[int, int]] = []
    for cell in cells:
        identifier = str(cell.get("id", ""))
        cell_id = _CELL_ID.match(identifier)
        if cell_id is None:
            continue
        clock = _CLOCK.search(str(cell.get("text", "")))
        if clock is None:
            continue
        remaining_ms = (int(clock["minutes"]) * 60 + int(clock["seconds"])) * 1_000
        matches.append((int(cell_id["pick"]), remaining_ms))
    return matches[0] if len(matches) == 1 else (None, None)


def _drafted_source_players(cells: Sequence[Mapping[str, Any]], board: Board) -> frozenset[str]:
    drafted: set[str] = set()
    for cell in cells:
        classes = {str(value) for value in cell.get("classes", ())}
        if "drafted" not in classes:
            continue
        resolved = _resolve_board_player(str(cell.get("text", "")), board)
        if resolved is not None:
            drafted.add(resolved)
    return frozenset(drafted)


def _roster_positions(
    cells: Sequence[Mapping[str, Any]], board: Board, target: SleeperBrowserTarget
) -> tuple[str, ...]:
    positions: list[str] = []
    for cell in cells:
        cell_id = _CELL_ID.match(str(cell.get("id", "")))
        if cell_id is None or int(cell_id["pick"]) not in _our_pick_numbers(target, rounds=18):
            continue
        text = str(cell.get("text", ""))
        if (player_name := _resolve_board_player(text, board)) is not None:
            positions.append(board.by_normalized_name[normalize_name(player_name)].position)
        elif (position := _POSITION.search(text)) is not None:
            positions.append(position["position"].upper())
    return tuple(positions)


def _row_actions(rows: Sequence[Mapping[str, Any]], board: Board) -> dict[str, frozenset[PlayerRowAction]]:
    actions: dict[str, frozenset[PlayerRowAction]] = {}
    for row in rows:
        if (name := _resolve_board_player(str(row.get("text", "")), board)) is None:
            continue
        visible_actions: set[PlayerRowAction] = set()
        if bool(row.get("hasDraftButton")):
            visible_actions.add(PlayerRowAction.DRAFT)
        if bool(row.get("hasQueueButton")):
            visible_actions.add(PlayerRowAction.QUEUE)
        if bool(row.get("hasDetailsControl")):
            visible_actions.add(PlayerRowAction.DETAILS)
        actions[name] = frozenset(visible_actions)
    return actions


def _resolve_board_player(text: str, board: Board) -> str | None:
    normalized_text = normalize_name(text)
    exact: list[str] = []
    abbreviated: list[str] = []
    for player in board.players:
        normalized_player = normalize_name(player.name)
        if normalized_player and normalized_player in normalized_text:
            exact.append(player.name)
            continue
        name_parts = re.findall(r"[a-z0-9]+", player.name.casefold())
        if len(name_parts) >= 2 and normalize_name(f"{name_parts[0][0]} {name_parts[-1]}") in normalized_text:
            abbreviated.append(player.name)
    candidates = exact or abbreviated
    return candidates[0] if len(candidates) == 1 else None


def _our_pick_numbers(target: SleeperBrowserTarget, rounds: int) -> frozenset[int]:
    picks: set[int] = set()
    for round_number in range(1, rounds + 1):
        within_round = target.draft_slot if round_number % 2 else target.teams + 1 - target.draft_slot
        picks.add((round_number - 1) * target.teams + within_round)
    return frozenset(picks)


def _next_our_pick(current_pick_number: int, target: SleeperBrowserTarget) -> int:
    return next(
        (pick for pick in sorted(_our_pick_numbers(target, rounds=18)) if pick >= current_pick_number),
        target.teams * 18 + 1,
    )


_DOM_SNAPSHOT_SCRIPT = f"""() => {{
  const text = (node) => (node && node.innerText ? node.innerText : '').trim();
  const rows = Array.from(document.querySelectorAll('{_PLAYER_ROW}')).map((row) => ({{
    text: text(row),
    hasDraftButton: Boolean(row.querySelector('{_DRAFT_BUTTON}')),
    hasQueueButton: Boolean(row.querySelector('[class*=queue], [data-action=queue]')),
    hasDetailsControl: Boolean(row.querySelector('a, [class*=player-card], [data-action=details]')),
  }}));
  const draftCells = Array.from(document.querySelectorAll('[id^="draft-cell-"]')).map((cell) => ({{
    id: cell.id,
    text: text(cell),
    classes: Array.from(cell.classList),
  }}));
  return {{
    url: window.location.href,
    bodyText: text(document.body),
    draftCells,
    playerRows: rows,
    hasUnexpectedDialog: Boolean(document.querySelector('[role=dialog][aria-modal=true]')),
  }};
}}"""
