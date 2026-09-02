"""Fail-closed guard for an autonomous browser executor."""

from __future__ import annotations

from dataclasses import dataclass

from .board import normalize_name
from .models import DraftState, Recommendation, Tag


@dataclass(frozen=True)
class ExecutionGate:
    allowed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AutonomousDraftGuard:
    """Validates every proposed UI action without making the UI action itself."""

    league_id: str
    username: str

    def evaluate(self, state: DraftState, recommendation: Recommendation) -> ExecutionGate:
        reasons: list[str] = []
        primary = recommendation.primary
        if state.league_id != self.league_id:
            reasons.append("League ID does not match the approved autonomous-draft league.")
        if state.username is not None and state.username.casefold() != self.username.casefold():
            reasons.append("Sleeper username does not match the approved draft account.")
        if state.draft_status != "drafting":
            reasons.append("Sleeper draft is not actively drafting.")
        if state.auto_pick_enabled is not False:
            reasons.append("The live browser has not confirmed that Sleeper auto-pick is off.")
        if recommendation.requires_special_teams_policy:
            reasons.append("No named K/DST policy exists, so autonomous selection is prohibited.")
        if primary is None:
            reasons.append("The policy did not return a named player.")
        elif primary.player.tag == Tag.AVOID and not primary.discounted_avoid:
            reasons.append("An AVOID player was not marked as a discounted policy value.")
        elif primary.player.tag == Tag.CHECK_NEWS:
            review = state.news_reviews.get(primary.player.name)
            if review is None or not review.is_clear_and_fresh(state.observed_at):
                reasons.append("A CHECK_NEWS player lacks a fresh, source-linked CLEAR live-news review.")
        elif primary.player.position in {"K", "DEF"}:
            visible_specialists = {
                normalize_name(name)
                for names in state.sleeper_ranked_specialists.values()
                for name in names
            }
            if normalize_name(primary.player.name) not in visible_specialists:
                reasons.append("The specialist is not on the visible Sleeper-ranked specialist list.")
        elif state.available_players is not None and normalize_name(primary.player.name) not in {
            normalize_name(name) for name in state.available_players
        }:
            reasons.append("The selected player is not present on the interface's available-player list.")
        if (
            state.current_pick_number is not None
            and state.our_pick_number is not None
            and state.current_pick_number != state.our_pick_number
        ):
            reasons.append("It is not the approved team's live pick; do not touch the Sleeper UI.")
        return ExecutionGate(allowed=not reasons, reasons=tuple(reasons))
