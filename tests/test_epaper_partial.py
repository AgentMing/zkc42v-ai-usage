from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from epaper_partial import Rect, diff_rects, extract_plane_rect  # noqa: E402
from bleprobe import build_partial_steps, cmd_partial, raw_command, raw_data  # noqa: E402


def plane(width: int, height: int, updates: dict[tuple[int, int], int] | None = None) -> bytes:
    data = bytearray(b"\xff" * ((width * height) // 8))
    for (x_byte, y), value in (updates or {}).items():
        data[y * (width // 8) + x_byte] = value
    return bytes(data)


def write_frame(path: Path, width: int, height: int, black: bytes, red: bytes) -> None:
    import struct

    path.write_bytes(b"ZKEPD1\n" + struct.pack("<HH", width, height) + black + red)


class PartialRefreshTests(unittest.TestCase):
    def test_no_difference_has_no_rectangles(self):
        old_black = plane(32, 4)
        old_red = plane(32, 4)
        self.assertEqual(
            diff_rects(32, 4, old_black, old_red, old_black, old_red), []
        )

    def test_single_changed_byte_is_aligned_to_eight_pixels(self):
        old_black = plane(32, 4)
        new_black = plane(32, 4, {(1, 2): 0x00})
        rects = diff_rects(32, 4, old_black, plane(32, 4), new_black, plane(32, 4))
        self.assertEqual(rects, [Rect(8, 2, 8, 1)])

    def test_adjacent_rows_merge_into_one_window(self):
        old_black = plane(32, 5)
        new_black = plane(32, 5, {(1, 1): 0x00, (1, 2): 0x00, (1, 3): 0x00})
        rects = diff_rects(32, 5, old_black, plane(32, 5), new_black, plane(32, 5))
        self.assertEqual(rects, [Rect(8, 1, 8, 3)])

    def test_disjoint_spans_remain_separate(self):
        old_black = plane(32, 3)
        new_black = plane(32, 3, {(0, 0): 0x00, (2, 0): 0x00, (0, 2): 0x00})
        rects = diff_rects(32, 3, old_black, plane(32, 3), new_black, plane(32, 3))
        self.assertEqual(rects, [Rect(0, 0, 8, 1), Rect(16, 0, 8, 1), Rect(0, 2, 8, 1)])

    def test_red_plane_changes_are_included(self):
        old_red = plane(32, 2)
        new_red = plane(32, 2, {(2, 1): 0x00})
        rects = diff_rects(32, 2, plane(32, 2), old_red, plane(32, 2), new_red)
        self.assertEqual(rects, [Rect(16, 1, 8, 1)])

    def test_extract_rect_preserves_row_order(self):
        source = bytes(range(16))
        rect = Rect(8, 1, 16, 2)
        self.assertEqual(extract_plane_rect(source, 32, 4, rect), bytes([5, 6, 9, 10]))

    def test_partial_plan_uses_raw_window_commands_and_no_full_refresh(self):
        old = (32, 4, plane(32, 4), plane(32, 4))
        new = (32, 4, plane(32, 4, {(1, 1): 0x00}), plane(32, 4))
        rects, steps = build_partial_steps(old, new)
        payloads = [step[0] for step in steps]

        self.assertEqual(rects, [Rect(8, 1, 8, 1)])
        self.assertEqual(payloads[0], b"\x01\x02")
        self.assertEqual(payloads[1], b"\x31\x00\x00")
        self.assertIn(raw_command(0x44), payloads)
        self.assertIn(raw_command(0x45), payloads)
        self.assertIn(raw_command(0x24), payloads)
        self.assertIn(raw_command(0x26), payloads)
        self.assertIn(raw_command(0x22), payloads)
        self.assertEqual(payloads[-1], raw_command(0x20))
        self.assertNotIn(b"\x05", payloads)
        self.assertTrue(any(p == raw_data(b"\x00") for p in payloads))

    def test_raw_envelopes(self):
        self.assertEqual(raw_command(0x44), b"\x03\x44")
        self.assertEqual(raw_data(b"abc"), b"\x04abc")
        with self.assertRaises(ValueError):
            raw_data(b"")

    def test_red_change_uses_full_fallback_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = Path(td) / "old.bin"
            new_path = Path(td) / "new.bin"
            old_black = old_red = plane(32, 2)
            new_red = plane(32, 2, {(1, 1): 0x00})
            write_frame(old_path, 32, 2, old_black, old_red)
            write_frame(new_path, 32, 2, old_black, new_red)
            fallback = AsyncMock(return_value=7)
            with patch("bleprobe.cmd_send", fallback):
                result = asyncio.run(cmd_partial("tag", str(old_path), str(new_path)))
            self.assertEqual(result, 7)
            fallback.assert_awaited_once()

    def test_red_change_can_use_partial_transport_when_enabled(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = Path(td) / "old.bin"
            new_path = Path(td) / "new.bin"
            old_black = old_red = plane(32, 2)
            new_red = plane(32, 2, {(1, 1): 0x00})
            write_frame(old_path, 32, 2, old_black, old_red)
            write_frame(new_path, 32, 2, old_black, new_red)
            run_steps = AsyncMock(return_value=0)
            with patch("bleprobe._run_ble_steps", run_steps):
                result = asyncio.run(
                    cmd_partial("tag", str(old_path), str(new_path), allow_red=True)
                )
            self.assertEqual(result, 0)
            run_steps.assert_awaited_once()
            steps = run_steps.await_args.args[1]
            self.assertEqual(steps[-1][0], raw_command(0x20))

    def test_black_only_change_can_build_partial_transport_plan(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = Path(td) / "old.bin"
            new_path = Path(td) / "new.bin"
            old_black = old_red = plane(32, 2)
            new_black = plane(32, 2, {(1, 1): 0x00})
            write_frame(old_path, 32, 2, old_black, old_red)
            write_frame(new_path, 32, 2, new_black, old_red)
            run_steps = AsyncMock(return_value=0)
            with patch("bleprobe._run_ble_steps", run_steps):
                result = asyncio.run(cmd_partial("tag", str(old_path), str(new_path)))
            self.assertEqual(result, 0)
            run_steps.assert_awaited_once()
            steps = run_steps.await_args.args[1]
            self.assertEqual(steps[-1][0], raw_command(0x20))

    def test_transport_failure_retries_full_refresh(self):
        with tempfile.TemporaryDirectory() as td:
            old_path = Path(td) / "old.bin"
            new_path = Path(td) / "new.bin"
            old_black = old_red = plane(32, 2)
            new_black = plane(32, 2, {(1, 1): 0x00})
            write_frame(old_path, 32, 2, old_black, old_red)
            write_frame(new_path, 32, 2, new_black, old_red)
            run_steps = AsyncMock(return_value=1)
            fallback = AsyncMock(return_value=0)
            with patch("bleprobe._run_ble_steps", run_steps), patch("bleprobe.cmd_send", fallback):
                result = asyncio.run(cmd_partial("tag", str(old_path), str(new_path)))
            self.assertEqual(result, 0)
            fallback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
