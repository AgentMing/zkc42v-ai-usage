"""Aggregate local coding-agent activity from WSL logs (never uploads content)."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc


def _date(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _empty() -> dict[str, Any]:
    return {"sessions": set(), "turns": set(), "tools": 0, "skills": 0,
            "tokens": 0, "messages": [], "outputs": [], "compacts": 0,
            "failures": 0, "tool_total": 0, "failure_tool_total": 0}


def _text_size(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_size(x) for x in value)
    if isinstance(value, dict):
        if value.get("type") in ("text", "input_text", "output_text"):
            return _text_size(value.get("text") or value.get("content"))
        return sum(_text_size(v) for v in value.values())
    return 0


def _has_skill_reference(value: Any) -> bool:
    if isinstance(value, str):
        text = value.lower()
        return "skill.md" in text or "skill://" in text or "/skills/" in text or "\\skills\\" in text
    if isinstance(value, list):
        return any(_has_skill_reference(item) for item in value)
    if isinstance(value, dict):
        return any(_has_skill_reference(item) for item in value.values())
    return False


def _in_window(ts: dt.datetime | None, lo: dt.datetime, hi: dt.datetime) -> bool:
    return ts is not None and lo <= ts < hi


def _collect(root: Path, now: dt.datetime) -> dict[str, Any]:
    windows = {"current": _empty(), "previous": _empty()}
    ranges = {"current": (now - dt.timedelta(hours=24), now),
              "previous": (now - dt.timedelta(hours=48), now - dt.timedelta(hours=24))}

    def bucket(ts: dt.datetime | None) -> str | None:
        for name, (lo, hi) in ranges.items():
            if _in_window(ts, lo, hi):
                return name
        return None

    # Codex JSONL: timestamped event and response records; count tool output
    # sizes only, never persist or print prompt/tool contents.
    codex = root / ".codex" / "sessions"
    if codex.is_dir():
        for path in codex.glob("**/*.jsonl"):
            try:
                if path.stat().st_mtime < (now - dt.timedelta(hours=50)).timestamp():
                    continue
                with path.open(encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                            ts = _date(row.get("timestamp")); b = bucket(ts)
                            if b is None: continue
                            p = row.get("payload") or {}; s = windows[b]
                            typ = row.get("type"); pt = p.get("type") if isinstance(p, dict) else ""
                            sid = p.get("session_id") if typ == "session_meta" else None
                            if sid: s["sessions"].add(str(sid))
                            if typ == "event_msg" and pt == "task_started":
                                s["turns"].add(str(p.get("turn_id") or f"codex:{ts.isoformat()}"))
                            if typ == "response_item" and pt in ("custom_tool_call", "function_call"):
                                s["tools"] += 1; s["tool_total"] += 1
                                s["failure_tool_total"] += 1
                                if "skill" in str(p.get("name", "")).lower() or _has_skill_reference(p.get("input")):
                                    s["skills"] += 1
                            if typ == "response_item" and pt == "custom_tool_call_output":
                                n = _text_size(p.get("output"));
                                if n: s["outputs"].append(n / 4)
                                if str(p.get("status", "")).lower() in ("failed", "error"):
                                    s["failures"] += 1
                            if typ == "response_item" and pt == "message" and p.get("role") == "user":
                                n = _text_size(p.get("content"));
                                if n: s["messages"].append(n / 4)
                            if typ == "token_usage_record":
                                usage = p.get("turn_token_usage") or p.get("usage") or {}
                                s["tokens"] += int(usage.get("total_tokens") or 0)
                            if "compact" in str(pt).lower() or "compaction" in str(pt).lower():
                                s["compacts"] += 1
                        except (ValueError, TypeError, AttributeError):
                            continue
            except OSError:
                continue

    # Grok's ACP updates carry exact Unix timestamps and explicit turn/tool
    # lifecycle events. Count tool_call once (tool_call_update is its status
    # stream and would double count it).
    grok = root / ".grok" / "sessions"
    if grok.is_dir():
        for updates in grok.glob("**/updates.jsonl"):
            try:
                sid = updates.parent.name
                for line in updates.open(encoding="utf-8", errors="ignore"):
                    try:
                        row = json.loads(line)
                    except ValueError: continue
                    raw_ts = row.get("timestamp")
                    ts = dt.datetime.fromtimestamp(float(raw_ts), UTC) if isinstance(raw_ts, (int, float)) else _date(raw_ts)
                    b = bucket(ts)
                    if b is None: continue
                    s = windows[b]; s["sessions"].add("grok:" + sid)
                    params = row.get("params") or {}; u = params.get("update") or {}
                    event = str(u.get("sessionUpdate") or "")
                    if event == "turn_completed":
                        s["turns"].add("grok:" + sid + ":" + str(u.get("prompt_id") or ts.timestamp()))
                        usage = u.get("usage") or {}
                        if isinstance(usage, dict):
                            total = usage.get("total_tokens")
                            if isinstance(total, (int, float)):
                                s["tokens"] += int(total)
                            else:
                                s["tokens"] += sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens") if isinstance(usage.get(k), (int, float)))
                    if event == "tool_call":
                        s["tools"] += 1; s["tool_total"] += 1
                        title = str(u.get("title") or "").lower()
                        if "skill" in title or _has_skill_reference(u.get("rawInput")): s["skills"] += 1
                    if "compact" in event or "compaction" in event: s["compacts"] += 1
            except OSError: continue

    # Devin CLI log files include UTC timestamps. Parse event labels and sizes
    # without retaining any log text. No prompt bodies are copied to history.
    devin = root / ".local" / "share" / "devin" / "cli" / "logs"
    if devin.is_dir():
        for path in devin.glob("devin_*.log*"):
            try:
                if path.stat().st_mtime < (now - dt.timedelta(hours=50)).timestamp(): continue
                opener = __import__("gzip").open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)", line)
                        ts = _date(m.group(1)) if m else None; b = bucket(ts)
                        if b is None: continue
                        s = windows[b]; low = line.lower()
                        if "cli_startup" in low or "acp startup" in low: s["sessions"].add("devin:" + path.name)
                        if any(x in low for x in ("tool_call", "tool call", "tool invocation")):
                            s["tools"] += 1; s["tool_total"] += 1
                            if "skill" in low or "/skills/" in low: s["skills"] += 1
                        if "compact" in low or "compaction" in low: s["compacts"] += 1
            except (OSError, EOFError): continue

    def p95(values: list[float]) -> int | None:
        if not values: return None
        values.sort(); return int(round(values[min(len(values)-1, math.ceil(.95*len(values))-1)]))

    result: dict[str, Any] = {}
    for key in ("sessions", "turns", "tools", "skills", "tokens", "tools_per_turn", "msg_p95", "tool_out_p95", "compacts", "tool_fail_rate"):
        vals = []
        for name in ("current", "previous"):
            s = windows[name]
            if key == "sessions": v = len(s["sessions"])
            elif key == "turns": v = len(s["turns"])
            elif key == "tools": v = s["tools"]
            elif key == "skills": v = s["skills"]
            elif key == "tokens": v = s["tokens"]
            elif key == "tools_per_turn": v = round(s["tools"] / len(s["turns"]), 1) if s["turns"] else "N/A"
            elif key == "msg_p95": v = p95(s["messages"])
            elif key == "tool_out_p95": v = p95(s["outputs"])
            elif key == "compacts": v = s["compacts"]
            else: v = round(100 * s["failures"] / s["failure_tool_total"], 1) if s["failure_tool_total"] else None
            vals.append(v)
        cur, prev = vals
        delta = round(cur - prev, 1) if isinstance(cur, (int, float)) and isinstance(prev, (int, float)) else None
        if key == "tokens":
            result[key] = {"value": f"{cur/1_000_000:.1f}M" if isinstance(cur, (int,float)) else cur, "delta": f"{delta/1_000_000:+.1f}M" if delta is not None else None}
        elif key in ("msg_p95", "tool_out_p95"):
            value = f"{cur/1000:.1f}K" if isinstance(cur, (int, float)) and cur >= 1000 else (str(cur) if cur is not None else "N/A")
            result[key] = {"value": value, "delta": delta}
        elif key == "tool_fail_rate":
            result[key] = {"value": f"{cur:.1f}%" if isinstance(cur, (int,float)) else "N/A", "delta": f"{delta:+.1f}pp" if delta is not None else None}
        else:
            result[key] = {"value": cur, "delta": delta}
    result["_available"] = any((root / part).is_dir() for part in (
        ".codex/sessions", ".grok/sessions", ".local/share/devin/cli/logs"
    ))
    return result


def collect_agent_stats(now: dt.datetime | None = None) -> dict[str, Any]:
    """Run the same collector in WSL on Windows; return only aggregate values."""
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    if os.name == "nt":
        script = str(Path(__file__).resolve())
        try:
            linux_path = subprocess.run(["wsl", "-e", "wslpath", "-a", script], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            out = subprocess.run(["wsl", "-e", "python3", linux_path, "--worker", now.isoformat()], capture_output=True, text=True, timeout=90, check=True)
            return json.loads(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}
    if not (os.environ.get("WSL_DISTRO_NAME") or Path("/proc/sys/kernel/osrelease").exists() and "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(errors="ignore").lower()):
        return {}
    return _collect(Path.home(), now)


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--worker":
    print(json.dumps(_collect(Path.home(), _date(sys.argv[2]) or dt.datetime.now(UTC))))
