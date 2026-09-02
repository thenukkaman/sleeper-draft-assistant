"""The current approved draft-sheet logic as a swappable policy module."""

from __future__ import annotations

from collections import Counter

from ..board import Board, normalize_name
from ..models import Candidate, DraftState, Player, Recommendation, Tag
from .value_model import ValueModel
from .consensus import AnalystRanks, ConsensusModel
from ..research import WaldmanRedraftRecord


class SourceBoardPolicy:
    """Strictly applies the player board and stated Superflex constraints."""

    name = "source-board-2026-superflex-value-first-v2"

    def __init__(
        self,
        value_model: ValueModel | None = None,
        waldman_redraft: dict[str, WaldmanRedraftRecord] | None = None,
        consensus_model: ConsensusModel | None = None,
    ) -> None:
        # Independent module: next year's market/risk policy can be swapped
        # without rewriting the board, roster constraints, or browser adapter.
        self.value_model = value_model or ValueModel()
        self.waldman_redraft = waldman_redraft or {}
        self.consensus_model = consensus_model or ConsensusModel()

    def recommend(self, board: Board, state: DraftState, limit: int = 3) -> Recommendation:
        if state.draft_status != "drafting":
            return Recommendation(
                policy_name=self.name,
                candidates=(),
                directive="Do not act: Sleeper draft status is not drafting.",
                warnings=(f"Draft status is {state.draft_status!r}.",),
            )

        counts = Counter(position.upper() for position in state.roster_positions)
        available = self._eligible(board, state)
        warnings = self._warnings(board, state, counts)

        if state.round_number == 17 and counts["DEF"] < 1:
            return Recommendation(
                policy_name=self.name,
                candidates=(),
                directive="Draft one DST from the Sleeper-ranked specialist list.",
                warnings=warnings + ("A Sleeper-rank DST plug-in is required for autonomous selection.",),
                requires_special_teams_policy=True,
            )
        if state.round_number == 18 and counts["K"] < 1:
            return Recommendation(
                policy_name=self.name,
                candidates=(),
                directive="Draft one K from the Sleeper-ranked specialist list.",
                warnings=warnings + ("A Sleeper-rank kicker plug-in is required for autonomous selection.",),
                requires_special_teams_policy=True,
            )

        ranked = self._score_flex_candidates(board, available, state, counts)
        directive = (
            "Value first: take the highest remaining modeled value. "
            "Starter coverage receives increasing pressure as roster flexibility disappears, but no ordinary round forces a position."
        )
        return self._result(ranked, directive, warnings, limit)

    @staticmethod
    def _eligible(board: Board, state: DraftState) -> list[Player]:
        reviews = {normalize_name(name): review for name, review in state.news_reviews.items()}
        return [
            player
            for player in board.available(state)
            if player.tag != Tag.CHECK_NEWS
            or (
                (review := reviews.get(normalize_name(player.name))) is not None
                and review.is_clear_and_fresh(state.observed_at)
            )
        ]

    @staticmethod
    def _named(available: list[Player], names: list[str], reason: str) -> list[Candidate]:
        by_name = {normalize_name(player.name): player for player in available}
        return [
            Candidate(
                player=by_name[key],
                score=10000 - index,
                reason=reason,
                discounted_avoid=by_name[key].tag == Tag.AVOID,
            )
            for index, key in enumerate(map(normalize_name, names))
            if key in by_name
        ]

    @staticmethod
    def _as_candidates(players: list[Player], reason: str) -> list[Candidate]:
        return [
            Candidate(
                player=player,
                score=1000 - player.rank,
                reason=reason,
                discounted_avoid=player.tag == Tag.AVOID,
            )
            for player in players
        ]

    def _score_flex_candidates(
        self, board: Board, available: list[Player], state: DraftState, counts: Counter[str]
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for player in available:
            if player.position == "QB" and counts["QB"] >= 3:
                continue
            analyst_score, consensus = self._analyst_score(board, player)
            # The analyst model is the actual baseline, rather than a label
            # pasted onto JJ's inherited order. Live VBD, roster needs, and
            # market price then settle cross-position decisions.
            score = analyst_score * 240.0
            target = state.round_number >= 4
            tag_price = self.value_model.tag_price(player, state)
            score += tag_price.adjustment
            if player.tag == Tag.UPSIDE and target:
                score += self.value_model.upside_bonus
            vbd = self.value_model.vbd(player, available, state)
            if vbd is not None:
                # VBD is intentionally a tie-breaker scale, not a wholesale
                # rewrite of the user-ranked board. A 4-point tier cliff can
                # legitimately overcome a close source-rank difference.
                score += vbd
            rsp = board.rsp_adjustment(player.name)
            harmon = board.harmon_wr_adjustment(player.name)
            rookie_harmon = board.harmon_rookie_wr_adjustment(player.name)
            harmon_adjustment = self.value_model.harmon_wr_adjustment(player, harmon)
            # Harmon is already a weighted input to ``_analyst_score``.
            # Applying a second raw "slots × points" bump here lets one
            # analyst leap a clear JJ/Waldman tier, which contradicts the
            # Venn model.  Retain the delta below as human-readable context,
            # but score it exactly once through the normalized consensus.
            rookie_adjustment = self.value_model.harmon_rookie_adjustment(rookie_harmon)
            score += rookie_adjustment
            conviction = self.value_model.is_waldman_harmon_conviction(player, rsp, harmon, rookie_harmon)
            triple_conviction = self.value_model.is_triple_conviction(player, rsp, harmon, rookie_harmon) or (
                board.source_tag(player.name) in {Tag.TARGET, Tag.UPSIDE}
                and self.value_model.is_waldman_harmon_conviction(player, rsp, harmon, rookie_harmon)
            )
            if consensus is not None:
                # Tight agreement is a confidence signal on top of the
                # weighted rank. It never replaces an explicit personal gate
                # or promotes a low-ranked late player to an early pick.
                if consensus.label == "GOLD":
                    score += 38.0 if consensus.score >= 0.60 else 8.0
                elif consensus.label == "STRONG":
                    score += 20.0 if consensus.score >= 0.55 else 5.0
            if state.round_number >= 4:
                score += (
                    self.value_model.waldman_jj_harmon_conviction_bonus
                    if triple_conviction
                    else self.value_model.waldman_harmon_conviction_bonus if conviction else 0.0
                )
            if state.round_number >= 4 and rsp:
                score += float(rsp["points"])
            score += self._roster_coverage_urgency(player, counts, state)
            if player.position == "TE" and player.name == "Brock Bowers" and state.round_number <= 3:
                score += 65.0
            reason = self._flex_reason(player, state, counts)
            if player.tag in {Tag.TARGET, Tag.AVOID}:
                reason += " " + tag_price.summary
            source_tag = board.source_tag(player.name)
            preference = board.preference_adjustment(player.name)
            if preference is not None:
                direction = "above" if preference["slots"] > 0 else "below"
                reason += (
                    f" Personal {preference['label']}: {abs(preference['slots'])} spots "
                    f"{direction} consensus."
                )
            if source_tag != Tag.NONE:
                reason += f" JJ qualitative signal: {source_tag.value}."
            if vbd is not None:
                reason += f" VBD: +{vbd:.1f} projected points over {player.position} replacement."
            if harmon is not None:
                direction = "above" if harmon_adjustment >= 0 else "below"
                reason += (
                    f" Harmon WR overlay: WR{harmon['rank']}, Tier {harmon['tier']} "
                    f"({abs(harmon_adjustment) / self.value_model.harmon_wr_rank_weight:.0f} board slots {direction} source rank)."
                )
            if rookie_harmon is not None:
                reason += (
                    f" Harmon rookie coverage composite: {rookie_harmon['composite']:.1f} "
                    f"over {rookie_harmon['routes']} routes."
                )
            if triple_conviction and state.round_number >= 4:
                reason += " Waldman + JJ + Harmon conviction: TRIPLE-WINNER TARGET."
            elif conviction and state.round_number >= 4:
                reason += " Waldman + Harmon conviction: STRONG TARGET."
            if consensus is not None:
                reason += f" Consensus {consensus.label}: {consensus.score:.2f} value / {consensus.alignment:.2f} dispersion."
            if self.value_model.is_position_run(player.position, state):
                reason += (
                    " Position run detected; pivot only if the live VBD tier break exceeds "
                    f"{self.value_model.pivot_vbd_gap:.1f} points."
                )
            if state.round_number >= 4 and (rsp := board.rsp_adjustment(player.name)):
                reason += f" RSP overlay: {rsp['note']}"
            candidates.append(
                Candidate(
                    player=player,
                    score=score,
                    reason=reason,
                    discounted_avoid=player.tag == Tag.AVOID,
                )
            )
        return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.player.rank, candidate.player.name))

    @staticmethod
    def _roster_coverage_urgency(player: Player, counts: Counter[str], state: DraftState) -> float:
        """Price roster coverage without round-by-round position mandates.

        Missing starters get a graduated premium. It becomes a hard safety
        edge only if skipping the position would make the starting core
        mathematically impossible to finish with the remaining own picks.
        """

        # The lineup has five fixed offensive starters (QB/RB/RB/WR/WR/TE),
        # one W/R/T FLEX, and one Superflex.  QB2 is therefore valuable in
        # this format, but is *not* a mandatory starter: a second RB, WR, or
        # TE may validly occupy the Superflex.  DST is never flex-eligible.
        starters = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
        depth_targets = {"QB": 2, "RB": 5, "WR": 6, "TE": 2}
        # QB1 carries a meaningful Superflex scarcity premium. QB2 itself is
        # still an optional Superflex value choice, not roster obligation.
        starter_weight = {"QB": 82.0, "RB": 24.0, "WR": 18.0, "TE": 10.0}
        position = player.position
        if position not in starters:
            return 0.0
        # Before buying a luxury QB3 or TE3, finish the playable weekly core:
        # RB2/WR2 starters plus one bench back and receiver.  This is a
        # construction constraint, not a round script; it stops TEP/value
        # enthusiasm from creating a five-TE roster while RB/WR are thin.
        rb_wr_depth_missing = counts["RB"] < 3 or counts["WR"] < 3
        luxury_position = (
            (position == "QB" and counts["QB"] >= 2)
            or (position == "TE" and counts["TE"] >= 2)
        )
        if luxury_position and rb_wr_depth_missing:
            return -10_000.0
        # TE2 remains a normal TEP/value decision. TE3+ is allowed only when
        # its modeled edge can overcome a meaningful opportunity-cost charge.
        if position == "TE" and counts["TE"] >= 2:
            return -60.0
        if state.round_number >= 4 and position in {"RB", "WR"} and counts[position] < 3:
            return 110.0 if counts[position] < starters[position] else 70.0
        missing = max(0, starters[position] - counts[position])
        total_missing = sum(max(0, target - counts[key]) for key, target in starters.items())
        picks_made = len(state.roster_positions)
        picks_remaining_after_this = max(0, 17 - picks_made)
        missing_after_this = total_missing - (1 if missing else 0)
        if missing and picks_remaining_after_this < missing_after_this:
            return 10_000.0
        if missing:
            time_pressure = max(0, state.round_number - 1) * (starter_weight[position] / 6.0)
            return missing * starter_weight[position] + time_pressure
        return 5.0 if counts[position] < depth_targets[position] else 0.0

    def _ranked_by_analyst(self, board: Board, players):
        """Position order from the weighted Venn model, stable on ties."""

        return sorted(
            players,
            key=lambda player: (-self._analyst_score(board, player)[0], player.rank, player.name),
        )

    def _analyst_score(self, board: Board, player: Player) -> tuple[float, object | None]:
        """Return a comparable 0–1 analyst score and its consensus evidence.

        The original JJ rank is always present. Waldman and Harmon are added
        at their declared weights when they made a positional call. JJ's
        original tag is retained as a small qualitative adjustment, not an
        instruction that can overrule the multi-source order.
        """

        consensus = self._consensus(board, player, board.harmon_wr_adjustment(player.name))
        pool = sum(1 for candidate in board.players if candidate.position == player.position)
        jj_anchor = self.consensus_model._value(player.rank, pool)
        base = consensus.score if consensus is not None else jj_anchor
        # Reconciliation can settle a close tier but may not leap a clear JJ
        # source-rank tier on an ordinary signal.  Only GOLD three-source
        # alignment can exceed this bounded six-slot overlay.
        if consensus is not None and consensus.label != "GOLD":
            base = min(base, jj_anchor + (6.0 / pool))
        source_adjustment = {
            Tag.TARGET: 0.018,
            Tag.UPSIDE: 0.009,
            Tag.AVOID: -0.018,
            Tag.CHECK_NEWS: 0.0,
            Tag.NONE: 0.0,
        }[board.source_tag(player.name)]
        preference = board.preference_adjustment(player.name)
        # This converts the user's stated rank move into the same 0--1
        # positional scale as consensus.  It is deliberately applied after
        # analyst reconciliation: SHADE/GLAZE alters the final draft order,
        # never the underlying JJ/Waldman/Harmon evidence.
        preference_adjustment = (
            float(preference["slots"]) / pool if preference is not None else 0.0
        )
        return max(0.0, min(1.0, base + source_adjustment + preference_adjustment)), consensus

    def _consensus(self, board: Board, player: Player, harmon: dict[str, int] | None):
        record = self.waldman_redraft.get(normalize_name(player.name))
        if record is None:
            return None
        pool = sum(1 for candidate in board.players if candidate.position == player.position)
        return self.consensus_model.score(
            player.position,
            AnalystRanks(
                jj=player.rank,
                waldman=record.position_rank,
                harmon=int(harmon["rank"]) if harmon is not None else None,
                position_pool=pool,
                rookie=board.rsp_adjustment(player.name) is not None,
            ),
        )

    @staticmethod
    def _flex_reason(player: Player, state: DraftState, counts: Counter[str]) -> str:
        if player.name == "Brock Bowers" and state.round_number <= 3:
            return "Premium 1.5-TEP pivot once the viable QB2 tier is gone."
        if player.tag == Tag.TARGET:
            return "Source TARGET wins the close call."
        if player.tag == Tag.UPSIDE and state.round_number >= 4:
            return "Source UPSIDE is preferred over floor after Round 3."
        if player.tag == Tag.AVOID:
            return "AVOID: only draft after its explicit market-clearance requirement is met."
        required_starters = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
        if counts[player.position] < required_starters.get(player.position, 0):
            return f"Build the required {player.position} starting core before deeper bench value."
        return "Highest remaining eligible source-board value."

    @staticmethod
    def _warnings(board: Board, state: DraftState, counts: Counter[str]) -> tuple[str, ...]:
        warnings: list[str] = []
        if state.round_number >= 4 and counts["QB"] < 2:
            warnings.append("QB2 remains an open Superflex option; take it only when its value clears RB/WR alternatives.")
        if counts["QB"] >= 3:
            warnings.append("Three-QB target reached: QB4 is forbidden by this policy.")
        if state.pick_label not in board.pick_plan:
            warnings.append("Pick label is not in the approved plan; verify the draft format before acting.")
        reviews = {normalize_name(name): review for name, review in state.news_reviews.items()}
        unresolved = [
            player.name
            for player in board.available(state)
            if player.tag == Tag.CHECK_NEWS
            and not (
                (review := reviews.get(normalize_name(player.name))) is not None
                and review.is_clear_and_fresh(state.observed_at)
            )
        ]
        if unresolved:
            warnings.append(
                "Fresh live web review required before these players are eligible: "
                + ", ".join(unresolved)
                + "."
            )
        return tuple(warnings)

    def _result(
        self, candidates: list[Candidate], directive: str, warnings: tuple[str, ...], limit: int
    ) -> Recommendation:
        return Recommendation(
            policy_name=self.name,
            candidates=tuple(candidates[:limit]),
            directive=directive,
            warnings=warnings,
        )
