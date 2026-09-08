"""Unit tests for 400×300 quota layout renderer."""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from quotas.layout import (  # noqa: E402
    W,
    H,
    BLACK,
    WHITE,
    DEFAULT_LAYOUT,
    _balance_parts,
    _relative_reset,
    _row_element_defaults,
    format_beijing,
    image_bwr_only,
    image_has_ink,
    image_has_red,
    load_layout,
    load_quotes,
    pick_display_windows,
    render_layout,
    render_quota_image,
    save_quota_png,
)
from quotas.models import QuotaRecord  # noqa: E402
import make_image  # noqa: E402


def sample_records():
    return [
        QuotaRecord(
            name="codex",
            status="ok",
            used_percent=42.0,
            remaining_percent=58.0,
            reset_at="2026-08-16T12:00:00+00:00",
            windows=[
                {
                    "label": "5h",
                    "used_percent": 10.0,
                    "remaining_percent": 90.0,
                    "reset_at": "2026-08-09T08:00:00+00:00",
                    "window_seconds": 18000,
                },
                {
                    "label": "week",
                    "used_percent": 42.0,
                    "remaining_percent": 58.0,
                    "reset_at": "2026-08-16T12:00:00+00:00",
                    "window_seconds": 604800,
                },
            ],
        ),
        QuotaRecord(
            name="grok",
            status="ok",
            used_percent=80.0,
            remaining_percent=20.0,
            reset_at="2026-08-09T02:33:59+00:00",
            windows=[
                {
                    "label": "week",
                    "used_percent": 80.0,
                    "remaining_percent": 20.0,
                    "reset_at": "2026-08-09T02:33:59+00:00",
                    "window_seconds": 604800,
                },
            ],
        ),
        QuotaRecord(
            name="kimi",
            status="unavailable",
            detail="no KIMI_API_KEY",
        ),
        QuotaRecord(
            name="opencode-go",
            status="ok",
            used_percent=12.5,
            remaining_percent=87.5,
            reset_at="2026-08-09T07:00:00+00:00",
            windows=[
                {
                    "label": "5h",
                    "used_percent": 12.5,
                    "remaining_percent": 87.5,
                    "reset_at": "2026-08-09T07:00:00+00:00",
                    "window_seconds": 18000,
                },
                {
                    "label": "week",
                    "used_percent": 40.0,
                    "remaining_percent": 60.0,
                    "reset_at": "2026-08-10T00:00:00+00:00",
                    "window_seconds": 604800,
                },
            ],
        ),
        QuotaRecord(
            name="ollama-pro",
            status="ok",
            used_percent=18.0,
            remaining_percent=82.0,
            windows=[
                {
                    "label": "5h",
                    "used_percent": 18.0,
                    "remaining_percent": 82.0,
                    "window_seconds": 18000,
                },
                {
                    "label": "week",
                    "used_percent": 9.0,
                    "remaining_percent": 91.0,
                    "window_seconds": 604800,
                },
            ],
        ),
        QuotaRecord(
            name="windsurf",
            status="ok",
            used_percent=24.0,
            remaining_percent=76.0,
            windows=[
                {
                    "label": "day",
                    "display_label": "day",
                    "used_percent": 24.0,
                    "remaining_percent": 76.0,
                    "window_seconds": 86400,
                },
                {
                    "label": "week",
                    "used_percent": 38.0,
                    "remaining_percent": 62.0,
                    "window_seconds": 604800,
                },
            ],
        ),
    ]


