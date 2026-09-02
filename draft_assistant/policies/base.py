"""Policy plug-in contract. Interfaces must depend only on this contract."""

from __future__ import annotations

from typing import Protocol

from ..board import Board
from ..models import DraftState, Recommendation


class DraftPolicy(Protocol):
    name: str

    def recommend(self, board: Board, state: DraftState, limit: int = 3) -> Recommendation:
        """Return a ranked recommendation without touching Sleeper or a UI."""
