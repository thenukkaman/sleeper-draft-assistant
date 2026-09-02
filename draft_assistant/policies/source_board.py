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

    name = "source-board-2026-superflex-v1"

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

        if state.round_number == 1 and counts["QB"] == 0:
            names = ["Josh Allen", "Lamar Jackson", "Drake Maye", "Jayden Daniels", "Jalen Hurts"]
            forced = self._named(available, names, "QB1 at 1.05 is mandatory in this Superflex build.")
            if forced:
                return self._result(forced, "QB1 mandate: take the first available elite QB.", warnings, limit)
            fallback = self._named(available, ["Brock Bowers", "Jahmyr Gibbs", "Bijan Robinson"], "All five elite QBs are gone; use the approved fallback order.")
            return self._result(fallback, "Elite-QB fallback order from the proxy sheet.", warnings, limit)

        qbs = sorted((player for player in available if player.position == "QB"), key=lambda player: player.rank)
        viable_qb2 = [player for player in qbs if player.rank <= 10]
        if counts["QB"] < 2 and state.round_number == 2 and viable_qb2:
            return self._result(
                self._as_candidates(viable_qb2, "QB2 before the TEP/RB/WR pivot."),
                "QB2 priority at 2.08: use the viable source tier before any non-QB.",
                warnings,
                limit,
            )
        if counts["QB"] < 2 and state.round_number <= 3:
            return self._result(
                self._as_candidates(qbs, "QB2 hard deadline at 3.05."),
                "QB2 hard deadline: draft the highest eligible QB; an AVOID QB is a discounted fallback only.",
                warnings,
                limit,
            )
        if counts["QB"] < 2:
            return self._result(
                self._as_candidates(qbs, "Two-QB Superflex core is still incomplete."),
                "Repair the incomplete Superflex QB core before other positions.",
                warnings,
                limit,
            )

        # Two RB starters are mandatory.  This is deliberately a hard roster
        # constraint, not a soft score bonus: after QB2, RB1 must be secured by
        # the Round-3 pick and RB2 by the Round-4 pick.  In particular, QB3,
        # TEP, tags, and late-round UPSIDE preference may not postpone RB2.
        rbs = sorted((player for player in available if player.position == "RB"), key=lambda player: player.rank)
        rb_required = (counts["RB"] == 0 and state.round_number >= 3) or (
            counts["RB"] < 2 and state.round_number >= 4
        )
        if rb_required and rbs:
            required_slot = "RB1" if counts["RB"] == 0 else "RB2"
            # A required position does not erase the user's AVOID price rule.
            # Take the best ordinary/target/upside RB first; use an AVOID RB
            # only when no other RB remains.
            non_avoid_rbs = [player for player in rbs if player.tag != Tag.AVOID]
            return self._result(
                self._as_candidates(
                    non_avoid_rbs + [player for player in rbs if player.tag == Tag.AVOID] or rbs,
                    f"{required_slot} is a mandatory starting-roster requirement.",
                ),
                f"Draft {required_slot} now: this league requires two starting RBs before QB3 or optional upside.",
                warnings,
                limit,
            )

        if counts["QB"] < 3 and 7 <= state.round_number <= 9:
            qb3 = [player for player in qbs if player.rank <= 20]
            if qb3:
                return self._result(
                    self._as_candidates(qb3, "Viable QB3 value in the R7-R9 window."),
                    "QB3 value window: a viable third QB beats a bench RB/WR swing here.",
                    warnings,
                    limit,
                )
        if counts["QB"] < 3 and state.round_number >= 10:
            return self._result(
                self._as_candidates(qbs, "QB3 is still missing; never take a fourth QB."),
                "Finish the three-QB build before adding more bench depth.",
                warnings,
                limit,
            )

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
        directive = board.pick_plan.get(state.pick_label, "Take the highest remaining eligible source-board value.")
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
            score = 500.0 - player.rank * 3.0
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
            score += harmon_adjustment
            rookie_adjustment = self.value_model.harmon_rookie_adjustment(rookie_harmon)
            score += rookie_adjustment
            conviction = self.value_model.is_waldman_harmon_conviction(player, rsp, harmon, rookie_harmon)
            triple_conviction = self.value_model.is_triple_conviction(player, rsp, harmon, rookie_harmon)
            consensus = self._consensus(board, player, harmon)
            if consensus is not None:
                # The baseline source rank already determines most of the
                # score. Consensus moves close calls, and only tight genuine
                # agreement earns its separate label bonus.
                score += (consensus.score - 0.5) * 30.0
                if consensus.label == "GOLD":
                    score += 38.0
                elif consensus.label == "STRONG":
                    score += 20.0
            if state.round_number >= 4:
                score += (
                    self.value_model.waldman_jj_harmon_conviction_bonus
                    if triple_conviction
                    else self.value_model.waldman_harmon_conviction_bonus if conviction else 0.0
                )
            if state.round_number >= 4 and rsp:
                score += float(rsp["points"])
            score += max(0, 2 - counts[player.position]) * 8.0
            score += max(0, 1 - counts[player.position]) * 8.0
            if player.position in {"RB", "WR"} and state.round_number <= 6 and counts[player.position] < 2:
                score += 16.0
            if player.position == "TE" and player.name == "Brock Bowers" and state.round_number <= 3:
                score += 65.0
            if player.position == "TE" and counts["TE"] < 1 and state.round_number <= 8:
                score += 10.0
            reason = self._flex_reason(player, state, counts)
            if player.tag in {Tag.TARGET, Tag.AVOID}:
                reason += " " + tag_price.summary
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
        if counts[player.position] < 2:
            return f"Build the starting {player.position} core before deeper bench value."
        return "Highest remaining eligible source-board value."

    @staticmethod
    def _warnings(board: Board, state: DraftState, counts: Counter[str]) -> tuple[str, ...]:
        warnings: list[str] = []
        if state.round_number >= 4 and counts["QB"] < 2:
            warnings.append("QB2 deadline was missed; QB is now forced until repaired.")
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
