"""Safety wrapper for a concrete Sleeper DOM transport.

The transport is intentionally supplied by the host browser runtime. This
repository never reads profiles, cookies, or private Sleeper APIs; it receives
only the structured visible observation and has authority to request two exact
UI actions: player-row DRAFT and turning auto-pick off.
"""

from __future__ import annotations

from typing import Protocol

from ..board import normalize_name
from ..interfaces.browser_observation import BrowserObservation
from ..live_driver import DraftRoom


class SleeperDomTransport(Protocol):
    """Host-specific implementation over structured DOM/accessibility state."""

    def observe_visible_draft_room(self) -> BrowserObservation: ...

    def click_exact_draft(self, player_name: str) -> None: ...

    def set_auto_pick_off(self) -> None: ...


class SleeperBrowserDraftRoom(DraftRoom):
    """Enforce the browser action contract at the policy/DOM boundary.

    ``observe`` retains the last structured state. ``draft`` then permits only
    the named player's DRAFT action from that state; the host transport must
    resolve a live semantic row control at click time and must not translate
    this into a player-card or queue click. Auto-pick has no enable method.
    """

    def __init__(self, transport: SleeperDomTransport) -> None:
        self.transport = transport
        self._last_observation: BrowserObservation | None = None

    def observe(self) -> BrowserObservation:
        self._last_observation = self.transport.observe_visible_draft_room()
        return self._last_observation

    def draft(self, player_name: str) -> None:
        observation = self._last_observation
        if observation is None:
            raise RuntimeError("A fresh Sleeper observation is required before any draft action.")
        if not observation.has_draft_action(player_name):
            raise RuntimeError(
                f"Sleeper did not expose an exact DRAFT control for {player_name!r}; "
                "player-card and queue controls are prohibited."
            )
        available = observation.available_players
        if available is not None and normalize_name(player_name) not in {
            normalize_name(name) for name in available
        }:
            raise RuntimeError(f"{player_name!r} is not in the last visible Sleeper player list.")
        self.transport.click_exact_draft(player_name)

    def set_auto_pick(self, enabled: bool) -> None:
        if enabled:
            raise RuntimeError("The autonomous browser adapter may only turn auto-pick off.")
        observation = self._last_observation
        if observation is None or observation.auto_pick_enabled is not True:
            raise RuntimeError("Auto-pick was not visibly enabled in the last Sleeper observation.")
        self.transport.set_auto_pick_off()
