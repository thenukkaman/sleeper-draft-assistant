"""The current approved draft-sheet logic as a swappable policy module."""

from __future__ import annotations

from collections import Counter

from ..board import Board, normalize_name
from ..models import Candidate, DraftState, Player, Recommendation, Tag


class SourceBoardPolicy:
    """Strictly applies the player board and stated Superflex constraints."""

    name = "source-board-2026-superflex-v1"

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
            if player.tag == Tag.TARGET:
                score += 18.0
            elif player.tag == Tag.UPSIDE and target:
                score += 14.0
            elif player.tag == Tag.AVOID:
                # AVOID means "only at a falling price", never an absolute ban.
                # The discount remains material, but it shrinks after the core is built.
                score -= max(30.0, 135.0 - max(0, state.round_number - 4) * 15.0)
            if state.round_number >= 4 and (rsp := board.rsp_adjustment(player.name)):
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
    def _flex_reason(player: Player, state: DraftState, counts: Counter[str]) -> str:
        if player.name == "Brock Bowers" and state.round_number <= 3:
            return "Premium 1.5-TEP pivot once the viable QB2 tier is gone."
        if player.tag == Tag.TARGET:
            return "Source TARGET wins the close call."
        if player.tag == Tag.UPSIDE and state.round_number >= 4:
            return "Source UPSIDE is preferred over floor after Round 3."
        if player.tag == Tag.AVOID:
            return "AVOID at a discount: it has fallen behind enough source-board alternatives."
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
