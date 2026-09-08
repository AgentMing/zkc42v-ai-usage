#!/usr/bin/env python3
"""CLI: fetch provider quotas, render 400×300 image + frame.bin for e-paper."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Allow `python3 tools/quotas/cli.py` and `python3 -m tools.quotas`
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

from quotas.credentials import discover_credentials  # noqa: E402
from quotas.fetch import fetch_all_quotas, fetch_market_snapshot  # noqa: E402
from quotas.layout import (  # noqa: E402
    image_has_ink,
    load_layout,
    pick_display_windows,
    render_layout,
    render_quota_image,
)
from quotas.models import SERVICE_NAMES, QuotaRecord  # noqa: E402


SNAPSHOT_FILENAME = "epaper-quotas-last-records.json"
HISTORY_FILENAME = "epaper-quotas-history.json"
MARKET_FILENAME = "epaper-market-last.json"
HISTORY_MAX_SAMPLES = 288  # enough for five-minute refreshes across one day
BEIJING = ZoneInfo("Asia/Shanghai")


def _build_frame(png_path: Path, frame_path: Path) -> None:
    """Reuse tools/make_image.py plane builder."""
    # Import sibling make_image
    tools_dir = Path(__file__).resolve().parents[1]
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    import make_image  # type: ignore

    from PIL import Image

    img = Image.open(png_path).convert("RGB")
    if img.size != (make_image.W, make_image.H):
        img = img.resize((make_image.W, make_image.H))
    black, red = make_image.build_planes(img)
    import struct

    frame = b"ZKEPD1\n" + struct.pack("<HH", make_image.W, make_image.H) + black + red
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(frame)


def _print_records(records, as_json: bool) -> None:
    if as_json:
        print(json.dumps([r.to_dict() for r in records], indent=2, ensure_ascii=False))
        return
    for r in records:
        if r.status == "ok":
            rem = r.remaining_percent
            if rem is None and r.used_percent is not None:
                rem = 100.0 - r.used_percent
            used = r.used_percent
            parts = [f"{r.name:12s}", "ok"]
            if rem is not None:
                parts.append(f"remaining={rem:.1f}%")
            if used is not None:
                parts.append(f"used={used:.1f}%")
            if r.delta_percent is not None:
                sign = "+" if r.delta_percent > 0 else ""
                parts.append(f"delta={sign}{r.delta_percent:.1f}pp")
            if r.reset_at:
                parts.append(f"reset={r.reset_at}")
            if r.detail:
                parts.append(f"({r.detail})")
            print(" ".join(parts))
        else:
            print(f"{r.name:12s} {r.status}: {r.detail}")


def _records_signature(records) -> str:
    """Stable signature of the quota data actually shown on the panel.

    Rounds floats (API responses can drift in low bits) so an unchanged
    snapshot compares equal across refreshes. Timestamps are truncated to the
    minute because the panel only renders "MM-DD HH:MM" resets — sub-minute
    drift (e.g. microseconds from weekly APIs) must not count as a change.
    Ignores only locally-derived delta metadata: any change in balance / reset
    / status means the panel must be re-pushed.
    """

    def norm_ts(v):
        if isinstance(v, str) and v and ("T" in v or ":" in v):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                pass
        return v

    def norm(v):
        if isinstance(v, float):
            return round(v, 3)
        if isinstance(v, str):
            return norm_ts(v)
        if isinstance(v, dict):
            # Differential display metadata is derived locally and must not
            # make an otherwise unchanged panel look like new quota data.
            return {k: norm(x) for k, x in v.items() if k != "delta_percent"}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        return v

    return json.dumps([norm(r.to_dict()) for r in records], sort_keys=True, ensure_ascii=False)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _remaining_from_mapping(value: Any) -> float | None:
    """Read a remaining percentage from a record/window JSON mapping."""
    if not isinstance(value, dict):
        return None
    remaining = _finite_float(value.get("remaining_percent"))
    if remaining is not None:
        return max(0.0, min(100.0, remaining))
    used = _finite_float(value.get("used_percent"))
    if used is not None:
        return max(0.0, min(100.0, 100.0 - used))
    absolute_remaining = _finite_float(value.get("remaining"))
    limit = _finite_float(value.get("limit"))
    if absolute_remaining is not None and limit is not None and limit > 0:
        return max(0.0, min(100.0, absolute_remaining / limit * 100.0))
    return None


def _window_key(value: Any) -> str | None:
    """Use the same canonical 5h/week mapping as the panel when possible."""
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip().lower()
    if kind in ("5h", "week"):
        return kind
    label = str(value.get("label") or "").strip().lower()
    seconds = _finite_float(value.get("window_seconds"))
    if seconds is not None:
        if 3600 <= seconds <= 8 * 3600:
            return "5h"
        if seconds >= 5 * 86400:
            return "week"
    if any(part in label for part in ("rolling", "5h", "5-hour", "five hour", "secondary", "daily", "day", "24h")):
        return "5h"
    if any(part in label for part in ("weekly", "week", "primary")):
        return "week"
    return label or None


def _percent_delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    result = round(current - previous, 1)
    return 0.0 if abs(result) < 0.05 else result


def _load_quota_snapshot(path: Path) -> dict[str, dict[str, Any]]:
    """Load the last successful quota records without failing a refresh."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    rows = data.get("records") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("name"):
            out[str(row["name"])] = row
    return out


