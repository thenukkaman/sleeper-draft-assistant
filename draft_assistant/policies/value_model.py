"""Pluggable market-value, VBD, and position-run rules.

This module deliberately has no browser dependency and no player names.  The
source board says *who* we like; the live adapter supplies ADP, projections,
and recently drafted positions; this module says when a price is good enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..board import normalize_name
from ..models import DraftState, Player, Tag


@dataclass(frozen=True)
class TagPrice:
    """An explicit tag price stated in overall draft slots."""

    adjustment: float
    summary: str
    clearance_met: bool = False


@dataclass(frozen=True)
class ValueModel:
    """Default redraft pricing for the approved 12-team SF PPR/TEP build.

    The numbers are intentionally modest. A target wins a close call; it does
    not override a real one-round market fall.  An AVOID is unavailable at its
    ordinary price and becomes eligible only after the named clearance.
    """

    score_per_slot: float = 3.0
    # Round bands: 1-3, 4-8, 9+.
    target_premium_slots: tuple[int, int, int] = (6, 8, 5)
    avoid_discount_slots: tuple[int, int, int] = (12, 10, 6)
    upside_bonus: float = 14.0
    harmon_wr_rank_weight: float = 8.0
    harmon_rookie_composite_weight: float = 2.0
    waldman_harmon_conviction_bonus: float = 24.0
    waldman_jj_harmon_conviction_bonus: float = 38.0
    replacement_depth: dict[str, int] | None = None
    run_short_window: int = 6
    run_short_count: int = 3
    run_long_window: int = 12
    run_long_count: int = 5
    pivot_vbd_gap: float = 4.0

    def __post_init__(self) -> None:
        if self.replacement_depth is None:
            # Startable population, including the two flex slots, rather than
            # the much smaller nominal starting-position count.
            object.__setattr__(self, "replacement_depth", {"QB": 24, "RB": 30, "WR": 36, "TE": 16, "K": 12, "DEF": 12})

    def band_index(self, round_number: int) -> int:
        return 0 if round_number <= 3 else 1 if round_number <= 8 else 2

    def tag_price(self, player: Player, state: DraftState) -> TagPrice:
        """Return the explicit market adjustment for a board tag.

        ``market_adp`` is mandatory for a measured clearance. If it was not
        captured, an AVOID gets a conservative penalty instead of an invented
        claim that it fell by a particular number of picks.
        """

        band = self.band_index(state.round_number)
        if player.tag == Tag.TARGET:
            premium = self.target_premium_slots[band]
            return TagPrice(premium * self.score_per_slot, f"TARGET: pay up to {premium} overall picks above market in this round band.")
        if player.tag != Tag.AVOID:
            return TagPrice(0.0, "No market tag adjustment.")
        clearance = self.avoid_discount_slots[band]
        adp = self._named_number(state.market_adp, player.name)
        if adp is None or state.current_pick_number is None:
            return TagPrice(
                -clearance * self.score_per_slot,
                f"AVOID: needs a {clearance}-pick market fall; Sleeper ADP was not captured, so hold below ordinary values.",
            )
        fall = state.current_pick_number - adp
        if fall < clearance:
            remaining = clearance - fall
            return TagPrice(
                -(remaining + 2) * self.score_per_slot,
                f"AVOID: only {fall:.1f} picks below ADP; needs {clearance} (another {remaining:.1f}).",
            )
        surplus = min(4.0, fall - clearance)
        return TagPrice(
            surplus * self.score_per_slot,
            f"AVOID clearance met: {fall:.1f} picks below ADP versus a {clearance}-pick requirement.",
            True,
        )

    def harmon_wr_adjustment(self, player: Player, harmon: dict[str, int] | None) -> float:
        """Weight Harmon's WR order more heavily than other outside overlays."""

        if player.position != "WR" or harmon is None:
            return 0.0
        return (player.rank - int(harmon["rank"])) * self.harmon_wr_rank_weight

    def harmon_rookie_adjustment(self, rookie: dict[str, float] | None) -> float:
        """Reward proven rookie coverage success above a 70-point baseline."""

        if rookie is None:
            return 0.0
        return max(0.0, float(rookie["composite"]) - 70.0) * self.harmon_rookie_composite_weight

    @staticmethod
    def is_waldman_harmon_conviction(
        player: Player,
        rsp: dict[str, object] | None,
        harmon: dict[str, int] | None,
        rookie: dict[str, float] | None = None,
    ) -> bool:
        """Both analysts must independently be affirmative for a strong target.

        RSP is affirmative at +3 or better. Harmon must either place the WR at
        least as highly as the source board or show a 75+ rookie coverage
        composite. A weaker overall Harmon rank alone is not agreement.
        """

        return bool(
            player.position == "WR"
            and rsp is not None
            and float(rsp.get("points", 0)) >= 3
            and (
                (harmon is not None and int(harmon["rank"]) <= player.rank)
                or (rookie is not None and float(rookie["composite"]) >= 75.0)
            )
        )

    @staticmethod
    def is_triple_conviction(
        player: Player,
        rsp: dict[str, object] | None,
        harmon: dict[str, int] | None,
        rookie: dict[str, float] | None = None,
    ) -> bool:
        """Require affirmative Waldman, JJ source-tag, and Harmon evidence."""

        return bool(
            player.tag in {Tag.TARGET, Tag.UPSIDE}
            and ValueModel.is_waldman_harmon_conviction(player, rsp, harmon, rookie)
        )

    def vbd(self, player: Player, available: Iterable[Player], state: DraftState) -> float | None:
        """Projected points above a position-appropriate replacement player.

        Returns ``None`` until the adapter captured projections for both the
        player and a meaningful set of his position peers.  That makes a
        missing feed a neutral condition, never a fake precision value.
        """

        projection = self._named_number(state.projected_points, player.name)
        if projection is None:
            return None
        peers = sorted(
            (
                value
                for candidate in available
                if candidate.position == player.position
                if (value := self._named_number(state.projected_points, candidate.name)) is not None
            ),
            reverse=True,
        )
        replacement_depth = (self.replacement_depth or {})[player.position]
        # A virtualized screen often exposes only its first ~20 rows. Using
        # its final visible row as RB30/WR36 would fabricate a VBD edge, so
        # require the actual replacement population before scoring VBD.
        if len(peers) < replacement_depth:
            return None
        replacement_index = replacement_depth - 1
        return projection - peers[replacement_index]

    def is_position_run(self, position: str, state: DraftState) -> bool:
        picks = tuple(item.upper() for item in state.recent_pick_positions)
        position = position.upper()
        return (
            len(picks[-self.run_short_window :]) >= self.run_short_window
            and picks[-self.run_short_window :].count(position) >= self.run_short_count
        ) or (
            len(picks[-self.run_long_window :]) >= self.run_long_window
            and picks[-self.run_long_window :].count(position) >= self.run_long_count
        )

    def should_pivot(self, position: str, values: Iterable[float | None], state: DraftState) -> bool:
        """Chase a run only when it exposes a real tier cliff.

        The first value is the best remaining option; the fifth approximates
        what will remain after the next short run. No complete projections
        means no early pivot.
        """

        ordered = [value for value in values if value is not None]
        return (
            self.is_position_run(position, state)
            and len(ordered) >= 5
            and ordered[0] - ordered[4] >= self.pivot_vbd_gap
        )

    @staticmethod
    def _named_number(values: dict[str, float], player_name: str) -> float | None:
        expected = normalize_name(player_name)
        for name, value in values.items():
            if normalize_name(name) == expected:
                return float(value)
        return None
