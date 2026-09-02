# Visual draft audit

The browser worker deliberately uses two different channels:

- **Visual audit:** when enabled, the Playwright transport saves a full-page PNG
  and a JSON sidecar on the first rendered state and at each pick's 120s, 60s,
  30s, 10s, and 0s clock edge. The sidecar records the exact DOM-observed
  clock, pick label, draft status, auto-pick state, and timestamp.
- **Data entry:** the existing semantic DOM search and exact row `DRAFT`
  control remain the only submission path. Screenshots never select a player.

Enable it for a controlled run with `--visual-audit`:

```text
python -m draft_assistant.local_worker --mode attached-draft --league-id ... \
  --league-name "North Redmond 40" --username kenikh --draft-slot 5 \
  --visual-audit --runtime-dir runtime
```

The images and sidecars are written under that run's `visual/` directory. The
recorder is opt-in and fail-open: a screenshot failure is retained as a missing
diagnostic artifact and cannot delay or alter a pick.
