"""Browser-facing data contract; no policy imports and no browser-control dependency."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..models import DraftState, NewsReview


class PlayerRowAction(StrEnum):
    """A semantic action exposed for one visible Sleeper player row.

    A player card, a queue icon, and a draft action are deliberately distinct.
    An executor must never infer that clicking a player name or queue control
    will submit a pick.
    """

    DRAFT = "draft"
    QUEUE = "queue"
    DETAILS = "details"


class BrowserBlockerKind(StrEnum):
    """A browser condition that makes a draft action unsafe.

    The runner records these independently from ``DraftState`` because they
    describe the browser's ability to act, not the fantasy-football policy.
    """

    MOCK_DRAFT_READY = "mock_draft_ready"
    LIVE_DRAFTROOM_READY = "live_draftroom_ready"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    PENDING_REQUEST = "pending_request"
    UNRESPONSIVE = "unresponsive"
    IDENTITY_MISMATCH = "identity_mismatch"


@dataclass(frozen=True)
class BrowserBlocker:
    """Structured evidence that the browser must not submit a pick.

    ``summary`` is normalized visible text or a bounded transport error. It is
    deliberately retained with the observation so the durable runner log can
    distinguish an expected pre-draft confirmation from a spinner, an unknown
    overlay, or a page that stopped answering DOM reads.
    """

    kind: BrowserBlockerKind
    summary: str
    action_labels: tuple[str, ...] = ()

    def reason(self) -> str:
        summary = self.summary.strip()
        ending = "" if summary.endswith((".", "!", "?")) else "."
        labels = f" Actions visible: {', '.join(self.action_labels)}." if self.action_labels else ""
        return f"Browser blocker ({self.kind.value}): {summary}{ending}{labels}"


@dataclass(frozen=True)
class BrowserObservation:
    """Normalized values read from Sleeper's visible draft-room UI by any executor."""

    league_id: str
    username: str
    draft_status: str
    auto_pick_enabled: bool
    round_number: int
    pick_label: str
    current_pick_number: int
    our_pick_number: int
    roster_positions: tuple[str, ...]
    drafted_players: frozenset[str]
    clock_remaining_ms: int | None = None
    clock_precision_ms: int | None = None
    available_players: frozenset[str] | None = None
    news_reviews: dict[str, NewsReview] | None = None
    sleeper_ranked_specialists: dict[str, tuple[str, ...]] | None = None
    player_row_actions: dict[str, frozenset[PlayerRowAction]] | None = None
    blocker: BrowserBlocker | None = None

    def has_draft_action(self, player_name: str) -> bool:
        """Whether the current UI exposes an exact *draft* control for a name.

        ``None`` preserves compatibility with read-only and legacy test
        observations. A production browser adapter must always populate this
        field; an empty or queue/details-only action set is not draftable.
        """

        if self.player_row_actions is None:
            return True
        return PlayerRowAction.DRAFT in self.player_row_actions.get(player_name, frozenset())

    def to_state(self) -> DraftState:
        return DraftState(
            league_id=self.league_id,
            username=self.username,
            draft_status=self.draft_status,
            auto_pick_enabled=self.auto_pick_enabled,
            round_number=self.round_number,
            pick_label=self.pick_label,
            current_pick_number=self.current_pick_number,
            our_pick_number=self.our_pick_number,
            roster_positions=self.roster_positions,
            drafted_players=self.drafted_players,
            available_players=self.available_players,
            news_reviews=self.news_reviews or {},
            sleeper_ranked_specialists=self.sleeper_ranked_specialists or {},
        )
