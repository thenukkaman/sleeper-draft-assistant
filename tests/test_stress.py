from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from draft_assistant.stress import MockTelemetry, PickTelemetry, read_mock_telemetry, summarize, write_mock_telemetry


class StressTelemetryTests(unittest.TestCase):
    def test_round_trip_and_summary_preserve_pick_evidence(self) -> None:
        start = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
        confirmed = PickTelemetry(
            pick_label="1.05",
            decision_started_at=start,
            selection_requested_at=start + timedelta(milliseconds=300),
            confirmed_at=start + timedelta(milliseconds=700),
            expected_player="Josh Allen",
            selected_player="Josh Allen",
            current_pick_number=5,
            auto_pick_before=False,
            auto_pick_after=False,
            outcome="confirmed",
        )
        missed = PickTelemetry(
            pick_label="2.08",
            decision_started_at=start + timedelta(minutes=2),
            selection_requested_at=None,
            confirmed_at=None,
            expected_player="Justin Herbert",
            selected_player=None,
            current_pick_number=20,
            auto_pick_before=False,
            auto_pick_after=True,
            outcome="missed",
            reason="Clock advanced before the runner submitted an action.",
        )
        run = MockTelemetry(
            mock_id="mock-1",
            started_at=start,
            completed_at=start + timedelta(minutes=20),
            picks=(confirmed, missed),
            result="completed",
        )

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "mock-1.json"
            write_mock_telemetry(path, run)
            restored = read_mock_telemetry(path)

        report = summarize((restored,))
        self.assertEqual(report["mocks_completed"], 1)
        self.assertEqual(report["picks_confirmed"], 1)
        self.assertEqual(report["picks_missed"], 1)
        self.assertEqual(report["auto_pick_incidents"], 1)
        self.assertEqual(report["selection_latency_ms"]["median"], 300)
        self.assertEqual(report["confirmation_latency_ms"]["p95"], 700)
        self.assertEqual(report["failures"][0]["pick_label"], "2.08")
