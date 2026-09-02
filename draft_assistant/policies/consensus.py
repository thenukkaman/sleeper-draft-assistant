"""Analyst-aware consensus scoring, independent from browser transport."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev


@dataclass(frozen=True)
class AnalystRanks:
    """Normalized positional ranks: lower is better; ``None`` means no call."""

    jj: int
    waldman: int | None = None
    harmon: int | None = None
    position_pool: int = 60
    rookie: bool = False


@dataclass(frozen=True)
class Consensus:
    score: float
    alignment: float
    label: str
    weights: tuple[tuple[str, float], ...]


class ConsensusModel:
    """JJ baseline; Waldman overall/rookie authority; Harmon WR authority."""

    gold_alignment_ceiling = 0.10
    strong_alignment_ceiling = 0.18

    def weights_for(self, position: str, rookie: bool) -> dict[str, float]:
        position = position.upper()
        if position == "WR" and rookie:
            return {"jj": 0.20, "waldman": 0.35, "harmon": 0.45}
        if position == "WR":
            return {"jj": 0.35, "waldman": 0.25, "harmon": 0.40}
        if rookie:
            return {"jj": 0.35, "waldman": 0.65}
        return {"jj": 0.55, "waldman": 0.45}

    def score(self, position: str, ranks: AnalystRanks) -> Consensus:
        weights = self.weights_for(position, ranks.rookie)
        supplied = {"jj": ranks.jj, "waldman": ranks.waldman, "harmon": ranks.harmon}
        usable = {name: weight for name, weight in weights.items() if supplied[name] is not None}
        # A missing specialist does not silently make JJ more influential than
        # Waldman; re-normalize only the analysts who actually made a call.
        total = sum(usable.values())
        usable = {name: weight / total for name, weight in usable.items()}
        values = {name: self._value(int(supplied[name]), ranks.position_pool) for name in usable}
        score = sum(usable[name] * values[name] for name in usable)
        alignment = pstdev(values.values()) if len(values) > 1 else 1.0
        three_way = set(usable) == {"jj", "waldman", "harmon"}
        two_way = len(usable) >= 2
        if three_way and alignment <= self.gold_alignment_ceiling and score >= 0.60:
            label = "GOLD"
        elif two_way and alignment <= self.strong_alignment_ceiling and score >= 0.55:
            label = "STRONG"
        elif score >= 0.55:
            label = "VALUE"
        else:
            label = "NEUTRAL"
        return Consensus(score, alignment, label, tuple(usable.items()))

    @staticmethod
    def _value(rank: int, pool: int) -> float:
        if pool <= 1:
            raise ValueError("position_pool must be at least two")
        return max(0.0, min(1.0, 1.0 - (rank - 1) / (pool - 1)))
