# Sleeper recovery map

This map is for re-orienting an authenticated browser without relying on
browser history, a stale mock tab, or a conversational agent's memory. It was
visually and accessibly checked against the North Redmond 40 live UI on
2026-09-02.

## Approved routes

| Purpose | Route or visible path | Success evidence | Do not use it for |
| --- | --- | --- | --- |
| Global recovery | `https://sleeper.com/` → **FANTASY** | The authenticated `kenikh` account is visible; Fantasy takes this account back to North Redmond 40 pre-draft. | Selecting a player or starting a mock. |
| Canonical league recovery | `https://sleeper.com/leagues/1314724839730204672/predraft` | **North Redmond 40**, `2026 12-Team SF PPR TEP`, and **DRAFTROOM** are visible. | Creating a new mock. |
| Live draft room | Click the visible **DRAFTROOM** control on the canonical pre-draft page. | Route is `https://sleeper.com/draft/nfl/1314724839738601472`; header says `2 Min Per Pick · 12 Teams · 18 Rounds`; `kenikh` is slot 1.05. | Mock creation or mock start controls. |
| Mock-only area | Canonical pre-draft → **MOCK DRAFTS** | Mock lobby / New Mock Draft controls are visible. | Any live-draft recovery. |

The live league id (`1314724839730204672`) and its draft-room id
(`1314724839738601472`) are intentionally different. The route itself is not
enough to identify the target; the rendered league name, account name, format,
and slot must still match.

## Live-room landmarks

The live room and a Sleeper mock share the same primary structure:

- snake-board cells use `draft-cell-{overall_pick}` and show the visible pick
  label; `kenikh` has 1.05, 2.08, 3.05 … 18.08;
- the bottom player grid contains search, position filters, Sleeper ADP and
  projection columns;
- Queue, Roster, and Chat sit to the right; the auto-pick toggle is adjacent;
- before the draft starts, no active timer is expected. Treat that as
  `waiting`, not a stalled page.

Critical action distinction: the **left +** on a player row is Sleeper's draft
action. The purple document-plus is the queue action. Player-name/card clicks
open details and must never be used as a drafting fallback. Accessibility
currently exposes row icons generically, so a runner must bind and validate
the DOM's exact action control, not operate by an accessibility index.

## Recovery ladder

1. Preserve the existing Draft Room tab if it is still present; open a second
   tab for recovery rather than navigating it away.
2. Open the canonical pre-draft URL. Confirm league, account, format, 12
   teams, 18 rounds, and slot 1.05.
3. Click **DRAFTROOM** once. Do not click **MOCK DRAFTS**, **New Mock Draft**,
   or **Start Draft** in a live recovery.
4. In the room, confirm the single active draft cell before selecting. If the
   real draft has not opened a clock, remain in `waiting` and keep the
   preloaded emergency queue intact.
5. If a player-details card is open, press Escape, confirm its removal, then
   re-observe the board. Do not click through the overlay.
6. If an unknown consent, confirmation, or security dialog is visible, stop
   selection and surface the blocker; only the verified player-details card is
   automatically dismissible.

## Queue contingency

Before the live clock begins, seed the emergency ranked ladder through the
queue control. It is a fallback for Sleeper auto-pick, not an alternative to
the direct exact-DRAFT action. Keep K last and DST immediately before K. The
worker may refresh the queue after board changes, but a recovery must never
clear the existing queue until a replacement queue has been verified visible.
