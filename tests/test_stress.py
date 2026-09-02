from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from draft_assistant.stress import (
    MockTelemetry,
    PickTelemetry,
    TimingEvidence,
    read_mock_telemetry,
    summarize,
    write_mock_telemetry,
)


class StressTelemetryTests(unittest.TestCase):
    def test_round_trip_and_summary_preserve_pick_evidence(self) -> None:
        start = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
        confirmed = PickTelemetry(
            pick_label="1.05",
            decision_started_at=start + timedelta(seconds=10),
            selection_requested_at=start + timedelta(seconds=10, milliseconds=300),
            confirmed_at=start + timedelta(seconds=10, milliseconds=700),
            expected_player="Josh Allen",
            selected_player="Josh Allen",
            current_pick_number=5,
            auto_pick_before=False,
            auto_pick_after=False,
            prepared_player_before="Lamar Jackson",
            auto_pick_recovery="not_needed",
            outcome="confirmed",
            timing=TimingEvidence(
                poll_started_at=start + timedelta(milliseconds=9_700),
                prior_poll_completed_at=start - timedelta(milliseconds=500),
                observed_live_at=start + timedelta(seconds=10),
                browser_clock_remaining_ms=110_000,
                browser_clock_precision_ms=1_000,
                recommendation_ready_at=start + timedelta(milliseconds=10_100),
            ),
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
            auto_pick_recovery="missed_before_recovery",
            outcome="missed",
            reason="Clock advanced before the runner submitted an action.",
            timing=TimingEvidence(
                prior_poll_completed_at=start + timedelta(minutes=1, seconds=59),
                auto_pick_detected_at=start + timedelta(minutes=2, seconds=1),
                auto_pick_disable_requested_at=start + timedelta(minutes=2, seconds=1, milliseconds=40),
                auto_pick_disabled_at=start + timedelta(minutes=2, seconds=1, milliseconds=300),
            ),
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
        self.assertEqual(report["prepared_ladder_fallbacks"], 1)
        self.assertEqual(report["auto_pick_incidents"], 1)
        self.assertEqual(report["auto_pick_recovery_failures"], 1)
        self.assertEqual(report["auto_pick_incidents_missing_timing"], 0)
        self.assertEqual(report["selection_latency_ms"]["median"], 300)
        self.assertEqual(report["confirmation_latency_ms"]["p95"], 700)
        self.assertEqual(report["clock_detection_latency_ms"]["median"], 10_000)
        self.assertEqual(report["observation_latency_ms"]["median"], 300)
        self.assertEqual(report["recommendation_latency_ms"]["median"], 100)
        self.assertEqual(report["dispatch_latency_ms"]["median"], 200)
        self.assertEqual(report["confirmation_after_selection_ms"]["median"], 400)
        self.assertEqual(report["clock_to_selection_ms"]["median"], 10_300)
        self.assertEqual(report["auto_pick_detection_upper_bound_ms"]["median"], 2_000)
        self.assertEqual(report["auto_pick_disable_latency_ms"]["median"], 300)
        self.assertEqual(report["auto_pick_disable_request_latency_ms"]["median"], 40)
        self.assertEqual(report["auto_pick_toggle_verification_latency_ms"]["median"], 260)
        self.assertEqual(report["failures"][0]["pick_label"], "2.08")