def _without_deltas(value: Any) -> Any:
    """Remove locally-derived delta fields before saving a new baseline."""
    if isinstance(value, dict):
        return {
            key: _without_deltas(item)
            for key, item in value.items()
            if key != "delta_percent"
        }
    if isinstance(value, list):
        return [_without_deltas(item) for item in value]
    return value


def _apply_deltas(
    records: list[QuotaRecord],
    previous: dict[str, dict[str, Any]],
) -> None:
    """Attach remaining-percent deltas to current records and their windows."""
    for record in records:
        record.delta_percent = None
        for window in record.windows or []:
            if isinstance(window, dict):
                window.pop("delta_percent", None)
        if record.status != "ok":
            continue
        old = previous.get(record.name)
        if not isinstance(old, dict):
            continue

        record.delta_percent = _percent_delta(
            _remaining_from_mapping(record.to_dict()),
            _remaining_from_mapping(old),
        )

        old_windows: dict[str, dict[str, Any]] = {}
        for old_window in old.get("windows") or []:
            key = _window_key(old_window)
            if key and key not in old_windows and isinstance(old_window, dict):
                old_windows[key] = old_window
        for window in record.windows or []:
            if not isinstance(window, dict):
                continue
            key = _window_key(window)
            old_window = old_windows.get(key) if key else None
            delta = _percent_delta(
                _remaining_from_mapping(window),
                _remaining_from_mapping(old_window),
            )
            if delta is not None:
                window["delta_percent"] = delta


def _save_quota_snapshot(
    path: Path,
    previous: dict[str, dict[str, Any]],
    records: list[QuotaRecord],
) -> None:
    """Save only successful records, retaining the last good value on errors."""
    merged = dict(previous)
    for record in records:
        if record.status == "ok":
            merged[record.name] = _without_deltas(record.to_dict())
    rows = [merged[name] for name in SERVICE_NAMES if name in merged]
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records": rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _beijing_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(BEIJING)
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def _new_quota_history(now: datetime | None = None) -> dict[str, Any]:
    return {"version": 1, "date": _beijing_now(now).date().isoformat(), "samples": []}


def _load_quota_history(path: Path, now: datetime | None = None) -> dict[str, Any]:
    """Load today's usage samples; a new Beijing day starts a new series."""
    empty = _new_quota_history(now)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return empty
    if not isinstance(data, dict) or data.get("date") != empty["date"]:
        return empty
    samples = data.get("samples")
    if not isinstance(samples, list):
        return empty
    empty["samples"] = [sample for sample in samples if isinstance(sample, dict)][-HISTORY_MAX_SAMPLES:]
    return empty


def _total_used_percent(record: QuotaRecord) -> float | None:
    """Return the provider's top-level usage percentage for today's trend."""
    if record.status != "ok":
        return None
    used = _finite_float(record.used_percent)
    if used is None:
        remaining = _finite_float(record.remaining_percent)
        if remaining is not None:
            used = 100.0 - remaining
    if used is None:
        # A few providers only expose window mappings. Prefer the weekly
        # bucket as the closest approximation to a total when available.
        for window in reversed(pick_display_windows(record)):
            if window.get("missing"):
                continue
            remaining = _remaining_from_mapping(window)
            if remaining is not None:
                used = 100.0 - remaining
                break
    if used is None:
        return None
    return round(max(0.0, min(100.0, used)), 1)


def _usage_values_for_record(record: QuotaRecord) -> dict[str, float]:
    """Return today's plotted total usage percentage."""
    total = _total_used_percent(record)
    return {"total": total} if total is not None else {}


