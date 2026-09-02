"""Optional local-CDP transport for Sleeper's visible browser DOM.

The core package does not require Playwright.  When installed as the optional
``browser`` extra, a host process may attach to a browser the user already
opened and authenticated locally.  This adapter never launches a browser,
reads cookies, stores a profile, calls Sleeper's private APIs, or clicks a
coordinate.  It works only through visible DOM text and semantic controls.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import importlib
import json
from pathlib import Path
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..board import Board, canonical_name_key, normalize_name, sleeper_search_name
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
_FULL_DRAFT_ROSTER = re.compile(r"\bAll\s*18\s*/\s*18\b", re.IGNORECASE)

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
    visual_audit_dir: Path | None = None,
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
            transport=SleeperPlaywrightTransport(pages[0], board, target, visual_audit_dir),
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
    visual_audit_dir: Path | None = None,
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
            transport=SleeperPlaywrightTransport(page, board, target, visual_audit_dir),
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

    def __init__(
        self,
        page: Any,
        board: Board,
        target: SleeperBrowserTarget,
        visual_audit_dir: Path | None = None,
    ) -> None:
        self.page = page
        self.board = board
        self.target = target
        # Opt-in diagnostic recorder.  It observes the rendered page only; it
        # never supplies an action target.  Keeping it off by default avoids
        # adding screenshot latency to a live pick.
        self.visual_audit_dir = visual_audit_dir
        self._last_visual_key: tuple[Any, ...] | None = None
        # Populated only from the currently visible Sleeper K/DEF filter.
        # It is intentionally not a hand-maintained ranking list.
        self._sleeper_ranked_specialists: dict[str, tuple[str, ...]] = {}

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
        observation = observation_from_dom_snapshot(raw, self.board, self.target)
        required_position = self._required_specialist_position(observation)
        if required_position and not self._sleeper_ranked_specialists.get(required_position):
            self._capture_visible_specialist_ranks(required_position)
        if self._sleeper_ranked_specialists:
            # Sleeper abbreviates completed specialist cells (for example
            # ``D. Lions``), while the ranked filter exposes the full name.
            # Reconcile only against that already-rendered specialist list so
            # post-click confirmation can prove the exact K/DST was drafted.
            specialist_drafted = _known_specialist_drafted(
                _as_mappings(raw.get("draftCells")), self._sleeper_ranked_specialists
            )
            if specialist_drafted:
                observation = replace(
                    observation,
                    drafted_players=frozenset(set(observation.drafted_players) | specialist_drafted),
                )
        if self._sleeper_ranked_specialists:
            observation = replace(
                observation,
                sleeper_ranked_specialists=dict(self._sleeper_ranked_specialists),
            )
        self._record_visual_audit(observation)
        return observation

    @staticmethod
    def _required_specialist_position(observation: BrowserObservation) -> str | None:
        """Return the only Sleeper position filter we may switch to now."""

        if observation.draft_status != "drafting":
            return None
        roster = {position.upper() for position in observation.roster_positions}
        if observation.round_number == 17 and "DEF" not in roster:
            return "DEF"
        if observation.round_number == 18 and "K" not in roster:
            return "K"
        return None

    def _capture_visible_specialist_ranks(self, position: str) -> None:
        """Select Sleeper's visible K/DEF chip and cache its rendered order.

        This is a semantic filter interaction, never a coordinate click or an
        API call.  A missing/ambiguous filter intentionally leaves the cache
        empty so the late-round policy cannot guess a specialist.
        """

        try:
            # A prior ordinary pick leaves its player name in this search
            # field. Clear it before reading K/DEF, otherwise the position
            # filter has no rendered rows to rank.
            search = self.page.locator(_SEARCH_INPUT)
            if search.count() == 1:
                search.fill("", timeout=500)
            clicked = self.page.evaluate(_SELECT_POSITION_FILTER_SCRIPT, position)
            if not clicked:
                return
            # Sleeper's virtualized player list can take a few frames to
            # replace after a K/DEF filter change. This bounded wait is only
            # used in R17/R18, never on an ordinary timed pick.
            self.page.wait_for_timeout(350)
            raw = self.page.evaluate(_DOM_SNAPSHOT_SCRIPT)
            if not isinstance(raw, Mapping):
                return
            names = _visible_specialist_names(_as_mappings(raw.get("playerRows")), position)
            if names:
                self._sleeper_ranked_specialists[position] = names
        except Exception:
            # A specialist rank read is advisory input.  The policy remains
            # fail-closed rather than selecting from an unverified fallback.
            return

    def _record_visual_audit(self, observation: BrowserObservation) -> None:
        """Capture rendered evidence on state transitions when explicitly enabled.

        The screenshot is evidence, not a source of truth for player entry:
        the worker still uses the semantic DOM control to submit a pick.  This
        lets post-mortems answer whether the visible clock, active pick, and
        draft state agreed at the moment the worker acted.
        """

        if self.visual_audit_dir is None:
            return
        screenshot = getattr(self.page, "screenshot", None)
        if not callable(screenshot):
            return
        remaining = observation.clock_remaining_ms
        # Capture the opening state and meaningful clock edges, not every
        # 250-ms poll.  Exact remaining time stays in the sidecar metadata.
        clock_phase = (
            "none"
            if remaining is None
            else next((threshold for threshold in (120_000, 60_000, 30_000, 10_000, 0) if remaining <= threshold), 120_000)
        )
        key = (
            observation.draft_status,
            observation.current_pick_number,
            observation.pick_label,
            clock_phase,
            observation.auto_pick_enabled,
            tuple(sorted(observation.drafted_players)),
        )
        if key == self._last_visual_key:
            return
        self._last_visual_key = key
        self.visual_audit_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        stem = f"{stamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{observation.current_pick_number:03d}"
        image_path = self.visual_audit_dir / f"{stem}.png"
        metadata_path = self.visual_audit_dir / f"{stem}.json"
        try:
            screenshot(path=str(image_path), animations="disabled")
            metadata_path.write_text(
                json.dumps(
                    {
                        "captured_at": stamp.isoformat(),
                        "image": image_path.name,
                        "draft_status": observation.draft_status,
                        "pick_label": observation.pick_label,
                        "current_pick_number": observation.current_pick_number,
                        "clock_remaining_ms": observation.clock_remaining_ms,
                        "auto_pick_enabled": observation.auto_pick_enabled,
                        "blocker": observation.blocker.summary if observation.blocker else None,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception:
            # Diagnostics must never block or change the draft decision path.
            # Leave the transition key advanced so a broken screenshot backend
            # cannot repeatedly consume the pick clock.
            return

    def reveal_player_row(self, player_name: str) -> None:
        """Use Sleeper's named player search; this never submits a draft pick."""

        try:
            search = self.page.locator(_SEARCH_INPUT)
            if search.count() != 1:
                raise SleeperPlaywrightTransportError("Sleeper player search control was not uniquely visible.")
            search.fill(self._search_query(player_name), timeout=500)
            row = self._wait_for_named_row(player_name)
            row.wait_for(state="visible", timeout=500)
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not reveal the exact Sleeper row for {player_name!r}: {type(error).__name__}."
            ) from error

    def click_exact_draft(self, player_name: str) -> None:
        """Click only one live row's semantic Sleeper DRAFT control."""

        try:
            search = self.page.locator(_SEARCH_INPUT)
            search.fill(self._search_query(player_name), timeout=500)
            row = self._wait_for_named_row(player_name)
            # This is a readiness assertion, not a polling loop. On an
            # already rendered search result it completes immediately and
            # ensures the semantic DRAFT control belongs to a visible row.
            row.wait_for(state="visible", timeout=500)
            # Sleeper's 24px plus control is a div.  Once the exact searched
            # row exists, submit one forced click immediately; this is the
            # latency-critical action and intentionally has no readiness loop.
            row.locator(_DRAFT_BUTTON).click(timeout=750, force=True)
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not click Sleeper's exact DRAFT button for {player_name!r}: {type(error).__name__}."
            ) from error

    def set_auto_pick_off(self) -> None:
        """Issue the one permitted auto-pick action: visible TURN OFF control."""

        try:
            control = self.page.get_by_text(_AUTO_PICK_OFF, exact=True)
            if control.count() != 1 or not control.is_visible():
                raise SleeperPlaywrightTransportError("Visible TURN OFF AUTO-PICK control was not unique.")
            control.click(timeout=750, force=True)
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not issue the targeted Sleeper auto-pick disable: {type(error).__name__}."
            ) from error

    def dismiss_player_card(self) -> bool:
        """Close a verified player-details card and prove it disappeared.

        Escape is a native, non-destructive modal close. It is used only after
        the visible snapshot identifies Sleeper's player-details card, never
        for a generic cookie, terms, or confirmation dialog.
        """

        try:
            before = self.page.evaluate(_DOM_SNAPSHOT_SCRIPT)
            if not isinstance(before, Mapping) or not bool(before.get("playerCardOpen")):
                return False
            self.page.keyboard.press("Escape")
            after = self.page.evaluate(_DOM_SNAPSHOT_SCRIPT)
            if not isinstance(after, Mapping) or bool(after.get("playerCardOpen")):
                raise SleeperPlaywrightTransportError("Sleeper player card remained open after Escape.")
            return True
        except Exception as error:
            if isinstance(error, SleeperPlaywrightTransportError):
                raise
            raise SleeperPlaywrightTransportError(
                f"Could not dismiss the verified Sleeper player card: {type(error).__name__}."
            ) from error

    def _named_row(self, player_name: str) -> Any:
        """Return one row only after text confirms the intended player name."""

        # Sleeper's visible spelling occasionally differs from the board
        # (e.g. Jonathon/Jonathan Brooks).  Resolve identity with the same
        # canonical key the board uses, but keep the requested spelling only
        # for diagnostics and the search box.
        expected = canonical_name_key(player_name)
        rows = self.page.locator(_PLAYER_ROW)
        matching: list[Any] = []
        for index in range(rows.count()):
            row = rows.nth(index)
            try:
                text = row.inner_text(timeout=150)
            except Exception as error:
                # React can replace the virtualized row between the count and
                # text read immediately after a search.  Normalize that short
                # rendering gap so the bounded caller can retry the lookup;
                # never reinterpret it as permission to click another row.
                raise SleeperPlaywrightTransportError(
                    "Sleeper player row was replaced while its visible text was being read."
                ) from error
            resolved = _resolve_board_player(text, self.board)
            if resolved is not None and canonical_name_key(resolved) == expected:
                matching.append(row)
            elif resolved is None:
                # K/DST are deliberately sourced from Sleeper's visible rank,
                # not from the analyst board.  Their row is already narrowed
                # by the relevant position filter, so accept only a literal
                # displayed name (or the final team token for a DST such as
                # ``Los Angeles Chargers`` rendered as ``L. Chargers``).
                normalized_text = normalize_name(text)
                full_name = normalize_name(player_name)
                team_tail = normalize_name(player_name.rsplit(" ", 1)[-1])
                if full_name in normalized_text or (
                    team_tail and team_tail in normalized_text and _POSITION.search(text)
                ):
                    matching.append(row)
        if len(matching) != 1:
            raise SleeperPlaywrightTransportError(
                f"Expected exactly one currently rendered Sleeper row for {player_name!r}; found {len(matching)}."
            )
        return matching[0]

    def _search_query(self, player_name: str) -> str:
        """Use a Sleeper-compatible query for board players and specialists."""

        canonical = canonical_name_key(player_name)
        if canonical in self.board.by_normalized_name:
            return sleeper_search_name(player_name)
        # Sleeper abbreviates team defenses in the visible grid.  Their final
        # team word is unique after the DEF filter has been selected.
        return player_name.rsplit(" ", 1)[-1]

    def _wait_for_named_row(self, player_name: str) -> Any:
        """Allow the virtualized Sleeper table a bounded time to apply search.

        The search input can update just before React renders the matching row.
        This is intentionally a tiny bounded retry, not a generic retry of a
        draft action: it only proves that the already named row exists.
        """

        last_error: SleeperPlaywrightTransportError | None = None
        # The player table is virtualized and can take several render frames
        # to replace its prior row after a name search.  This is still a
        # bounded, non-submitting preparation step: a one-second allowance is
        # negligible against a two-minute clock and eliminates the observed
        # React search-render race without retrying a draft action.
        for attempt in range(14):
            try:
                return self._named_row(player_name)
            except SleeperPlaywrightTransportError as error:
                last_error = error
                if attempt == 13:
                    break
                self.page.wait_for_timeout(75)
        assert last_error is not None
        raise last_error

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
    if bool(raw.get("playerCardOpen")):
        return _blocked(
            target,
            BrowserBlockerKind.PLAYER_CARD,
            "Sleeper player-details card is open; dismiss the verified card before continuing.",
            ("Dismiss player card",),
        )
    if dialog_summary := _dialog_blocker_summary(raw, body_text):
        return _blocked(target, BrowserBlockerKind.UNEXPECTED_DIALOG, dialog_summary)

    current_pick, clock_remaining_ms = _current_pick_and_clock(cells)
    if current_pick is None or clock_remaining_ms is None:
        if "Draft Complete" in body_text or "DRAFT COMPLETE" in body_text or _FULL_DRAFT_ROSTER.search(body_text):
            return _observation(
                target,
                draft_status="completed",
                current_pick_number=target.teams * 18,
                clock_remaining_ms=None,
                auto_pick_enabled=_auto_pick_is_enabled(body_text),
                drafted_players=_drafted_source_players(cells, board),
                roster_positions=_roster_positions(cells, board, target),
                row_actions=_row_actions(rows, board),
                recent_pick_positions=_recent_pick_positions(cells),
                market_adp=_market_metrics(rows, board)[0],
                projected_points=_market_metrics(rows, board)[1],
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
                recent_pick_positions=_recent_pick_positions(cells),
                market_adp=_market_metrics(rows, board)[0],
                projected_points=_market_metrics(rows, board)[1],
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
        recent_pick_positions=_recent_pick_positions(cells),
        market_adp=_market_metrics(rows, board)[0],
        projected_points=_market_metrics(rows, board)[1],
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
    recent_pick_positions: tuple[str, ...] = (),
    market_adp: dict[str, float] | None = None,
    projected_points: dict[str, float] | None = None,
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
        recent_pick_positions=recent_pick_positions,
        market_adp=market_adp or {},
        projected_points=projected_points or {},
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


def _visible_specialist_names(rows: Sequence[Mapping[str, Any]], position: str) -> tuple[str, ...]:
    """Extract the ordered names from Sleeper's rendered K or DEF rows."""

    expected = position.upper()
    names: list[str] = []
    for row in rows:
        lines = [line.strip() for line in str(row.get("text", "")).splitlines() if line.strip()]
        position_line = next(
            (
                index
                for index, line in enumerate(lines)
                if re.fullmatch(rf"{re.escape(expected)}(?:\s*-.*)?", line, flags=re.IGNORECASE)
            ),
            None,
        )
        if position_line is None or position_line == 0:
            continue
        name = lines[position_line - 1]
        # Do not promote an ellipsized virtual-row label into an executable
        # player name. Sleeper normally renders all specialist names in full.
        if name and "…" not in name and "..." not in name and name not in names:
            names.append(name)
    return tuple(names)


def _contains_exact_visible_text(text: str, value: str) -> bool:
    return any(line.strip().casefold() == value.casefold() for line in text.splitlines())


def _dialog_blocker_summary(raw: Mapping[str, Any], body_text: str) -> str | None:
    dialog_text = "\n".join(str(value) for value in raw.get("dialogTexts", ()) if value)
    if "Cookies and Personal Information" in dialog_text:
        return "Cookie-consent dialog is visible; a user privacy choice is required before drafting can continue."
    if bool(raw.get("hasUnexpectedDialog")):
        return "Sleeper reported an unexpected visible dialog or overlay."
    if "NEW MOCK DRAFT" in body_text and "START DRAFT" not in body_text:
        return "Mock creation remained in an unresolved request state."
    return None


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


def _known_specialist_drafted(
    cells: Sequence[Mapping[str, Any]], specialists: Mapping[str, Sequence[str]]
) -> set[str]:
    """Resolve only completed K/DST cells against Sleeper's visible rank cache."""

    drafted: set[str] = set()
    for cell in cells:
        if "drafted" not in {str(value) for value in cell.get("classes", ())}:
            continue
        text = str(cell.get("text", ""))
        position_match = _POSITION.search(text)
        if position_match is None:
            continue
        position = position_match["position"].upper()
        if position not in {"K", "DEF"}:
            continue
        normalized_text = normalize_name(text)
        matches = [
            name
            for name in specialists.get(position, ())
            if normalize_name(name) in normalized_text
            or _sleeper_abbreviation(name) in normalized_text
        ]
        if len(matches) == 1:
            drafted.add(matches[0])
    return drafted


def _recent_pick_positions(cells: Sequence[Mapping[str, Any]], window: int = 12) -> tuple[str, ...]:
    """Read draft-cell positions in chronological pick order for run analysis."""

    chronological: list[tuple[int, str]] = []
    for cell in cells:
        cell_id = _CELL_ID.match(str(cell.get("id", "")))
        classes = {str(value) for value in cell.get("classes", ())}
        if cell_id is None or "drafted" not in classes:
            continue
        position = _POSITION.search(str(cell.get("text", "")))
        if position is not None:
            chronological.append((int(cell_id["pick"]), position["position"].upper()))
    return tuple(position for _, position in sorted(chronological)[-window:])


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
        text = str(row.get("text", ""))
        name = _resolve_board_player(text, board)
        if name is None:
            # K/DST candidates deliberately do not live in the analyst
            # board: their order comes only from Sleeper's rendered
            # specialist filter in R17/R18.  Retain their literal visible
            # identity here so the browser safety boundary can prove that
            # *this exact row* has a DRAFT control before transport clicks
            # it.  Other unknown player rows remain excluded.
            positions = tuple(
                position
                for position in ("K", "DEF")
                if _visible_specialist_names(({"text": text},), position)
            )
            if len(positions) != 1:
                continue
            names = _visible_specialist_names(({"text": text},), positions[0])
            if len(names) != 1:
                continue
            name = names[0]
        if not name:
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


def _market_metrics(rows: Sequence[Mapping[str, Any]], board: Board) -> tuple[dict[str, float], dict[str, float]]:
    """Extract only visible Sleeper ADP and projected-points columns.

    Sleeper virtualizes the table, so this is intentionally a partial snapshot:
    the VBD policy stays neutral unless it has enough position peers. The
    continuous lookahead worker can accumulate these observations off-clock.
    """

    adp: dict[str, float] = {}
    projections: dict[str, float] = {}
    for row in rows:
        text = str(row.get("text", ""))
        if (name := _resolve_board_player(text, board)) is None:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        try:
            name_index = next(index for index, line in enumerate(lines) if normalize_name(line) == normalize_name(name))
            # Sleeper's visible order is Player, Position, Team, ADP, Bye,
            # Projected Points, Average. Reject malformed or shifted rows.
            adp_value = float(lines[name_index + 3])
            points_value = float(lines[name_index + 5])
        except (IndexError, StopIteration, ValueError):
            continue
        if adp_value >= 0 and points_value >= 0:
            adp[name] = adp_value
            projections[name] = points_value
    return adp, projections


def _resolve_board_player(text: str, board: Board) -> str | None:
    normalized_text = normalize_name(text)
    # Include every platform-facing board alias in the initial exact-text
    # lookup.  A later canonical comparison is too late: if Sleeper calls a
    # player ``Jonathon`` and the board calls him ``Jonathan``, the old
    # resolver returned None before identity reconciliation could run.
    exact = {
        player.name
        for platform_name, player in board.by_normalized_name.items()
        if platform_name and platform_name in normalized_text
    }
    abbreviated: list[str] = []
    for player in board.players:
        normalized_player = normalize_name(player.name)
        if normalized_player and normalized_player in normalized_text:
            exact.add(player.name)
            continue
        if _sleeper_abbreviation(player.name) in normalized_text:
            abbreviated.append(player.name)
    candidates = sorted(exact) if exact else abbreviated
    if not candidates:
        # Sleeper truncates long drafted-cell labels (for example
        # ``J. Smith-N...``).  Treat the visible pre-ellipsis text as a fuzzy
        # VLOOKUP key only when it matches exactly one board abbreviation.  A
        # short/common label, missing ellipsis, or position ambiguity remains
        # unresolved rather than being guessed.
        prefixes = tuple(
            normalize_name(line.split("...", maxsplit=1)[0].split("…", maxsplit=1)[0])
            for line in text.splitlines()
            if "..." in line or "…" in line
        )
        fuzzy = [
            player.name
            for player in board.players
            if any(
                len(prefix) >= 5 and _sleeper_abbreviation(player.name).startswith(prefix)
                for prefix in prefixes
            )
        ]
        candidates = fuzzy
    if len(candidates) > 1 and (position := _POSITION.search(text)) is not None:
        candidates = [
            name
            for name in candidates
            if board.by_normalized_name[normalize_name(name)].position == position["position"].upper()
        ]
    return candidates[0] if len(candidates) == 1 else None


def _sleeper_abbreviation(player_name: str) -> str:
    """Return Sleeper's compact first-initial/family-name form.

    Multi-word family names retain their family particle (``St. Brown``), while
    suffixes such as ``Jr.`` are ignored. This prevents `A. Brown` from being
    confused with `A. St. Brown` when reconciling drafted cells.
    """

    parts = re.findall(r"[a-z0-9]+", player_name.casefold())
    if len(parts) < 2:
        return ""
    # Sleeper keeps every component of a hyphenated family name in its
    # abbreviated cells (``J. Smith-N...``).  ``re.findall`` above separates
    # those components, so preserve the final hyphenated word as one family
    # unit before applying the ordinary particle logic below.
    raw_words = player_name.casefold().split()
    if raw_words and "-" in raw_words[-1]:
        family = " ".join(re.findall(r"[a-z0-9]+", raw_words[-1]))
        return normalize_name(f"{parts[0][0]} {family}")
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    family_end = len(parts) - 1
    while family_end > 0 and parts[family_end] in suffixes:
        family_end -= 1
    family_start = family_end
    # A particle immediately after the first token can be part of a compound
    # given name (for example ``De'Von Achane``), not the family name. Keep
    # particles only when at least two given-name tokens precede them, as in
    # ``Amon-Ra St. Brown``.
    if family_start > 2 and parts[family_start - 1] in {"st", "van", "von", "de", "del", "la", "le"}:
        family_start -= 1
    return normalize_name(f"{parts[0][0]} {' '.join(parts[family_start:family_end + 1])}")


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
  const isVisible = (node) => {{
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
  }};
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
  const playerCardOpen = Array.from(document.querySelectorAll('[role=dialog], [class*=modal], [class*=drawer]'))
    .filter(isVisible)
    .some((overlay) => /GAME LOGS|PLAYER RANKINGS|LATEST NEWS/.test(text(overlay)));
  return {{
    url: window.location.href,
    bodyText: text(document.body),
    draftCells,
    playerRows: rows,
    playerCardOpen,
    // Cookie providers can leave a zero-sized, "visible" role=dialog in the
    // DOM after the user has dismissed the banner.  It is not an interaction
    // blocker.  Only report dialogs that have an on-screen rendering box;
    // real modals still stop the worker before it can draft.
    hasUnexpectedDialog: Array.from(document.querySelectorAll('[role=dialog][aria-modal=true]')).some((dialog) => {{
      const rect = dialog.getBoundingClientRect();
      const style = window.getComputedStyle(dialog);
      return rect.width > 1 && rect.height > 1 &&
        style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
    }}),
    dialogTexts: Array.from(document.querySelectorAll('[role=dialog]'))
      .filter((dialog) => {{
        const rect = dialog.getBoundingClientRect();
        const style = window.getComputedStyle(dialog);
        return rect.width > 1 && rect.height > 1 &&
          style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
      }})
      .map(text),
  }};
}}"""


_SELECT_POSITION_FILTER_SCRIPT = """(position) => {
  const wanted = String(position || '').trim().toUpperCase();
  if (!['K', 'DEF'].includes(wanted)) return false;
  const normalizedText = (node) => (node && node.innerText ? node.innerText : '')
    .replace(/\\s+/g, ' ').trim().toUpperCase();
  const isVisible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 1 && rect.height > 1 && style.display !== 'none' &&
      style.visibility !== 'hidden' && Number(style.opacity || '1') > 0;
  };
  const chipText = new RegExp('^' + wanted + '(?: 0/1)?$');
  const controls = new Set();
  for (const node of Array.from(document.querySelectorAll('button, [role="button"], [class*="filter" i], [class*="position" i]'))) {
    if (!isVisible(node) || !chipText.test(normalizedText(node))) continue;
    const control = node.closest('button, [role="button"], [class*="filter" i], [class*="position" i]') || node;
    if (isVisible(control) && chipText.test(normalizedText(control))) controls.add(control);
  }
  const candidates = Array.from(controls).sort((left, right) => {
    const a = left.getBoundingClientRect();
    const b = right.getBoundingClientRect();
    return (a.width * a.height) - (b.width * b.height);
  });
  if (candidates.length !== 1) return false;
  candidates[0].click();
  return true;
}"""
