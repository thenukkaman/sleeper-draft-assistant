"""Read-only Sleeper adapter. It deliberately has no pick-submission method."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from ..models import DraftState


API_ROOT = "https://api.sleeper.app/v1"


@dataclass(frozen=True)
class SleeperSnapshot:
    state: DraftState
    draft_id: str
    user_id: str
    raw_draft: dict[str, Any]


class SleeperReadOnlyClient:
    """Only public GET requests; browser automation remains a separate interface."""

    def __init__(self, api_root: str = API_ROOT, timeout_seconds: int = 10) -> None:
        self.api_root = api_root.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def snapshot_for_user(self, league_id: str, username: str) -> SleeperSnapshot:
        league = self._get(f"/league/{league_id}")
        draft_id = str(league.get("draft_id") or self._latest_draft_id(league_id))
        draft = self._get(f"/draft/{draft_id}")
        users = self._get(f"/league/{league_id}/users")
        user = next(
            (
                item
                for item in users
                if any(
                    str(identity or "").casefold() == username.casefold()
                    for identity in (item.get("username"), item.get("display_name"))
                )
            ),
            None,
        )
        if user is None:
            raise ValueError(f"Sleeper user {username!r} was not found in league {league_id}.")
        user_id = str(user["user_id"])
        picks = self._get(f"/draft/{draft_id}/picks")
        own_picks = [pick for pick in picks if str(pick.get("picked_by", "")) == user_id]
        roster_positions = tuple(
            str(pick.get("metadata", {}).get("position", "")).upper()
            for pick in own_picks
            if pick.get("metadata", {}).get("position")
        )
        drafted_players = frozenset(filter(None, (self._pick_name(pick) for pick in picks)))
        next_pick_number = max((int(pick.get("pick_no", 0)) for pick in picks), default=0) + 1
        our_pick_number, pick_label = self._next_standard_snake_pick(draft, user_id, next_pick_number)
        return SleeperSnapshot(
            state=DraftState(
                round_number=int(pick_label.split(".")[0]),
                pick_label=pick_label,
                roster_positions=roster_positions,
                drafted_players=drafted_players,
                league_id=league_id,
                username=username,
                draft_status=str(draft.get("status", "unknown")),
                current_pick_number=next_pick_number,
                our_pick_number=our_pick_number,
            ),
            draft_id=draft_id,
            user_id=user_id,
            raw_draft=draft,
        )

    def _latest_draft_id(self, league_id: str) -> str:
        drafts = self._get(f"/league/{league_id}/drafts")
        if not drafts:
            raise ValueError(f"No Sleeper draft exists for league {league_id}.")
        active = next((draft for draft in drafts if draft.get("status") in {"pre_draft", "drafting"}), drafts[0])
        return str(active["draft_id"])

    def _next_standard_snake_pick(self, draft: dict[str, Any], user_id: str, next_pick: int) -> tuple[int, str]:
        """Draft-order exceptions are detected by the UI guard, never guessed here."""
        try:
            slot = int(draft["draft_order"][user_id])
            teams = int(draft["settings"]["teams"])
            rounds = int(draft["settings"]["rounds"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Sleeper draft metadata is missing standard snake-order fields.") from error
        for round_number in range(1, rounds + 1):
            slot_this_round = slot if round_number % 2 else teams + 1 - slot
            pick_number = (round_number - 1) * teams + slot_this_round
            if pick_number >= next_pick:
                return pick_number, f"{round_number}.{slot_this_round}"
        raise ValueError("No remaining pick is available for this Sleeper user.")

    @staticmethod
    def _pick_name(pick: dict[str, Any]) -> str | None:
        metadata = pick.get("metadata", {})
        first = metadata.get("first_name", "").strip()
        last = metadata.get("last_name", "").strip()
        if first or last:
            return f"{first} {last}".strip()
        return metadata.get("player_name") or pick.get("player_name")

    def _get(self, path: str) -> Any:
        request = Request(f"{self.api_root}{path}", headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - endpoint is fixed by client.
            return json.loads(response.read().decode("utf-8"))
