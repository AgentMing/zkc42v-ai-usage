"""400×300 BWR (black/white/red) high-contrast layout for quota rows."""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .models import SERVICE_NAMES, QuotaRecord

W, H = 400, 300

# Pure BWR only — no grays (grays dither poorly / look washed on e-paper)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
# Pure red so make_image.classify() hits the red plane reliably
RED = (255, 0, 0)

BEIJING = ZoneInfo("Asia/Shanghai")

# Canonical short / long windows shown on each row
WINDOW_ORDER = ("5h", "week")

# Keep machine-readable service ids in QuotaRecord while using the more
# natural product label on the small display.
SERVICE_DISPLAY_NAMES = {
    "opencode-go": "opencode",
    "ollama-pro": "ollama pro",
    "windsurf": "windsurf",
}
MARKET_ORDER = ("sh", "sz", "spx", "ndx")
MARKET_LABELS = {"sh": "沪", "sz": "深", "spx": "标普", "ndx": "纳"}


def beijing_now(now: Optional[datetime] = None) -> datetime:
    """Return datetime in Beijing time (UTC+8)."""
    if now is None:
        return datetime.now(BEIJING)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def to_beijing(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        # Assume UTC when timezone-naive ISO from APIs
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING)


def format_beijing(dt_or_iso: datetime | str, fmt: str = "%m-%d %H:%M") -> str:
    """Format a datetime or ISO string as Beijing time."""
    if isinstance(dt_or_iso, str):
        s = dt_or_iso.strip()
        if not s:
            return ""
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return s[:16]
    else:
        dt = dt_or_iso
    return to_beijing(dt).strftime(fmt)


_FONTS_DIR = Path(__file__).resolve().parents[2] / "fonts"
_HOS_BOLD = _FONTS_DIR / "HarmonyOS_Sans_SC_Bold.ttf"
_HOS_REGULAR = _FONTS_DIR / "HarmonyOS_Sans_SC_Regular.ttf"


