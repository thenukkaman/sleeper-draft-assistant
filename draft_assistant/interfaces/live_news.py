"""Pluggable boundary for real-time CHECK_NEWS research.

The decision policy deliberately does not search the web.  A live executor performs
that work, records the source-linked result, and supplies the result in DraftState.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import NewsReview, Player


class LiveNewsResolver(Protocol):
    """Returns a current, source-linked review for one CHECK_NEWS player."""

    def review(self, player: Player, checked_at: datetime) -> NewsReview:
        """Search current reporting and return either CLEAR or BLOCK with a URL."""

