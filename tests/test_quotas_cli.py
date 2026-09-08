"""Tests for quota refresh history and display deltas."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from quotas.cli import (  # noqa: E402
    _apply_deltas,
    _append_quota_history,
    _history_series,
    _load_quota_snapshot,
    _new_quota_history,
    _records_signature,
    run_once,
    _save_quota_snapshot,
)
from quotas.layout import _delta_text  # noqa: E402
from quotas.models import QuotaRecord  # noqa: E402


class QuotaDeltaTests(unittest.TestCase):
    def test_today_history_collects_used_percent_series(self):
        record = QuotaRecord(
            name="ollama-pro",
            status="ok",
            windows=[
                {"label": "5h", "remaining_percent": 90.0, "window_seconds": 18000},
                {"label": "week", "remaining_percent": 80.0, "window_seconds": 604800},
            ],
        )
        first = datetime(2026, 8, 26, 23, 50, tzinfo=timezone.utc)
        second = datetime(2026, 8, 27, 0, 5, tzinfo=timezone.utc)
        history = _new_quota_history(first)
        _append_quota_history(history, [record], first)
        self.assertEqual(history["date"], "2026-08-27")
        self.assertEqual(_history_series(history)["ollama-pro"]["total"], [20.0])

        record.windows[0]["remaining_percent"] = 85.0
        _append_quota_history(history, [record], second)
        series = _history_series(history)
        self.assertEqual(series["ollama-pro"]["total"], [20.0, 20.0])

        next_day = datetime(2026, 8, 27, 16, 5, tzinfo=timezone.utc)
        _append_quota_history(history, [record], next_day)
        self.assertEqual(history["date"], "2026-08-28")
        self.assertEqual(len(history["samples"]), 1)

    def test_delta_is_remaining_percentage_points_per_window(self):
        previous = {
            "ollama-pro": {
                "name": "ollama-pro",
                "status": "ok",
                "remaining_percent": 82.0,
                "windows": [
                    {"label": "5h", "remaining_percent": 82.0, "window_seconds": 18000},
                    {"label": "week", "remaining_percent": 91.0, "window_seconds": 604800},
                ],
            }
        }
        current = QuotaRecord(
            name="ollama-pro",
            status="ok",
            remaining_percent=80.0,
            windows=[
                {"label": "5h", "remaining_percent": 80.0, "window_seconds": 18000},
                {"label": "week", "remaining_percent": 93.0, "window_seconds": 604800},
            ],
        )

        _apply_deltas([current], previous)

        self.assertEqual(current.delta_percent, -2.0)
        self.assertEqual([w["delta_percent"] for w in current.windows], [-2.0, 2.0])
        self.assertEqual(_delta_text(current.windows[0]), "Δ-2.0")
        self.assertEqual(_delta_text(current.windows[1]), "Δ+2.0")

    def test_first_refresh_has_no_delta(self):
        current = QuotaRecord(
            name="codex",
            status="ok",
            remaining_percent=75.0,
            windows=[{"label": "week", "remaining_percent": 75.0}],
        )
        _apply_deltas([current], {})
        self.assertIsNone(current.delta_percent)
        self.assertNotIn("delta_percent", current.windows[0])

    def test_snapshot_strips_derived_delta(self):
        record = QuotaRecord(
            name="ollama-pro",
            status="ok",
            remaining_percent=80.0,
            delta_percent=-2.0,
            windows=[
                {
                    "label": "5h",
                    "remaining_percent": 80.0,
                    "delta_percent": -2.0,
                }
            ],
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snapshot.json"
            _save_quota_snapshot(path, {}, [record])
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("delta_percent", raw)
            loaded = _load_quota_snapshot(path)
            self.assertEqual(loaded["ollama-pro"]["remaining_percent"], 80.0)

    def test_signature_ignores_delta_metadata(self):
        record = QuotaRecord(
            name="codex",
            status="ok",
            remaining_percent=75.0,
            windows=[{"label": "week", "remaining_percent": 75.0}],
        )
        baseline = _records_signature([record])
        record.delta_percent = -1.0
        record.windows[0]["delta_percent"] = -1.0
        self.assertEqual(_records_signature([record]), baseline)

    def test_run_once_passes_history_to_renderer(self):
        from argparse import Namespace
        from unittest.mock import patch
        from PIL import Image

        def record(remaining):
            return QuotaRecord(
                name="ollama-pro",
                status="ok",
                remaining_percent=remaining,
                windows=[
                    {"label": "5h", "remaining_percent": remaining, "window_seconds": 18000},
                    {"label": "week", "remaining_percent": 90.0, "window_seconds": 604800},
                ],
            )

        args = Namespace(
            out_dir=None,
            json=False,
            send=False,
            partial=False,
            partial_red=False,
            force=False,
            bleprobe="bleprobe.py",
            layout=None,
            verbose=False,
        )
        with tempfile.TemporaryDirectory() as td:
            args.out_dir = td
            with (
                patch("quotas.cli.discover_credentials", return_value={}),
                patch("quotas.cli.fetch_all_quotas", side_effect=[[record(90.0)], [record(80.0)]]),
                patch("quotas.cli.fetch_market_snapshot", return_value={"items": []}),
                patch("quotas.cli.render_quota_image", return_value=Image.new("RGB", (400, 300), "white")) as render,
            ):
                self.assertEqual(run_once(args), 0)
                self.assertEqual(run_once(args), 0)
            history_path = Path(td) / "epaper-quotas-history.json"
            history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(len(history["samples"]), 2)
            plotted = render.call_args.kwargs["history"]["ollama-pro"]["total"]
            self.assertEqual(plotted, [10.0, 20.0])


if __name__ == "__main__":
    unittest.main()
