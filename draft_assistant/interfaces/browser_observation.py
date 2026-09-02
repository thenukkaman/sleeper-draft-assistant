"""Browser-facing data contract; no policy imports and no browser-control dependency."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import DraftState, NewsReview


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
    available_players: frozenset[str] | None = None
    news_reviews: dict[str, NewsReview] | None = None
    sleeper_ranked_specialists: dict[str, tuple[str, ...]] | None = None

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