class LayoutTests(unittest.TestCase):
    def test_dimensions_and_ink(self):
        img = render_quota_image(sample_records())
        self.assertEqual(img.size, (W, H))
        self.assertEqual(img.size, (400, 300))
        self.assertTrue(image_has_ink(img))

    def test_pick_display_windows_5h_and_week(self):
        recs = {r.name: r for r in sample_records()}
        slots = pick_display_windows(recs["opencode-go"])
        self.assertEqual([s["kind"] for s in slots], ["5h", "week"])
        self.assertFalse(slots[0].get("missing"))
        self.assertFalse(slots[1].get("missing"))
        # grok only has week → 5h slot missing placeholder
        grok_slots = pick_display_windows(recs["grok"])
        self.assertTrue(grok_slots[0].get("missing"))
        self.assertFalse(grok_slots[1].get("missing"))

    def test_bwr_only_palette(self):
        """No grays — only pure black / white / red for e-paper contrast."""
        img = render_quota_image(sample_records())
        self.assertTrue(image_bwr_only(img))
        # red used for balance numbers + unavailable kimi
        self.assertTrue(image_has_red(img))

    def test_beijing_time_formatting(self):
        # UTC 12:00 → Beijing 20:00 same day
        self.assertEqual(format_beijing("2026-08-16T12:00:00+00:00"), "08-16 20:00")
        # UTC 02:33 → Beijing 10:33
        self.assertEqual(format_beijing("2026-08-09T02:33:59+00:00"), "08-09 10:33")

    def test_header_uses_beijing(self):
        from datetime import datetime, timezone

        # fixed UTC noon → header should show 20:00 BJ
        now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        img = render_quota_image(sample_records(), now=now)
        # ensure palette still pure
        self.assertTrue(image_bwr_only(img))
        # red plane should still get red pixels from alert rows
        black, red = make_image.build_planes(img.convert("RGB"))
        self.assertNotEqual(red, b"\xff" * len(red), "red plane should have ink")

    def test_market_ticker_is_rendered_between_calendar_and_time(self):
        market = {
            "items": [
                {"key": "sh", "change_percent": 0.65},
                {"key": "sz", "change_percent": 0.81},
                {"key": "spx", "change_percent": -0.02},
                {"key": "ndx", "change_percent": -0.08},
            ]
        }
        plain = render_quota_image(sample_records())
        ticker = render_quota_image(sample_records(), market=market)
        plain_px, ticker_px = plain.load(), ticker.load()
        changed = sum(
            1
            for y in range(0, 27)
            for x in range(118, 325)
            if plain_px[x, y] != ticker_px[x, y]
        )
        self.assertGreater(changed, 0, "market ticker should occupy the header middle")

    def test_balance_always_percent(self):
        # absolute remaining+limit must still render as a single percentage
        w = {"remaining": 41.0, "limit": 100.0, "remaining_percent": 41.0}
        self.assertEqual("".join(t for t, _ in _balance_parts(w)), "41%")
        w2 = {"remaining_percent": 100.0}
        self.assertEqual("".join(t for t, _ in _balance_parts(w2)), "100%")

    def test_relative_reset(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)  # BJ 08-09 20:00
        self.assertEqual(_relative_reset({"reset_at": "2026-08-12T14:00:00+00:00"}, now), "3天2时")
        self.assertEqual(_relative_reset({"reset_at": "2026-08-09T15:30:00+00:00"}, now), "3时30分")
        self.assertEqual(_relative_reset({"reset_at": "2026-08-09T12:35:00+00:00"}, now), "35分")
        self.assertEqual(_relative_reset({"reset_at": "2026-08-09T11:00:00+00:00"}, now), "已重置")
        self.assertEqual(_relative_reset({"missing": True}, now), "—")

    def test_services_use_full_width_rows(self):
        from datetime import datetime, timezone

        now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        img = render_quota_image(sample_records(), now=now)
        px = img.load()

        def ink(x0, x1, y0, y1):
            n = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if px[x, y] != WHITE:
                        n += 1
            return n

        # Codex occupies row 1 and Grok row 2. The right half of each row
        # contains its status/value, giving text twice the old card width.
        self.assertGreater(ink(150, 390, 46, 82), 0, "codex value should use the full-width row")
        self.assertGreater(ink(150, 390, 82, 118), 0, "grok value should use the full-width row")

    def test_daodejing_cache(self):
        quotes = load_quotes()
        self.assertGreaterEqual(len(quotes), 5, "local 道德经 cache should be populated")
        for q in quotes:
            self.assertTrue(q.get("q") and q.get("e"))

    def test_reset_times_not_clipped(self):
        """Full window lines (tag + balance + rst) must stay on-canvas.

        Regression: the 2x supersampled draw proxy used to double-scale the
        accumulated x, pushing the trailing reset times off-canvas so they
        never appeared on the panel.
        """
        img = render_quota_image(sample_records())
        px = img.load()

        def ink(x0, x1, y0, y1):
            n = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if px[x, y] != WHITE:
                        n += 1
            return n

        # Codex reset times are in the right side of the first service row.
        self.assertGreater(
            ink(150, 400, 50, 75),
            0,
            "codex reset time should be visible in the first row",
        )
        # opencode-go reset times remain visible in the fourth service row.
        self.assertGreater(
            ink(150, 400, 130, 170),
            0,
            "opencode-go reset line should stay visible in the fourth row",
        )

    def test_ollama_row_is_rendered_in_the_fifth_slot(self):
        img = render_quota_image(sample_records())
        px = img.load()
        row_ink = sum(
            1
            for y in range(167, 202)
            for x in range(0, 400)
            if px[x, y] != WHITE
        )
        self.assertGreater(row_ink, 0, "Ollama Pro should render in the fifth service row")

    def test_today_total_usage_sparkline_is_rendered_once(self):
        record = QuotaRecord(
            name="ollama-pro",
            status="ok",
            windows=[
                {"label": "5h", "remaining_percent": 90.0, "window_seconds": 18000},
                {"label": "week", "remaining_percent": 80.0, "window_seconds": 604800},
            ],
        )
        history = {
            "ollama-pro": {
                "total": [1.0, 2.0, 5.0, 4.0],
            }
        }
        plain = render_quota_image([record])
        plotted = render_quota_image([record], history=history)
        plain_px = plain.load()
        plotted_px = plotted.load()
        # The single total chart sits at the far right of the fifth row; no
        # text occupies this rectangle in the plain render.
        changed = sum(
            1
            for y in range(180, 202)
            for x in range(282, 396)
            if plain_px[x, y] != plotted_px[x, y]
        )
        self.assertGreater(changed, 0, "today total usage history should draw one sparkline")

    def test_blank_detection(self):
        from PIL import Image

        white = Image.new("RGB", (400, 300), (255, 255, 255))
        black = Image.new("RGB", (400, 300), (0, 0, 0))
        self.assertFalse(image_has_ink(white))
        self.assertFalse(image_has_ink(black))

    def test_frame_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            png = save_quota_png(sample_records(), td / "q.png")
            self.assertTrue(png.is_file())
            from PIL import Image as PILImage

            img = PILImage.open(png)
            self.assertEqual(img.size, (400, 300))
            black, red = make_image.build_planes(img.convert("RGB"))
            self.assertEqual(len(black), (400 * 300) // 8)
            self.assertEqual(len(red), (400 * 300) // 8)
            # red plane must be used (true 3-color)
            self.assertNotEqual(red, b"\xff" * len(red))
            frame = b"ZKEPD1\n" + struct.pack("<HH", 400, 300) + black + red
            out = td / "frame.bin"
            out.write_bytes(frame)
            raw = out.read_bytes()
            self.assertTrue(raw.startswith(b"ZKEPD1\n"))
            w, h = struct.unpack_from("<HH", raw, 7)
            self.assertEqual((w, h), (400, 300))
            planes = raw[11:]
            self.assertNotEqual(planes, b"\xff" * len(planes))

    def test_render_layout_default_matches_builtin(self):
        """DEFAULT_LAYOUT + render_layout should equal the built-in dashboard."""
        from unittest.mock import patch

        fixed = {"q": "上善若水", "e": "最高的善，就像水一样"}
        with patch("quotas.layout.random_quote", return_value=fixed):
            img_default = render_layout(sample_records(), DEFAULT_LAYOUT)
            img_builtin = render_quota_image(sample_records())

        def strip_border_rows(img):
            """Whiten 1px separator lines so block-border vs builtin-border
            conventions (off-by-one) don't count as content differences."""
            px = img.load()
            for y in range(H):
                colors = {px[x, y] for x in range(W)}
                if len(colors) == 1:
                    for x in range(W):
                        px[x, y] = WHITE
            return img

        a = strip_border_rows(img_default.copy())
        b = strip_border_rows(img_builtin.copy())
        self.assertEqual(a.size, (W, H))
        self.assertEqual(list(a.getdata()), list(b.getdata()))

    def test_render_layout_custom_block(self):
        """Moving a row block renders ink at the new position."""
        layout = {
            "version": 1,
            "canvas": {"w": W, "h": H},
            "blocks": [
                {
                    "id": "row-codex",
                    "type": "row",
                    "service": "codex",
                    "x": 0,
                    "y": 100,
                    "w": W,
                    "h": 60,
                    "border": True,
                }
            ],
        }
        img = render_layout(sample_records(), layout)
        self.assertEqual(img.size, (W, H))
        self.assertTrue(image_bwr_only(img))
        px = img.load()
        # Name/value ink should appear inside the moved block (y 100..160)
        row_ink = sum(
            1
            for y in range(100, 160)
            for x in range(0, 400)
            if px[x, y] != WHITE
        )
        self.assertGreater(row_ink, 0, "moved row block should render ink")

    def test_render_layout_elements_defaults_match_builtin(self):
        """Row blocks carrying the exported elements schema equal the built-in."""
        from unittest.mock import patch

        fixed = {"q": "上善若水", "e": "最高的善，就像水一样"}
        layout = json.loads(json.dumps(DEFAULT_LAYOUT))
        for b in layout["blocks"]:
            if b["type"] == "row":
                b["elements"] = _row_element_defaults()
        with patch("quotas.layout.random_quote", return_value=fixed):
            img = render_layout(sample_records(), layout)
            img_builtin = render_quota_image(sample_records())

        def strip_border_rows(img):
            px = img.load()
            for y in range(H):
                colors = {px[x, y] for x in range(W)}
                if len(colors) == 1:
                    for x in range(W):
                        px[x, y] = WHITE
            return img

        a = strip_border_rows(img.copy())
        b = strip_border_rows(img_builtin.copy())
        self.assertEqual(list(a.getdata()), list(b.getdata()))

    def test_render_layout_elements_per_element_tweak(self):
        """Hidden columns / moved logo change the rendered output."""
        layout = {
            "version": 1,
            "canvas": {"w": W, "h": H},
            "blocks": [
                {
                    "id": "row-codex",
                    "type": "row",
                    "service": "codex",
                    "x": 0,
                    "y": 46,
                    "w": W,
                    "h": 49,
                    "border": True,
                    "elements": _row_element_defaults(),
                }
            ],
        }
        base = render_layout(sample_records(), layout)
        layout["blocks"][0]["elements"]["col2"]["show"] = False
        layout["blocks"][0]["elements"]["logo"]["x"] = 40
        layout["blocks"][0]["elements"]["name"]["font"] = 24
        tweaked = render_layout(sample_records(), layout)
        self.assertNotEqual(list(base.getdata()), list(tweaked.getdata()))
        self.assertTrue(image_bwr_only(tweaked))
        px = tweaked.load()
        # Bigger name font should leave ink in a row that had none before
        self.assertTrue(any(px[x, y] != WHITE for y in range(70, 80) for x in range(0, 200)))

    def test_load_layout_roundtrip(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "layout.json"
            p.write_text(json.dumps(DEFAULT_LAYOUT), encoding="utf-8")
            loaded = load_layout(p)
            self.assertEqual(loaded["blocks"][0]["type"], "header")
            self.assertEqual(len(loaded["blocks"]), len(DEFAULT_LAYOUT["blocks"]))
            self.assertIsNone(load_layout(Path(td) / "nope.json"))
            self.assertIsNone(load_layout(None))


if __name__ == "__main__":
    unittest.main()
