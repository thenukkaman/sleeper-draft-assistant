from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from draft_assistant.continuous_session import SessionReport, SessionTermination
from draft_assistant.local_worker import _write_run_artifacts
from draft_assistant.persistent_runner import JsonlPickTelemetrySink
from draft_assistant.stress import PickTelemetry, read_mock_telemetry


class LocalWorkerArtifactTests(unittest.TestCase):
    def test_finishes_a_secret_free_mock_evidence_bundle(self) -> None:
        now = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
        pick = PickTelemetry(
            pick_label="1.05",
            decision_started_at=now,
            selection_requested_at=now,
            confirmed_at=now,
            expected_player="Josh Allen",
            selected_player="Josh Allen",
            current_pick_number=5,
            auto_pick_before=False,
            auto_pick_after=False,
            outcome="confirmed",
        )
        report = SessionReport(
            started_at=now,
            completed_at=now,
            termination=SessionTermination.DRAFT_COMPLETE,
            cycles_completed=10,
            reason="Sleeper reported the draft complete.",
            last_pick_label="18.12",
        )
        with TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "run"
            JsonlPickTelemetrySink(run_directory / "picks.jsonl").record(pick)
            _write_run_artifacts(run_directory, report, "https://sleeper.com/draft/nfl/mock-1")
            mock = read_mock_telemetry(run_directory / "mock.json")
            session_text = (run_directory / "session.json").read_text(encoding="utf-8")

        self.assertEqual(mock.mock_id, "mock-1")
        self.assertEqual(mock.result, "completed")
        self.assertEqual(mock.picks[0].selected_player, "Josh Allen")
        self.assertNotIn("cookie", session_text.casefold())


if __name__ == "__main__":
    unittest.main()
