"""Frame-diff helpers for SSD16xx windowed e-paper updates.

The project frame format stores each 1bpp plane row-major, MSB first.  The
SSD1619 X address is byte based, so dirty rectangles are always aligned to
8-pixel boundaries on the X axis.  This module deliberately has no BLE or
Pillow dependency; it is safe to unit-test without a tag connected.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Rect:
    """A byte-aligned pixel rectangle."""

    x: int
    y: int
    w: int
    h: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0 or self.w <= 0 or self.h <= 0:
            raise ValueError(f"invalid rectangle: {self}")
        if self.x % 8 or self.w % 8:
            raise ValueError(f"rectangle x/w must be 8-pixel aligned: {self}")

    @property
    def x_byte(self) -> int:
        return self.x // 8

    @property
    def byte_width(self) -> int:
        return self.w // 8

    @property
    def area(self) -> int:
        return self.w * self.h


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    y: int
    h: int


def _validate_planes(width: int, height: int, *planes: bytes) -> int:
    if width <= 0 or height <= 0 or width % 8:
        raise ValueError(f"width must be positive and divisible by 8: {width}")
    expected = (width * height) // 8
    if any(len(p) != expected for p in planes):
        raise ValueError(f"plane size mismatch: expected {expected} bytes")
    return width // 8


def _row_runs(
    old_black: bytes,
    old_red: bytes,
    new_black: bytes,
    new_red: bytes,
    row: int,
    row_bytes: int,
) -> list[tuple[int, int]]:
    """Return dirty byte spans for one row as [start, end) pairs."""
    offset = row * row_bytes
    runs: list[tuple[int, int]] = []
    i = 0
    while i < row_bytes:
        dirty = (
            old_black[offset + i] != new_black[offset + i]
            or old_red[offset + i] != new_red[offset + i]
        )
        if not dirty:
            i += 1
            continue
        start = i
        i += 1
        while i < row_bytes:
            if (
                old_black[offset + i] == new_black[offset + i]
                and old_red[offset + i] == new_red[offset + i]
            ):
                break
            i += 1
        runs.append((start, i))
    return runs


def diff_rects(
    width: int,
    height: int,
    old_black: bytes,
    old_red: bytes,
    new_black: bytes,
    new_red: bytes,
    *,
    merge_gap_bytes: int = 0,
) -> list[Rect]:
    """Calculate byte-aligned dirty rectangles for two BWR frames.

    Dirty spans on adjacent rows are vertically merged when they overlap (or
    are within ``merge_gap_bytes``).  The returned rectangles never contain a
    pixel outside the enclosing dirty span of their rows, although unchanged
    bytes inside a rectangle are intentionally retained for simple windowed
    writes.
    """
    if merge_gap_bytes < 0:
        raise ValueError("merge_gap_bytes must be non-negative")
    row_bytes = _validate_planes(
        width, height, old_black, old_red, new_black, new_red
    )

    active: list[_Span] = []
    finished: list[_Span] = []
    for y in range(height):
        runs = _row_runs(old_black, old_red, new_black, new_red, y, row_bytes)
        next_active: list[_Span] = []
        used: set[int] = set()

        for start, end in runs:
            matches = [
                i
                for i, previous in enumerate(active)
                if i not in used
                and start <= previous.end + merge_gap_bytes
                and previous.start <= end + merge_gap_bytes
            ]
            if not matches:
                next_active.append(_Span(start, end, y, 1))
                continue

            first = active[matches[0]]
            start2 = min(start, first.start)
            end2 = max(end, first.end)
            y2 = first.y
            for i in matches:
                used.add(i)
                previous = active[i]
                start2 = min(start2, previous.start)
                end2 = max(end2, previous.end)
                y2 = min(y2, previous.y)
            next_active.append(_Span(start2, end2, y2, y - y2 + 1))

        finished.extend(previous for i, previous in enumerate(active) if i not in used)
        active = next_active

    finished.extend(active)
    return [
        Rect(span.start * 8, span.y, (span.end - span.start) * 8, span.h)
        for span in sorted(finished, key=lambda s: (s.y, s.start))
    ]


def extract_plane_rect(plane: bytes, width: int, height: int, rect: Rect) -> bytes:
    """Extract row-major bytes for ``rect`` from one packed 1bpp plane."""
    row_bytes = _validate_planes(width, height, plane)
    if rect.x + rect.w > width or rect.y + rect.h > height:
        raise ValueError(f"rectangle outside frame: {rect} for {width}x{height}")
    out = bytearray()
    for row in range(rect.y, rect.y + rect.h):
        start = row * row_bytes + rect.x_byte
        out.extend(plane[start : start + rect.byte_width])
    return bytes(out)


def rects_area(rects: list[Rect]) -> int:
    """Return total pixel area covered by dirty rectangles."""
    return sum(rect.area for rect in rects)