def _font(size: int, *, bold: bool = True) -> ImageFont.ImageFont:
    """Latin/digit stack following the manufacturer's own choice.

    ZKONG (ZKC42V's maker) officially licenses **Arial** for its ESL panels,
    citing its smooth, even edges as ideal for small screens; SES-imagotag's
    e-ink guide likewise says prefer heavy bold sans-serif at small sizes.
    So English/digits → Arial (Bold for display & data, Regular fallback).
    CJK stays on Heiti (STHeiti) via _cjk_font.
    """
    if bold:
        for p in (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        for p in (str(_HOS_BOLD), str(_HOS_REGULAR)):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
        roboto = _FONTS_DIR / "Roboto.ttf"
        if roboto.is_file():
            try:
                f = ImageFont.truetype(str(roboto), size)
                out = f.set_variation_by_axes([700])
                return out or f
            except Exception:
                pass
    else:
        for p in (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/System/Library/Fonts/Menlo.ttc",
            "/System/Library/Fonts/SFNSMono.ttf",
            str(_HOS_REGULAR),
            str(_HOS_BOLD),
            str(_FONTS_DIR / "Roboto.ttf"),
        ):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _remaining(rec: QuotaRecord) -> Optional[float]:
    if rec.remaining_percent is not None:
        return float(rec.remaining_percent)
    if rec.used_percent is not None:
        return max(0.0, 100.0 - float(rec.used_percent))
    return None


def _win_remaining_percent(w: dict[str, Any]) -> Optional[float]:
    if w.get("remaining_percent") is not None:
        try:
            return float(w["remaining_percent"])
        except (TypeError, ValueError):
            pass
    if w.get("used_percent") is not None:
        try:
            return max(0.0, 100.0 - float(w["used_percent"]))
        except (TypeError, ValueError):
            pass
    rem, lim = w.get("remaining"), w.get("limit")
    try:
        if rem is not None and lim is not None and float(lim) > 0:
            return max(0.0, min(100.0, float(rem) / float(lim) * 100.0))
    except (TypeError, ValueError):
        pass
    return None


def _win_used_percent(w: dict[str, Any]) -> Optional[float]:
    if w.get("used_percent") is not None:
        try:
            return float(w["used_percent"])
        except (TypeError, ValueError):
            pass
    rem = _win_remaining_percent(w)
    if rem is not None:
        return max(0.0, 100.0 - rem)
    return None


def _classify_window_kind(w: dict[str, Any]) -> Optional[str]:
    """Map a raw window dict to canonical '5h' or 'week' (else None)."""
    label = str(w.get("label") or "").strip().lower()
    secs = w.get("window_seconds")
    if secs is not None:
        try:
            s = float(secs)
            # 1h–8h → short rolling bucket; ≥5d → week
            if 3600 <= s <= 8 * 3600:
                return "5h"
            if s >= 5 * 86400:
                return "week"
        except (TypeError, ValueError):
            pass
    if any(k in label for k in ("rolling", "5h", "5-hour", "five hour", "secondary")):
        return "5h"
    if any(k in label for k in ("daily", "day", "24h")):
        # Windsurf exposes a daily bucket rather than the 5h bucket used by
        # other providers. It occupies the same aligned short-column slot;
        # the renderer preserves the truthful "day" label separately.
        return "5h"
    if any(k in label for k in ("weekly", "week", "primary")):
        # "primary" is often the weekly codex window when window_seconds missing
        if "primary" in label and secs is not None:
            try:
                if float(secs) < 5 * 86400:
                    return "5h"
            except (TypeError, ValueError):
                pass
        return "week"
    if label in ("monthly", "month"):
        return None
    return None


def pick_display_windows(rec: QuotaRecord) -> list[dict[str, Any]]:
    """Return up to two display slots: 5h then week (synthetic fallbacks ok)."""
    by_kind: dict[str, dict[str, Any]] = {}
    for w in rec.windows or []:
        if not isinstance(w, dict):
            continue
        kind = _classify_window_kind(w)
        if kind and kind not in by_kind:
            by_kind[kind] = dict(w)
            by_kind[kind]["kind"] = kind

    # Synthetic from top-level fields when a slot is empty
    if "week" not in by_kind and (rec.used_percent is not None or rec.remaining_percent is not None):
        # Prefer assigning top-level to week when no windows at all, or only 5h exists
        if not by_kind or "5h" in by_kind:
            by_kind.setdefault(
                "week",
                {
                    "kind": "week",
                    "label": "week",
                    "used_percent": rec.used_percent,
                    "remaining_percent": rec.remaining_percent,
                    "delta_percent": rec.delta_percent,
                    "remaining": rec.remaining,
                    "limit": rec.limit,
                    "reset_at": rec.reset_at,
                },
            )
        elif "week" not in by_kind and "5h" not in by_kind:
            by_kind["week"] = {
                "kind": "week",
                "label": "week",
                "used_percent": rec.used_percent,
                "remaining_percent": rec.remaining_percent,
                "delta_percent": rec.delta_percent,
                "remaining": rec.remaining,
                "limit": rec.limit,
                "reset_at": rec.reset_at,
            }

    # If only one unclassified window exists, put it on week slot
    if not by_kind and rec.windows:
        w0 = rec.windows[0]
        if isinstance(w0, dict):
            by_kind["week"] = {**w0, "kind": "week"}
    if not by_kind and rec.status == "ok":
        by_kind["week"] = {
            "kind": "week",
            "label": "week",
            "used_percent": rec.used_percent,
            "remaining_percent": rec.remaining_percent,
            "delta_percent": rec.delta_percent,
            "remaining": rec.remaining,
            "limit": rec.limit,
            "reset_at": rec.reset_at,
        }

    out: list[dict[str, Any]] = []
    for kind in WINDOW_ORDER:
        if kind in by_kind:
            slot = dict(by_kind[kind])
            slot["kind"] = kind
            out.append(slot)
        else:
            out.append({"kind": kind, "missing": True})
    return out


def _balance_parts(w: dict[str, Any]) -> list[tuple[str, tuple[int, int, int]]]:
    """Build (text, color) runs: remaining balance as a single percentage."""
    if w.get("missing"):
        return [("—", BLACK)]
    rem_pct = _win_remaining_percent(w)
    if rem_pct is not None:
        return [(f"{rem_pct:.0f}%", RED)]
    used_pct = _win_used_percent(w)
    if used_pct is not None:
        return [(f"{max(0.0, 100.0 - used_pct):.0f}%", RED)]
    return [("—", BLACK)]


def _delta_value(w: dict[str, Any]) -> Optional[float]:
    """Return a finite, display-friendly remaining-percent delta."""
    try:
        value = float(w.get("delta_percent"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    # Avoid showing tiny floating-point noise from API calculations.
    return 0.0 if abs(value) < 0.05 else value


def _delta_text(w: dict[str, Any]) -> Optional[str]:
    value = _delta_value(w)
    if value is None:
        return None
    sign = "+" if value > 0 else ""
    return f"Δ{sign}{value:.1f}"


def _history_values(history: Any, kind: str) -> list[float]:
    """Read a validated per-service usage series."""
    if not isinstance(history, dict):
        return []
    values = history.get(kind)
    if isinstance(values, dict):
        values = values.get("values")
    if not isinstance(values, (list, tuple)):
        return []
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(max(0.0, min(100.0, number)))
    return out


def _draw_sparkline(
    d,
    x: int,
    y: int,
    w: int,
    h: int,
    values: Any,
) -> None:
    """Draw a tiny today's-used-percent trend with a red current endpoint."""
    points_values = values if isinstance(values, (list, tuple)) else []
    clean: list[float] = []
    for value in points_values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            clean.append(max(0.0, min(100.0, number)))
    if not clean or w < 3 or h < 3:
        return

    low, high = min(clean), max(clean)
    span = high - low
    if span < 1.0:
        center = (low + high) / 2.0
        low = max(0.0, center - 5.0)
        high = min(100.0, center + 5.0)
    else:
        padding = max(1.0, span * 0.15)
        low = max(0.0, low - padding)
        high = min(100.0, high + padding)
    if high <= low:
        high = low + 1.0

    if len(clean) == 1:
        px = x + w // 2
        py = y + h // 2
        d.ellipse([px - 2, py - 2, px + 2, py + 2], fill=RED)
        return

    points = []
    for index, value in enumerate(clean):
        px = x + (w - 1) * index / (len(clean) - 1)
        py = y + h - 1 - (value - low) / (high - low) * (h - 1)
        points.append((px, py))
    d.line(points, fill=BLACK, width=2)
    px, py = points[-1]
    d.ellipse([px - 2, py - 2, px + 2, py + 2], fill=RED)


def _draw_total_usage_chart(
    d,
    x0: int,
    y0: int,
    config: dict[str, Any],
    history: dict[str, Any],
) -> None:
    """Draw one clearly labelled total-usage trend for a service row."""
    if not config.get("show", True):
        return
    label = str(config.get("label") or "总用量")
    d.text(
        (
            x0 + int(config.get("label_x", config.get("x", 282))),
            y0 + int(config.get("label_y", 0)),
        ),
        label,
        fill=BLACK,
        font=_cjk_font(int(config.get("label_font", 9))),
    )
    _draw_sparkline(
        d,
        x0 + int(config.get("x", 282)),
        y0 + int(config.get("y", 13)),
        int(config.get("w", 110)),
        int(config.get("h", 19)),
        _history_values(history, "total"),
    )


def _parse_dt(value: Any) -> Optional[datetime]:
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _relative_reset(w: dict[str, Any], now: Optional[datetime] = None) -> str:
    """Relative time until reset, e.g. '剩3天2时' / '剩2时30分' / '已重置'."""
    if w.get("missing") or not w.get("reset_at"):
        return "—"
    dt = _parse_dt(w["reset_at"])
    if dt is None:
        return "—"
    bj = beijing_now(now)
    delta = dt.astimezone(BEIJING) - bj
    if delta.total_seconds() <= 0:
        return "已重置"
    days, secs = delta.days, delta.seconds
    hours = secs // 3600
    mins = (secs % 3600) // 60
    if days > 0:
        return f"{days}天{hours}时"
    if hours > 0:
        return f"{hours}时{mins}分"
    return f"{mins}分"


def _draw_runs(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    parts: list[tuple[str, tuple[int, int, int]] | tuple[str, tuple[int, int, int], ImageFont.ImageFont]],
    font: ImageFont.ImageFont,
) -> int:
    """Draw runs left→right. Parts are (text, color) using `font`, or
    (text, color, run_font) to override the font for that run."""
    for part in parts:
        if len(part) == 3:
            text, color, run_font = part
        else:
            text, color = part
            run_font = font
        if not text:
            continue
        d.text((x, y), text, fill=color, font=run_font)
        bb = d.textbbox((x, y), text, font=run_font)
        x = bb[2]
    return x


def _is_alert(rec: QuotaRecord) -> bool:
    if rec.status != "ok":
        return True
    rem = _remaining(rec)
    if rem is not None and rem <= 15:
        return True
    for w in pick_display_windows(rec):
        if w.get("missing"):
            continue
        wr = _win_remaining_percent(w)
        if wr is not None and wr <= 15:
            return True
    return False


_QUOTES: list[dict[str, str]] | None = None


def load_quotes() -> list[dict[str, str]]:
    """Load 道德经 quotes + explanations from the local cache (once)."""
    global _QUOTES
    if _QUOTES is None:
        p = Path(__file__).resolve().parent / "daodejing.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _QUOTES = [x for x in data if isinstance(x, dict) and x.get("q") and x.get("e")]
        except Exception:
            _QUOTES = []
    return _QUOTES


def random_quote(now: Optional[datetime] = None) -> dict[str, str]:
    quotes = load_quotes()
    if not quotes:
        return {"q": "", "e": ""}
    return dict(random.choice(quotes))


def _wrap_cjk(d, text: str, font, max_w: int, max_lines: int) -> list[str]:
    """Wrap Chinese text into ≤ max_lines lines (breaks after punctuation)."""
    lines: list[str] = []
    while text and len(lines) < max_lines:
        if d.textbbox((0, 0), text, font=font)[2] <= max_w:
            lines.append(text)
            return lines
        lo, hi = 1, len(text)
        while lo < hi:  # longest prefix that fits
            mid = (lo + hi + 1) // 2
            if d.textbbox((0, 0), text[:mid], font=font)[2] <= max_w:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for i in range(lo, 1, -1):  # break after punctuation if possible
            if text[i - 1] in "，。；、：！？":
                cut = i
                break
        lines.append(text[:cut])
        text = text[cut:].lstrip()
    if text and lines:
        lines[-1] = lines[-1][:-1] + "…"
    elif text:
        lines.append(text[: max(1, max_w // font.size)] + "…")
    return lines


def quantize_bwr(img: Image.Image) -> Image.Image:
    """Force every pixel to pure black / white / red (kill TTF anti-alias grays).

    Matches make_image.classify() intent so the red plane is used and text
    edges stay hard for e-paper contrast.
    """
    src = img.convert("RGB")
    out = Image.new("RGB", src.size, WHITE)
    sp = src.load()
    op = out.load()
    w, h = src.size
    for y in range(h):
        for x in range(w):
            r, g, b = sp[x, y]
            # red-ish (same spirit as make_image.classify)
            if r > 120 and r > g * 1.6 and r > b * 1.6 and (g + b) < 300:
                op[x, y] = RED
            elif (r + g + b) / 3 < 160:
                # slightly softer threshold so AA gray edges become solid ink
                op[x, y] = BLACK
            else:
                op[x, y] = WHITE
    return out


class _ScaledDraw:
    """ImageDraw proxy that scales all geometry by `scale` (for supersampling)."""

    def __init__(self, draw: ImageDraw.ImageDraw, scale: int):
        self._d = draw
        self._s = scale

    @staticmethod
    def _scale(v, s: int):
        if isinstance(v, (int, float)):
            return int(v * s)
        if isinstance(v, (list, tuple)):
            return type(v)(_ScaledDraw._scale(x, s) for x in v)
        return v

    def text(self, xy, text: str, **kw):
        self._d.text(self._scale(xy, self._s), text, **kw)

    def textbbox(self, xy, text: str, **kw):
        # Return bbox in INPUT (unscaled) coordinates so callers like
        # _draw_runs can accumulate x in the same space the proxy scales —
        # otherwise x is scaled twice and later segments drift off-canvas.
        bb = self._d.textbbox(self._scale(xy, self._s), text, **kw)
        return tuple(v // self._s for v in bb)

    def rectangle(self, xy, **kw):
        if kw.get("width"):
            kw = dict(kw, width=kw["width"] * self._s)
        self._d.rectangle(self._scale(xy, self._s), **kw)

    def line(self, xy, **kw):
        if kw.get("width"):
            kw = dict(kw, width=int(kw["width"] * self._s))
        self._d.line(self._scale(xy, self._s), **kw)

    def polygon(self, xy, **kw):
        if kw.get("width"):
            kw = dict(kw, width=int(kw["width"] * self._s))
        self._d.polygon(self._scale(xy, self._s), **kw)

    def ellipse(self, xy, **kw):
        if kw.get("width"):
            kw = dict(kw, width=int(kw["width"] * self._s))
        self._d.ellipse(self._scale(xy, self._s), **kw)


def _hexagon(cx: float, cy: float, r: float, rot: float = 0.0) -> list[tuple[float, float]]:
    return [
        (cx + r * math.cos(rot + i * math.pi / 3), cy + r * math.sin(rot + i * math.pi / 3))
        for i in range(6)
    ]


def draw_logo(d, x: float, y: float, size: float, kind: str, ink, bg) -> None:
    """Small monochrome geometric brand mark, centered at (x, y) in a `size` box.

    Simplified shapes (BWR panel has no grays or midtones), drawn in the tile's
    ink colour: codex → OpenAI-style hexagon knot, grok → xAI "X",
    kimi → crescent moon, opencode-go → terminal prompt chevron,
    ollama-pro → llama head, windsurf → W mark.
    """
    h = size / 2.0
    if kind == "codex":
        d.polygon(_hexagon(x, y, h * 0.96), fill=ink)
        d.polygon(_hexagon(x, y, h * 0.44, rot=math.pi / 6), fill=bg)
    elif kind == "grok":
        w = max(2.0, size * 0.16)
        d.line([(x - h * 0.8, y - h * 0.8), (x + h * 0.8, y + h * 0.8)], fill=ink, width=w)
        d.line([(x - h * 0.8, y + h * 0.8), (x + h * 0.8, y - h * 0.8)], fill=ink, width=w)
    elif kind == "kimi":
        d.ellipse([x - h, y - h, x + h, y + h], fill=ink)
        d.ellipse([x - h * 0.35, y - h * 0.9, x + h * 0.95, y + h * 0.9], fill=bg)
    elif kind == "opencode":
        w = max(2.0, size * 0.17)
        d.line(
            [(x - h * 0.6, y - h * 0.72), (x + h * 0.55, y), (x - h * 0.6, y + h * 0.72)],
            fill=ink,
            width=w,
            joint="curve",
        )
    elif kind == "ollama":
        # Small geometric llama mark that survives the panel's BWR palette.
        d.ellipse([x - h * 0.62, y - h * 0.58, x + h * 0.62, y + h * 0.78], fill=ink)
        d.polygon(
            [(x - h * 0.55, y - h * 0.45), (x - h * 0.9, y - h * 0.95), (x - h * 0.18, y - h * 0.7)],
            fill=ink,
        )
        d.polygon(
            [(x + h * 0.55, y - h * 0.45), (x + h * 0.9, y - h * 0.95), (x + h * 0.18, y - h * 0.7)],
            fill=ink,
        )
    elif kind == "windsurf":
        line_w = max(1.5, size * 0.15)
        d.line(
            [
                (x - h * 0.9, y - h * 0.45),
                (x - h * 0.35, y + h * 0.55),
                (x, y - h * 0.1),
                (x + h * 0.35, y + h * 0.55),
                (x + h * 0.9, y - h * 0.45),
            ],
            fill=ink,
            width=line_w,
            joint="curve",
        )


def _cjk_font(size: int, *, bold: bool = True) -> ImageFont.ImageFont:
    """CJK text (农历/节气/干支/道德经) — HarmonyOS Sans SC by default.

    Huawei's HarmonyOS Sans has even, generous strokes that survive the 1-bit
    BWR quantization far better than Microsoft YaHei's thinner glyphs at panel
    resolution; it also covers Latin, keeping mixed text consistent. Falls back
    to Heiti (macOS) / SimHei-YaHei (Windows).
    """
    if bold:
        candidates = [str(_HOS_BOLD), str(_HOS_REGULAR)]
    else:
        candidates = [str(_HOS_REGULAR), str(_HOS_BOLD)]
    candidates += [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def china_calendar_line(now: Optional[datetime] = None) -> str:
    """Compact Chinese calendar line: '丙午年 · 立秋 · 六月廿七'."""
    try:
        from lunar_python import Lunar
    except Exception:
        return ""
    try:
        bj = beijing_now(now).replace(tzinfo=None)
        lunar = Lunar.fromDate(bj)
        month = lunar.getMonthInChinese() or ""
        day = lunar.getDayInChinese() or ""
        lunar_str = f"{month}月{day}" if month and day else ""
        year_gz = lunar.getYearInGanZhi() or ""
        now_s = bj.strftime("%Y-%m-%d %H:%M:%S")
        jieqi = ""
        for name, s in lunar.getJieQiTable().items():
            if s.toYmdHms() <= now_s:
                jieqi = name
        return " · ".join(p for p in (f"{year_gz}年" if year_gz else "", jieqi, lunar_str) if p)
    except Exception:
        return ""


def render_quota_image(
    records: Iterable[QuotaRecord],
    *,
    title: str = "AI Quotas",
    now: Optional[datetime] = None,
    history: Optional[dict[str, dict[str, Any]]] = None,
    market: Optional[dict[str, Any]] = None,
) -> Image.Image:
    """Render a compact, high-contrast BWR quota dashboard.

    The services use full-width rows.  A 400 px e-paper panel is much
    easier to read this way than as narrow cards: names never collide
    with values, error details do not truncate as aggressively, and the
    important percentages can stay large.
    """
    recs = list(records)
    history = history or {}
    # Render at the panel's native resolution. FreeType's hinting can then snap
    # stems to the actual pixel grid. Supersampling + LANCZOS followed by BWR
    # thresholding creates broken gray edge fragments on a physical 1-bit
    # plane, which look much rougher than the source PNG suggests.
    S = 1
    img = Image.new("RGB", (W * S, H * S), WHITE)
    d = _ScaledDraw(ImageDraw.Draw(img), S)

    bj = beijing_now(now)

# Font sizes are physical display pixels so font hinting matches the panel.
    header_time_f = _font(14 * S, bold=True)
    header_cjk_f = _cjk_font(9 * S)
    quote_f = _cjk_font(19 * S)
    quote_explain_f = _cjk_font(12 * S, bold=True)

    # --- Header: compact Chinese calendar, market ticker, and clock. ---
    # The title is redundant on a dedicated quota tag. Keep the calendar and
    # timestamp on one compact line so the sixth provider still gets a readable
    # row below it.
    header_h = 27
    time_str = bj.strftime("%m-%d %H:%M")
    tw = d.textbbox((0, 0), time_str, font=header_time_f)[2]
    d.text((W - 10 - tw, 3), time_str, fill=BLACK, font=header_time_f)
    _draw_market_header(d, market, 0, 5, W, W - 10 - tw, 9)
    d.text((10, 3), china_calendar_line(now), fill=BLACK, font=header_cjk_f)
    d.line([(0, header_h - 1), (W, header_h - 1)], fill=BLACK, width=1)

    # --- Bottom bar: quote + explanation, compact and always on-canvas. ---
    # quote_f.size includes the supersampling factor; divide by S when
    # calculating line spacing. The previous code used the scaled size here,
    # which made long quotes run off the bottom of the panel.
    quote = random_quote()
    q_lines: list[str] = []
    e_lines: list[str] = []
    q_line_h = max(1, round(quote_f.size / S * 1.12))
    e_line_h = max(1, round(quote_explain_f.size / S * 1.16))
    if quote.get("q"):
        q_lines = _wrap_cjk(d, quote["q"], quote_f, W - 16, 1)
        e_lines = _wrap_cjk(d, quote["e"], quote_explain_f, W - 16, 1)

    # Shrink the quote panel for short quotes and let the service cards use the
    # recovered pixels. Long quotes still get enough height for two lines of
    # text plus a small top/bottom breathing room.
    quote_content_h = 7 + q_line_h * len(q_lines) + 4 + e_line_h * len(e_lines) + 7
    # Reserve a compact lower band; full-width service rows use the recovered
    # height for larger, sturdier glyphs.
    # Six provider rows need a compact but still legible lower band.
    quote_h = max(58, quote_content_h)
    quote_y0 = H - quote_h
    if q_lines:
        quote_block_h = q_line_h * len(q_lines) + 4 + e_line_h * len(e_lines)
        y = quote_y0 + max(7, (quote_h - quote_block_h) // 2)
        for ln in q_lines:
            d.text((8, y), ln, fill=BLACK, font=quote_f)
            y += q_line_h
        y += 4
        for ln in e_lines:
            d.text((8, y), ln, fill=BLACK, font=quote_explain_f)
            y += e_line_h
    d.line([(0, quote_y0 - 1), (W, quote_y0 - 1)], fill=BLACK, width=1)

# --- Service area: one full-width row per provider. ---
    rows = SERVICE_NAMES
    top = header_h
    row_h = (quote_y0 - top) // len(rows)
    records_by_name = {r.name: r for r in recs}

    for ri, name in enumerate(rows):
        y0 = top + ri * row_h
        if ri > 0:
            d.line([(0, y0), (W, y0)], fill=BLACK, width=1)
        rec = records_by_name.get(name) or QuotaRecord(name=name, status="unavailable", detail="missing")
        _draw_service_row(d, 0, y0, W, row_h, rec, now, history=history.get(name))

    # Snap FreeType's native-resolution antialiasing to the three panel inks.
    return quantize_bwr(img)


def image_has_ink(img: Image.Image, *, min_ink_pixels: int = 200) -> bool:
    """True if image is substantially non-blank (not all white/all black)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    ink = 0
    white = 0
    black = 0
    red = 0
    px = rgb.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            avg = (r + g + b) / 3
            if r > 200 and g < 40 and b < 40:
                red += 1
                ink += 1
            elif avg < 40:
                black += 1
                ink += 1
            elif avg > 240:
                white += 1
            else:
                ink += 1
    total = w * h
    if black > total * 0.98 or white > total * 0.98:
        return False
    return ink >= min_ink_pixels


def image_bwr_only(img: Image.Image) -> bool:
    """True if every pixel is pure black, white, or pure red (no gray/other)."""
    px = img.convert("RGB").load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if p not in (BLACK, WHITE, RED):
                return False
    return True


def image_has_red(img: Image.Image) -> bool:
    px = img.convert("RGB").load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            if px[x, y] == RED:
                return True
    return False


def save_quota_png(
    records: Iterable[QuotaRecord],
    path: str | Path,
    history: Optional[dict[str, dict[str, Any]]] = None,
    market: Optional[dict[str, Any]] = None,
) -> Path:
    path = Path(path)
    img = render_quota_image(records, history=history, market=market)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# Config-driven layout (design tool → JSON → this renderer)
# ---------------------------------------------------------------------------

def _row_element_defaults() -> dict[str, Any]:
    """Default per-element config for a service row (matches render_quota_image).

    Each row's inner elements (logo / name / subtitle / balance columns / hero /
    error state) are individually positionable via a row block's ``elements``
    dict; a missing key falls back to these values.
    """
    return {
        "logo": {"show": True, "x": 9, "cy": 0.5, "size": 11},
        "name": {"show": True, "x": 22, "y": 4, "font": 12, "color": "auto"},
        "subtitle": {"show": False, "x": 29, "y": 22, "font": 8, "color": "black"},
        "status": {"show": True, "x": 101, "y": 2, "font": 12},
        "status_msg": {"show": True, "x": 101, "y": 21, "font": 9},
        # Keep the legacy single-window hero opt-in only. The default fixed
        # columns keep a provider's value/reset vertically aligned with all
        # other rows, including providers that only expose the weekly window.
        "hero": {"show": False, "right": 12, "y": -2, "font": 26},
        "chart": {
            "show": True, "label": "总用量", "label_x": 282, "label_y": 0,
            "label_font": 9, "x": 282, "y": 13, "w": 110, "h": 19,
        },
        "col1": {
            "show": True, "x": 95, "tag_y": 0, "tag_font": 10,
            "balance_dx": 13, "balance_y": 0, "balance_font": 20,
            "delta_dx": 3, "delta_y": 5, "delta_font": 8,
            "detail_y": 28, "detail_font": 9,
        },
        "col2": {
            "show": True, "x": 195, "tag_y": 0, "tag_font": 10,
            "balance_dx": 13, "balance_y": 0, "balance_font": 20,
            "delta_dx": 3, "delta_y": 5, "delta_font": 8,
            "detail_y": 28, "detail_font": 9,
        },
    }


def _row_elements_for_block(b: dict[str, Any]) -> dict[str, Any]:
    """Merge a row block's ``elements`` sub-config over the defaults.

    Legacy flat keys (name_font, sub_font, ...) are honored when ``elements``
    is absent, so older exported layouts keep working.
    """
    base = _row_element_defaults()
    el = b.get("elements") or {}
    legacy = {
        "name": {"x": 22, "y": 4, "font": b.get("name_font")},
        "subtitle": {"x": 29, "y": 22, "font": b.get("sub_font")},
        "hero": {"right": 12, "y": -2, "font": b.get("hero_font")},
        "col1": {"balance_font": b.get("balance_font"), "tag_font": b.get("latin_font"), "detail_font": b.get("detail_font")},
        "col2": {"balance_font": b.get("balance_font"), "tag_font": b.get("latin_font"), "detail_font": b.get("detail_font")},
        "logo": {"size": b.get("logo_size")},
    }
    out = {}
    for key, defaults in base.items():
        sub = dict(defaults)
        if isinstance(el.get(key), dict):
            sub.update({k: v for k, v in el[key].items() if v is not None})
        for k, v in (legacy.get(key) or {}).items():
            if v is not None:
                sub[k] = v
        out[key] = sub
    return out


def _draw_service_row(
    d,
    x0: int,
    y0: int,
    w: int,
    h: int,
    rec: QuotaRecord,
    now: Optional[datetime],
    el: Optional[dict[str, Any]] = None,
    history: Optional[dict[str, Any]] = None,
) -> None:
    """Draw one service row inside the rect (x0,y0,w,h).

    ``el`` is the merged per-element config (see _row_element_defaults); when
    omitted the built-in dashboard defaults are used.
    """
    if el is None:
        el = _row_element_defaults()
    history = history or {}
    kind_label = {"5h": "5h", "week": "wk"}
    alert = _is_alert(rec)
    logo = el["logo"]
    name_el = el["name"]
    sub_el = el["subtitle"]
    hero = el["hero"]

    def _el_color(sub: dict[str, Any]) -> tuple[int, int, int]:
        c = str(sub.get("color") or "auto")
        if c == "red":
            return RED
        if c == "black":
            return BLACK
        return RED if alert else BLACK

    if logo.get("show", True):
        cy = y0 + float(logo.get("cy", 0.5)) * h
        logo_kind = {
            "opencode-go": "opencode",
            "ollama-pro": "ollama",
            "windsurf": "windsurf",
        }.get(rec.name, rec.name)
        draw_logo(d, x0 + int(logo.get("x", 16)), cy, float(logo.get("size", 17)), logo_kind, BLACK, WHITE)
    if name_el.get("show", True):
        d.text(
            (x0 + int(name_el.get("x", 29)), y0 + int(name_el.get("y", 4))),
            SERVICE_DISPLAY_NAMES.get(rec.name, rec.name),
            fill=_el_color(name_el),
            font=_font(int(name_el.get("font", 17)), bold=True),
        )

    detail = (rec.detail or "").lower()
    if rec.name == "codex":
        subtitle = "PLUS · OFFICIAL" if "plus" in detail else "OFFICIAL"
    elif rec.name == "grok":
        subtitle = "WEEKLY" if "weekly" in detail else "GROK BUILD"
    elif rec.name == "kimi":
        subtitle = "CODING PLAN"
    elif rec.name == "ollama-pro":
        subtitle = "PRO · CLOUD"
    elif rec.name == "windsurf":
        subtitle = "WINDSURF"
    else:
        subtitle = "GO PLAN"
    if sub_el.get("show", True):
        d.text(
            (x0 + int(sub_el.get("x", 29)), y0 + int(sub_el.get("y", 28))),
            subtitle,
            fill=_el_color(sub_el),
            font=_font(int(sub_el.get("font", 10)), bold=True),
        )

    if rec.status != "ok":
        st = el["status"]
        if st.get("show", True):
            d.text(
                (x0 + int(st.get("x", 151)), y0 + int(st.get("y", 4))),
                rec.status.upper(),
                fill=RED,
                font=_font(int(st.get("font", 16)), bold=True),
            )
        sm = el["status_msg"]
        if sm.get("show", True):
            msg = rec.detail or "no details"
            if len(msg) > 36:
                msg = msg[:33] + "..."
            d.text(
                (x0 + int(sm.get("x", 151)), y0 + int(sm.get("y", 27))),
                msg,
                fill=BLACK,
                font=_font(int(sm.get("font", 12)), bold=True),
            )
        return

    slots = pick_display_windows(rec)
    real_windows = [wd for wd in slots if not wd.get("missing")]
    if len(real_windows) == 1 and hero.get("show", False):
        wd = real_windows[0]
        tag = str(wd.get("display_label") or kind_label.get(str(wd.get("kind") or "?"), "?"))
        if hero.get("show", True):
            hero_txt = "".join(t for t, _ in _balance_parts(wd))
            hero_f = _font(int(hero.get("font", 30)), bold=True)
            hero_w = d.textbbox((0, 0), hero_txt, font=hero_f)[2]
            hero_x = x0 + w - int(hero.get("right", 12)) - hero_w
            hero_y = y0 + int(hero.get("y", -4))
            d.text(
                (hero_x, hero_y),
                hero_txt,
                fill=RED,
                font=hero_f,
            )
            delta = _delta_text(wd)
            if delta:
                delta_f = _font(9, bold=True)
                delta_w = d.textbbox((0, 0), delta, font=delta_f)[2]
                d.text(
                    (hero_x - delta_w - 4, hero_y + 7),
                    delta,
                    fill=RED if (_delta_value(wd) or 0) < 0 else BLACK,
                    font=delta_f,
                )
        c1 = el["col1"]
        if c1.get("show", True):
            d.text(
                (x0 + int(c1.get("x", 151)), y0 + int(c1.get("detail_y", 30))),
                f"{tag}  {_relative_reset(wd, now)}",
                fill=BLACK,
                font=_cjk_font(int(c1.get("detail_font", 13))),
            )
    else:
        # Always draw both canonical slots, including a missing placeholder.
        # This keeps the weekly value and reset line in the same x-column as
        # the corresponding values on every other provider row.
        for col_key, wd in zip(("col1", "col2"), slots):
            col = el[col_key]
            if not col.get("show", True):
                continue
            tag = str(wd.get("display_label") or kind_label.get(str(wd.get("kind") or "?"), "?"))
            cx = x0 + int(col.get("x", 151))
            d.text(
                (cx, y0 + int(col.get("tag_y", 2))),
                tag,
                fill=BLACK,
                font=_font(int(col.get("tag_font", 13)), bold=True),
            )
            balance_f = _font(int(col.get("balance_font", 22)), bold=True)
            balance_end = _draw_runs(
                d,
                cx + int(col.get("balance_dx", 25)),
                y0 + int(col.get("balance_y", 0)),
                [(t, c, balance_f) for t, c in _balance_parts(wd)],
                balance_f,
            )
            delta = _delta_text(wd)
            if delta:
                delta_f = _font(int(col.get("delta_font", 9)), bold=True)
                d.text(
                    (
                        balance_end + int(col.get("delta_dx", 3)),
                        y0 + int(col.get("delta_y", 4)),
                    ),
                    delta,
                    fill=RED if (_delta_value(wd) or 0) < 0 else BLACK,
                    font=delta_f,
                )
            d.text(
                (cx, y0 + int(col.get("detail_y", 30))),
                _relative_reset(wd, now),
                fill=BLACK,
                font=_cjk_font(int(col.get("detail_font", 13))),
            )

    if rec.status == "ok":
        _draw_total_usage_chart(d, x0, y0, el["chart"], history)


def _market_runs(market: Optional[dict[str, Any]]) -> list[tuple[str, tuple[int, int, int]]]:
    """Build compact header runs for China/US index daily changes."""
    if not isinstance(market, dict):
        return []
    by_key = {
        str(item.get("key")): item
        for item in (market.get("items") or [])
        if isinstance(item, dict) and item.get("key")
    }
    runs: list[tuple[str, tuple[int, int, int]]] = []
    for index, key in enumerate(MARKET_ORDER):
        item = by_key.get(key)
        if item is None:
            continue
        try:
            change = float(item.get("change_percent"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(change):
            continue
        if runs:
            runs.append(("  |  " if key == "spx" else "  ", BLACK))
        sign = "+" if change >= 0 else ""
        label = MARKET_LABELS.get(key, str(item.get("label") or key))
        runs.append((f"{label}{sign}{change:.1f}%", RED if change > 0 else BLACK))
    return runs


def _draw_market_header(
    d,
    market: Optional[dict[str, Any]],
    x: int,
    y: int,
    w: int,
    time_x: int,
    font_size: int = 9,
) -> None:
    runs = _market_runs(market)
    if not runs:
        return
    font = _cjk_font(font_size)
    market_width = sum(d.textbbox((0, 0), text, font=font)[2] for text, _ in runs)
    left = x + 126
    right = time_x - 8
    available = right - left
    if market_width > available:
        font = _cjk_font(max(8, font_size - 1))
        market_width = sum(d.textbbox((0, 0), text, font=font)[2] for text, _ in runs)
    if market_width > available:
        left = x + 8
    else:
        left += max(0, (available - market_width) // 2)
    _draw_runs(d, left, y, [(text, color, font) for text, color in runs], font)


def _draw_header_block(
    d,
    b: dict[str, Any],
    x: int,
    y: int,
    w: int,
    h: int,
    now: datetime,
    market: Optional[dict[str, Any]] = None,
) -> None:
    title_f = _font(int(b.get("title_font", 13)), bold=True)
    time_f = _font(int(b.get("time_font", 14)), bold=True)
    cjk_f = _cjk_font(int(b.get("cjk_font", 9)))
    if b.get("show_title", True):
        d.text((x + 10, y + 5), str(b.get("title") or "AI Quotas").upper(), fill=BLACK, font=title_f)
    if b.get("show_time", True):
        time_str = now.strftime("%m-%d %H:%M")
        tw = d.textbbox((0, 0), time_str, font=time_f)[2]
        d.text((x + w - 10 - tw, y + int(b.get("time_y", 3))), time_str, fill=BLACK, font=time_f)
        _draw_market_header(
            d,
            market,
            x,
            y + int(b.get("market_y", 5)),
            w,
            x + w - 10 - tw,
            int(b.get("market_font", 9)),
        )
    if b.get("show_calendar", True):
        calendar_y = b.get("calendar_y")
        if calendar_y is None:
            calendar_y = 28 if b.get("show_title", True) else 5
        d.text((x + 10, y + int(calendar_y)), china_calendar_line(now), fill=BLACK, font=cjk_f)
    if b.get("border", True):
        d.line([(x, y + h - 1), (x + w, y + h - 1)], fill=BLACK, width=1)


def _draw_quote_block(d, b: dict[str, Any], x: int, y: int, w: int, h: int) -> None:
    if b.get("border", True):
        d.line([(x, y), (x + w, y)], fill=BLACK, width=1)
    quote = random_quote()
    if not quote.get("q"):
        return
    quote_f = _cjk_font(int(b.get("quote_font", 19)))
    explain_f = _cjk_font(int(b.get("explain_font", 12)), bold=True)
    max_w = max(8, w - 16)
    q_lines = _wrap_cjk(d, quote["q"], quote_f, max_w, 1)
    e_lines = _wrap_cjk(d, quote["e"], explain_f, max_w, 1)
    q_line_h = max(1, round(quote_f.size * 1.12))
    e_line_h = max(1, round(explain_f.size * 1.16))
    block_h = q_line_h * len(q_lines) + 4 + e_line_h * len(e_lines)
    yy = y + max(7, (h - block_h) // 2)
    for ln in q_lines:
        d.text((x + 8, yy), ln, fill=BLACK, font=quote_f)
        yy += q_line_h
    yy += 4
    for ln in e_lines:
        d.text((x + 8, yy), ln, fill=BLACK, font=explain_f)
        yy += e_line_h


def render_layout(
    records: Iterable[QuotaRecord],
    layout: dict[str, Any],
    now: Optional[datetime] = None,
    history: Optional[dict[str, dict[str, Any]]] = None,
    market: Optional[dict[str, Any]] = None,
) -> Image.Image:
    """Render from a designer-exported layout JSON (see DEFAULT_LAYOUT schema).

    Each block is drawn inside its own (x, y, w, h) rect, so the web design
    tool's drag/resize positions map 1:1 onto the panel.
    """
    blocks = (layout or {}).get("blocks") or []
    history = history or {}
    img = Image.new("RGB", (W, H), WHITE)
    d = _ScaledDraw(ImageDraw.Draw(img), 1)
    recs = {r.name: r for r in records}
    bj = beijing_now(now)
    for b in blocks:
        btype = b.get("type")
        x = int(b.get("x", 0))
        y = int(b.get("y", 0))
        w = int(b.get("w", W))
        h = int(b.get("h", 40))
        if btype == "header":
            _draw_header_block(d, b, x, y, w, h, bj, market=market)
        elif btype == "row":
            service = str(b.get("service") or "codex")
            rec = recs.get(service) or QuotaRecord(name=service, status="unavailable", detail="missing")
            if b.get("border", True):
                d.line([(x, y), (x + w, y)], fill=BLACK, width=1)
            _draw_service_row(
                d,
                x,
                y,
                w,
                h,
                rec,
                now,
                _row_elements_for_block(b),
                history=(history or {}).get(service),
            )
        elif btype == "quote":
            _draw_quote_block(d, b, x, y, w, h)
    return quantize_bwr(img)


DEFAULT_LAYOUT: dict[str, Any] = {
    "version": 1,
    "canvas": {"w": W, "h": H},
    "blocks": [
        {
            "id": "header",
            "type": "header",
            "x": 0,
            "y": 0,
            "w": W,
            "h": 27,
            "title": "AI Quotas",
            "title_font": 13,
            "time_font": 14,
            "cjk_font": 9,
            "market_font": 9,
            "show_title": False,
            "show_time": True,
            "show_calendar": True,
            "calendar_y": 3,
            "border": True,
        },
        {
            "id": "row-codex",
            "type": "row",
            "service": "codex",
            "x": 0,
            "y": 27,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "row-grok",
            "type": "row",
            "service": "grok",
            "x": 0,
            "y": 62,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "row-kimi",
            "type": "row",
            "service": "kimi",
            "x": 0,
            "y": 97,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "row-opencode-go",
            "type": "row",
            "service": "opencode-go",
            "x": 0,
            "y": 132,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "row-ollama-pro",
            "type": "row",
            "service": "ollama-pro",
            "x": 0,
            "y": 167,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "row-windsurf",
            "type": "row",
            "service": "windsurf",
            "x": 0,
            "y": 202,
            "w": W,
            "h": 35,
            "name_font": 12,
            "sub_font": 8,
            "hero_font": 26,
            "balance_font": 20,
            "latin_font": 10,
            "detail_font": 9,
            "logo_size": 11,
            "border": True,
        },
        {
            "id": "quote",
            "type": "quote",
            "x": 0,
            "y": 242,
            "w": W,
            "h": 58,
            "quote_font": 19,
            "explain_font": 12,
            "border": True,
        },
    ],
}


def load_layout(path: str | Path | None) -> dict[str, Any] | None:
    """Load a designer-exported layout JSON (None if absent/invalid)."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("blocks"), list):
        return data
    return None