def _append_quota_history(
    history: dict[str, Any],
    records: list[QuotaRecord],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one refresh sample and keep only the current day's samples."""
    current = _beijing_now(now)
    if history.get("date") != current.date().isoformat():
        history.clear()
        history.update(_new_quota_history(current))
    services: dict[str, dict[str, float]] = {}
    for record in records:
        values = _usage_values_for_record(record)
        if values:
            services[record.name] = values
    if services:
        sample = {"at": current.isoformat(timespec="seconds"), "services": services}
        samples = history.setdefault("samples", [])
        # Keep every successful refresh, even when two manual refreshes land
        # in the same second; the chart represents refresh samples, not only
        # distinct wall-clock timestamps.
        samples.append(sample)
        history["samples"] = samples[-HISTORY_MAX_SAMPLES:]
    return history


def _history_series(history: dict[str, Any]) -> dict[str, dict[str, list[float]]]:
    """Flatten stored samples to the per-service series consumed by layout."""
    result: dict[str, dict[str, list[float]]] = {}
    for sample in history.get("samples") or []:
        if not isinstance(sample, dict):
            continue
        services = sample.get("services")
        if not isinstance(services, dict):
            continue
        for name, values in services.items():
            if not isinstance(values, dict):
                continue
            value = _finite_float(values.get("total"))
            if value is None:
                # Read histories written by the previous two-line chart. A
                # weekly point is the least surprising fallback for the new
                # total trend until the next refresh writes a real total.
                value = _finite_float(values.get("week"))
            if value is None:
                value = _finite_float(values.get("5h"))
            if value is not None:
                result.setdefault(str(name), {}).setdefault("total", []).append(
                    max(0.0, min(100.0, value))
                )
    return result


def _save_quota_history(path: Path, history: dict[str, Any]) -> None:
    path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _load_market_snapshot(path: Path) -> dict[str, Any]:
    """Load the last market response so a transient API failure is harmless."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": 1, "items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"version": 1, "items": []}
    return {
        "version": 1,
        "updated_at": data.get("updated_at"),
        "items": [item for item in data["items"] if isinstance(item, dict)],
    }


def _save_market_snapshot(path: Path, market: dict[str, Any]) -> None:
    path.write_text(json.dumps(market, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_once(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "epaper-quotas.png"
    frame_path = out_dir / "epaper-quotas-frame.bin"
    last_frame_path = out_dir / "epaper-quotas-last-frame.bin"

    creds = discover_credentials()
    if args.verbose:
        for name in SERVICE_NAMES:
            print(f"# cred {name}: {creds[name].redacted()}", file=sys.stderr)

    records = fetch_all_quotas(creds)
    snapshot_path = out_dir / SNAPSHOT_FILENAME
    history_path = out_dir / HISTORY_FILENAME
    market_path = out_dir / MARKET_FILENAME
    previous = _load_quota_snapshot(snapshot_path)
    history = _load_quota_history(history_path)
    market = _load_market_snapshot(market_path)
    try:
        live_market = fetch_market_snapshot()
        if live_market.get("items"):
            market = live_market
    except Exception as exc:
        print(f"could not fetch market snapshot: {exc}", file=sys.stderr)
    history_has_baseline = bool(history.get("samples"))
    sig = _records_signature(records)
    sig += "\nmarket=" + json.dumps(market.get("items") or [], sort_keys=True, ensure_ascii=False)
    _apply_deltas(records, previous)
    _append_quota_history(history, records)
    history_series = _history_series(history)
    _print_records(records, as_json=args.json)

    # Snapshot-level differential refresh: an unchanged snapshot should not
    # flash the panel for nothing.  --force overrides (e.g. to refresh the
    # header clock).  The layout file is folded into the signature so that
    # swapping data/layout.json always forces a re-push even if quota numbers
    # did not move.  --partial adds a separate frame/rectangle-level transport
    # path below; the existing full send remains the fallback.
    layout = load_layout(args.layout)
    if args.layout and layout is None:
        print(f"layout file not found or invalid: {args.layout}", file=sys.stderr)
        return 2
    if args.send and not args.json and not args.force:
        last_file = out_dir / "epaper-quotas-last.json"
        if layout is not None:
            sig += "\n" + json.dumps(layout, sort_keys=True, ensure_ascii=False)
        if last_file.is_file():
            try:
                prev = last_file.read_text()
            except OSError:
                prev = None
            # An older installation may already have the transport signature
            # but no quota-history file. Render once in that case so the new
            # delta display gets its first baseline.
            if prev == sig and previous and history_has_baseline:
                try:
                    _save_quota_history(history_path, history)
                except (OSError, TypeError, ValueError) as exc:
                    print(f"could not save quota history: {exc}", file=sys.stderr)
                print(
                    "quota data unchanged; skipped render + BLE push "
                    "(--force to update anyway)",
                    file=sys.stderr,
                )
                return 0
        last_file.write_text(sig)

    if layout is not None:
        img = render_layout(records, layout, history=history_series, market=market)
    else:
        img = render_quota_image(records, history=history_series, market=market)
    img.save(png_path)
    print(f"wrote {png_path} {img.size[0]}x{img.size[1]} ink={image_has_ink(img)}")
    _build_frame(png_path, frame_path)
    print(f"wrote {frame_path} bytes={frame_path.stat().st_size}")
    try:
        _save_quota_snapshot(snapshot_path, previous, records)
        print(f"wrote {snapshot_path}")
    except (OSError, TypeError, ValueError) as exc:
        print(f"could not save quota delta snapshot: {exc}", file=sys.stderr)
    try:
        _save_quota_history(history_path, history)
        print(f"wrote {history_path}")
    except (OSError, TypeError, ValueError) as exc:
        print(f"could not save quota history: {exc}", file=sys.stderr)
    try:
        if market.get("items"):
            _save_market_snapshot(market_path, market)
            print(f"wrote {market_path}")
    except (OSError, TypeError, ValueError) as exc:
        print(f"could not save market snapshot: {exc}", file=sys.stderr)

    if args.send:
        uuid = os.environ.get("EPAPER_UUID", "").strip()
        if not uuid:
            print("EPAPER_UUID not set; skip BLE send", file=sys.stderr)
            return 2
        ble = Path(args.bleprobe)
        if not ble.is_file():
            print(f"bleprobe not found: {ble}", file=sys.stderr)
            return 2
        import subprocess

        if args.partial:
            cmd = [str(ble), "partial", uuid, str(last_frame_path), str(frame_path)]
            if args.partial_red:
                cmd.append("--allow-red")
        else:
            cmd = [str(ble), "send", uuid, str(frame_path)]
        if ble.suffix.lower() == ".py":
            cmd = [sys.executable, *cmd]
        print("running:", " ".join(cmd), file=sys.stderr)
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"bleprobe {'partial ' if args.partial else ''}send failed rc={rc}", file=sys.stderr)
            return rc
        try:
            shutil.copyfile(frame_path, last_frame_path)
        except OSError as exc:
            print(f"could not save displayed frame state: {exc}", file=sys.stderr)

    # Exit 0 even with partial provider failures (isolated errors)
    return 0


def _in_active_window(start: str | None, end: str | None) -> bool:
    """True if now is inside the [start, end] active window (HH:MM, 24h).

    No window → always active. A start>end window wraps midnight (e.g.
    22:00–06:00 is active overnight).
    """
    if not start and not end:
        return True
    now = time.strftime("%H:%M")
    if start and end:
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
    if start:
        return now >= start
    return now <= end


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch AI quotas and render for ZKC42V e-paper")
    ap.add_argument(
        "--out-dir",
        default=os.environ.get("EPAPER_QUOTA_OUT", "/tmp"),
        help="Directory for PNG + frame.bin (default: /tmp or EPAPER_QUOTA_OUT)",
    )
    ap.add_argument("--json", action="store_true", help="Print machine-readable JSON records")
    ap.add_argument("--send", action="store_true", help="Push frame via bleprobe send")
    ap.add_argument(
        "--partial",
        action="store_true",
        help="Use experimental SSD1619 windowed refresh; falls back to full refresh",
    )
    ap.add_argument(
        "--partial-red",
        action="store_true",
        help="Allow experimental red-plane partial refresh (otherwise red changes use full refresh)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Push even if quota data is unchanged (delta refresh skips redundant pushes)",
    )
    ap.add_argument(
        "--bleprobe",
        default=str(_ROOT / ("bleprobe.py" if os.name == "nt" else Path("build") / "bleprobe")),
        help="Path to bleprobe binary",
    )
    ap.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Repeat every SECONDS (0 = once). Default interval for schedule: 900",
    )
    ap.add_argument(
        "--layout",
        default=None,
        metavar="PATH",
        help="Designer-exported layout JSON to render instead of the built-in layout",
    )
    ap.add_argument(
        "--start",
        default=None,
        metavar="HH:MM",
        help="Active window start (e.g. 09:00). Outside the window refreshes are skipped.",
    )
    ap.add_argument(
        "--end",
        default=None,
        metavar="HH:MM",
        help="Active window end (e.g. 18:00). start>end wraps midnight.",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    if args.loop and args.loop > 0:
        while True:
            if _in_active_window(args.start, args.end):
                try:
                    run_once(args)
                except Exception as e:
                    print(f"loop iteration error: {e}", file=sys.stderr)
            else:
                print(
                    f"[{time.strftime('%H:%M')}] outside active window "
                    f"({args.start or '00:00'}–{args.end or '24:00'}); skipping",
                    file=sys.stderr,
                )
            time.sleep(args.loop)
    if _in_active_window(args.start, args.end):
        return run_once(args)
    print(
        f"outside active window ({args.start or '00:00'}–{args.end or '24:00'}); nothing to do",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
