"""Stable domain types shared by policies and interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class Tag(str, Enum):
    TARGET = "TARGET"
    UPSIDE = "UPSIDE"
    AVOID = "AVOID"
    CHECK_NEWS = "CHECK_NEWS"
    NONE = ""


class NewsDecision(str, Enum):
    CLEAR = "CLEAR"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class NewsReview:
    """Result of a live, source-linked check for a CHECK_NEWS player."""

    decision: NewsDecision
    checked_at: datetime
    source_url: str
    note: str = ""

    def is_clear_and_fresh(self, observed_at: datetime, minutes: int = 15) -> bool:
        if self.decision != NewsDecision.CLEAR or not self.source_url:
            return False
        return timedelta(0) <= observed_at - self.checked_at <= timedelta(minutes=minutes)


@dataclass(frozen=True)
class Player:
    name: str
    position: str
    rank: int
    tag: Tag = Tag.NONE
    draftable: bool = True


@dataclass(frozen=True)
class DraftState:
    """Input supplied by any interface; it contains no policy logic."""

    round_number: int
    pick_label: str
    roster_positions: tuple[str, ...] = ()
    drafted_players: frozenset[str] = frozenset()
    available_players: frozenset[str] | None = None
    sleeper_ranked_specialists: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Market ADP and projected points are observations from Sleeper's current
    # player table, not source-board data.  Keeping them here lets a policy
    # price a tag or calculate VBD without contaminating the user's rankings.
    market_adp: dict[str, float] = field(default_factory=dict)
    projected_points: dict[str, float] = field(default_factory=dict)
    sleeper_specialist_points: dict[str, dict[str, float]] = field(default_factory=dict)
    # Ordered, most-recent-last positional selections.  A set of drafted
    # players cannot tell us whether a position is currently running.
    recent_pick_positions: tuple[str, ...] = ()
    news_reviews: dict[str, NewsReview] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    league_id: str | None = None
    username: str | None = None
    draft_status: str = "drafting"
    auto_pick_enabled: bool | None = None
    current_pick_number: int | None = None
    our_pick_number: int | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "DraftState":
        observed_at = _parse_datetime(raw.get("observed_at"))
        reviews = {
            name: NewsReview(
                decision=NewsDecision(item["decision"]),
                checked_at=_parse_datetime(item["checked_at"]),
                source_url=str(item["source_url"]),
                note=str(item.get("note", "")),
            )
            for name, item in raw.get("news_reviews", {}).items()
        }
        return cls(
            round_number=int(raw["round_number"]),
            pick_label=str(raw["pick_label"]),
            roster_positions=tuple(raw.get("roster_positions", [])),
            drafted_players=frozenset(raw.get("drafted_players", [])),
            available_players=(
                frozenset(raw["available_players"])
                if raw.get("available_players") is not None
                else None
            ),
            sleeper_ranked_specialists={
                position.upper(): tuple(names)
                for position, names in raw.get("sleeper_ranked_specialists", {}).items()
            },
            market_adp={str(name): float(value) for name, value in raw.get("market_adp", {}).items()},
            projected_points={str(name): float(value) for name, value in raw.get("projected_points", {}).items()},
            sleeper_specialist_points={
                str(position).upper(): {str(name): float(points) for name, points in values.items()}
                for position, values in raw.get("sleeper_specialist_points", {}).items()
            },
            recent_pick_positions=tuple(str(position).upper() for position in raw.get("recent_pick_positions", ())),
            news_reviews=reviews,
            observed_at=observed_at,
            league_id=raw.get("league_id"),
            username=raw.get("username"),
            draft_status=raw.get("draft_status", "drafting"),
            auto_pick_enabled=raw.get("auto_pick_enabled"),
            current_pick_number=raw.get("current_pick_number"),
            our_pick_number=raw.get("our_pick_number"),
        )


def _parse_datetime(value: Any) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Candidate:
    player: Player
    score: float
    reason: str
    discounted_avoid: bool = False


@dataclass(frozen=True)
class Recommendation:
    policy_name: str
    candidates: tuple[Candidate, ...]
    directive: str
    warnings: tuple[str, ...] = ()
    requires_special_teams_policy: bool = False

    @property
    def primary(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None
