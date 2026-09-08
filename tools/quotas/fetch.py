"""Per-provider quota fetch + normalize into QuotaRecord."""

from __future__ import annotations

import json
import math
import os
import re
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .credentials import Credential, discover_credentials
from .models import SERVICE_NAMES, QuotaRecord, error, unavailable

USER_AGENT = "epaper-quota/1.0 (+zkc42v)"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
# Public Codex CLI OAuth client id (refresh works against stored refresh_token).
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_TOKEN_URL = "https://auth.x.ai/oauth2/token"
KIMI_DEFAULT_BASE = "https://api.kimi.com/coding/v1"
# Public kimi-code CLI OAuth client id (device-code login).
KIMI_CODE_OAUTH_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CODE_OAUTH_HOST = "https://auth.kimi.com"
OPENCODE_GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
OPENCODE_GO_DASHBOARD = "https://opencode.ai/workspace/{wid}/go"
OLLAMA_USAGE_URL = "https://ollama.com/api/usage"
DEVIN_USAGE_URL = (
    "https://server.codeium.com/"
    "exa.seat_management_pb.SeatManagementService/GetUserStatus"
)
WINDSURF_USAGE_URL = (
    "https://server.self-serve.windsurf.com/"
    "exa.seat_management_pb.SeatManagementService/GetUserStatus"
)
DEVIN_WEB_QUOTA_BASE_URL = "https://app.devin.ai/api"
WINDSURF_CLIENT_VERSION = "1.108.2"
MARKET_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&"
    "secids=1.000001,0.399001,100.SPX,100.NDX&fields=f2,f3,f12,f14"
)
MARKET_SYMBOLS = (
    ("sh", "沪", "000001"),
    ("sz", "深", "399001"),
    ("spx", "标普", "SPX"),
    ("ndx", "纳", "NDX"),
)

HttpFn = Callable[..., tuple[int, dict[str, str], bytes]]
RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _default_http(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: float = 25.0,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, body


def _http_with_retry(
    http: HttpFn,
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    **kwargs: Any,
) -> tuple[int, dict[str, str], bytes]:
    """Retry transient connection failures and temporary HTTP responses.

    Authentication failures are deliberately not retried here; the provider
    fetchers retain their existing token-refresh flow for HTTP 401.
    """
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            result = http(url, **kwargs)
            if result[0] not in RETRYABLE_HTTP_STATUS or attempt == attempts - 1:
                return result
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
        time.sleep(base_delay * (2**attempt))
    assert last_error is not None
    raise last_error


def normalize_market_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize Eastmoney's compact China/US index response."""
    raw_data = payload.get("data") if isinstance(payload, dict) else None
    rows = raw_data.get("diff") if isinstance(raw_data, dict) else None
    if not isinstance(rows, list):
        return {"version": 1, "items": []}
    by_code = {
        str(row.get("f12")): row
        for row in rows
        if isinstance(row, dict) and row.get("f12") is not None
    }
    items: list[dict[str, Any]] = []
    for key, label, code in MARKET_SYMBOLS:
        row = by_code.get(code)
        if row is None:
            continue
        try:
            change = float(row.get("f3"))
        except (TypeError, ValueError):
            change = None
        try:
            price = float(row.get("f2"))
        except (TypeError, ValueError):
            price = None
        if change is None and price is None:
            continue
        item: dict[str, Any] = {"key": key, "label": label}
        if change is not None:
            item["change_percent"] = round(change, 2)
        if price is not None:
            item["price"] = round(price, 2)
        items.append(item)
    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": items,
    }


def fetch_market_snapshot(http: HttpFn = _default_http) -> dict[str, Any]:
    """Fetch compact China/US index changes for the e-paper header."""
    try:
        status, _, raw = _http_with_retry(
            http,
            MARKET_URL,
            attempts=2,
            base_delay=0.2,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": USER_AGENT,
            },
            timeout=4.0,
        )
    except Exception:
        return {"version": 1, "items": []}
    if status != 200:
        return {"version": 1, "items": []}
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, ValueError, TypeError):
        return {"version": 1, "items": []}
    return normalize_market_snapshot(payload) if isinstance(payload, dict) else {"version": 1, "items": []}


def _iso_from_unix(ts: Any) -> Optional[str]:
    try:
        n = float(ts)
        if n > 1e12:  # ms
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _iso_from_any(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _iso_from_unix(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return _iso_from_unix(int(s))
        try:
            # normalize Z
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            return s
    return None


def _fmt_reset_short(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%m-%d %H:%M")
    except Exception:
        return iso[:16]


def normalize_codex_usage(payload: dict[str, Any], name: str = "codex") -> QuotaRecord:
    """Normalize ChatGPT `GET /backend-api/codex/usage` JSON."""
    rl = payload.get("rate_limit") or {}
    primary = rl.get("primary_window") or {}
    secondary = rl.get("secondary_window") or {}
    used = primary.get("used_percent")
    if used is None and secondary:
        used = secondary.get("used_percent")
    if used is None:
        return error(name, "usage payload missing used_percent")
    try:
        used_f = float(used)
    except (TypeError, ValueError):
        return error(name, f"invalid used_percent: {used!r}")
    rem = max(0.0, 100.0 - used_f)
    reset_at = _iso_from_any(primary.get("reset_at") or secondary.get("reset_at"))
    if not reset_at and primary.get("reset_after_seconds") is not None:
        try:
            reset_at = (
                datetime.now(timezone.utc)
                + timedelta(seconds=float(primary["reset_after_seconds"]))
            ).isoformat()
        except Exception:
            pass
    def _codex_window_label(win: dict[str, Any], fallback: str) -> str:
        secs = win.get("limit_window_seconds")
        try:
            s = float(secs)
            if 3600 <= s <= 8 * 3600:
                return "5h"
            if s >= 5 * 86400:
                return "week"
        except (TypeError, ValueError):
            pass
        return fallback

    def _codex_window_reset(win: dict[str, Any]) -> Optional[str]:
        at = _iso_from_any(win.get("reset_at"))
        if at:
            return at
        if win.get("reset_after_seconds") is not None:
            try:
                return (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=float(win["reset_after_seconds"]))
                ).isoformat()
            except Exception:
                return None
        return None

    windows: list[dict[str, Any]] = []
    if primary:
        p_used = primary.get("used_percent")
        windows.append(
            {
                "label": _codex_window_label(primary, "primary"),
                "used_percent": p_used,
                "remaining_percent": (
                    max(0.0, 100.0 - float(p_used)) if p_used is not None else None
                ),
                "reset_at": _codex_window_reset(primary),
                "window_seconds": primary.get("limit_window_seconds"),
            }
        )
    if secondary:
        s_used = secondary.get("used_percent")
        windows.append(
            {
                "label": _codex_window_label(secondary, "secondary"),
                "used_percent": s_used,
                "remaining_percent": (
                    max(0.0, 100.0 - float(s_used)) if s_used is not None else None
                ),
                "reset_at": _codex_window_reset(secondary),
                "window_seconds": secondary.get("limit_window_seconds"),
            }
        )
    plan = payload.get("plan_type") or ""
    detail = f"plan={plan}" if plan else ""
    if rl.get("limit_reached"):
        detail = (detail + " limit_reached").strip()
    return QuotaRecord(
        name=name,
        status="ok",
        used_percent=used_f,
        remaining_percent=rem,
        reset_at=reset_at,
        detail=detail,
        windows=windows,
    )


def _fetch_codex_app_server(timeout: float = 25.0) -> Optional[QuotaRecord]:
    """Read limits through the installed Codex app-server JSON-RPC API.

    This is the first-party integration surface used by Codex clients.  It is
    preferable to calling a private chatgpt.com URL because the CLI owns OAuth
    refresh, account selection and backend compatibility.
    """
    if os.name == "nt":
        # Use native Codex on Windows. The standalone installer provides
        # codex.exe, while older npm installs provided a codex.cmd shim.
        # Resolve both forms so scheduled tasks keep working after upgrades.
        candidates: list[Path] = []
        configured = os.environ.get("CODEX_CLI_PATH", "").strip().strip('"')
        if configured:
            candidates.append(Path(configured))
        for name in ("codex.exe", "codex"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        candidates.append(
            Path.home() / "AppData" / "Local" / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
        )
        codex = next((path for path in candidates if path.is_file()), None)
        if codex is None:
            return None
        # WSL may have no route to chatgpt.com even while the Windows host is
        # online, causing every RPC to time out.
        cmd = [str(codex), "app-server", "--listen", "stdio://"]
    else:
        cmd = ["codex", "app-server", "--listen", "stdio://"]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None
    assert proc.stdin is not None and proc.stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read_stdout() -> None:
        for line in proc.stdout:
            lines.put(line)

    threading.Thread(target=read_stdout, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(response_id: int) -> Optional[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(lines.get(timeout=max(0.1, deadline - time.monotonic())))
            except (queue.Empty, json.JSONDecodeError):
                continue
            if message.get("id") == response_id:
                return message
        return None

    try:
        send({
            "method": "initialize",
            "id": 1,
            "params": {"clientInfo": {"name": "epaper_quota", "title": "E-paper Quota", "version": "1.0.0"}},
        })
        if not receive(1):
            return None
        send({"method": "initialized"})
        send({"method": "account/rateLimits/read", "id": 7})
        response = receive(7)
        if not response or response.get("error"):
            return None
        limits = ((response.get("result") or {}).get("rateLimits") or {})
        raw_windows = [limits.get("primary"), limits.get("secondary")]
        windows: list[dict[str, Any]] = []
        for raw in raw_windows:
            if not isinstance(raw, dict) or raw.get("usedPercent") is None:
                continue
            mins = raw.get("windowDurationMins")
            try:
                seconds = int(float(mins) * 60) if mins is not None else None
            except (TypeError, ValueError):
                seconds = None
            windows.append({
                "label": "week" if seconds and seconds >= 5 * 86400 else "5h",
                "used_percent": float(raw["usedPercent"]),
                "remaining_percent": max(0.0, 100.0 - float(raw["usedPercent"])),
                "reset_at": _iso_from_unix(raw.get("resetsAt")),
                "window_seconds": seconds,
            })
        if not windows:
            return None
        primary = windows[0]
        return QuotaRecord(
            name="codex",
            status="ok",
            used_percent=primary["used_percent"],
            remaining_percent=primary["remaining_percent"],
            reset_at=primary["reset_at"],
            detail=f"official app-server · {limits.get('planType') or ''}".rstrip(),
            windows=windows,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _native_curl_get(url: str, headers: dict[str, str], timeout: float = 25.0) -> tuple[int, dict[str, str], bytes]:
    """GET through Windows curl, avoiding urllib/OpenSSL proxy TLS failures."""
    if os.name != "nt":
        raise OSError("native curl fallback is only available on Windows")
    config = [f'url = "{url}"', "silent", "show-error", "location", f"max-time = {int(timeout)}"]
    for key, value in headers.items():
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        config.append(f'header = "{key}: {safe}"')
    proc = subprocess.run(
        ["curl.exe", "--write-out", "\\n%{http_code}", "--config", "-"],
        input="\n".join(config) + "\n",
        text=True,
        capture_output=True,
        timeout=timeout + 5,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or "native curl failed").strip())
    body, _, status_text = proc.stdout.rpartition("\n")
    return int(status_text), {}, body.encode()


def normalize_grok_billing(payload: dict[str, Any], name: str = "grok") -> QuotaRecord:
    """Normalize Grok `GET /v1/billing?format=credits` JSON."""
    cfg = payload.get("config") or {}
    period = cfg.get("currentPeriod") or {}
    has_usage = "creditUsagePercent" in cfg
    used_raw = cfg.get("creditUsagePercent")
    if has_usage:
        try:
            used_f = float(used_raw)
        except (TypeError, ValueError):
            return error(name, f"invalid creditUsagePercent: {used_raw!r}")
    else:
        # protobuf JSON omits zero fields — treat as 0% used when period present
        if not period and "monthlyLimit" not in cfg and "used" not in cfg:
            return error(name, "billing payload missing usage fields")
        used_f = 0.0
        # fallback plain billing shape: used/monthlyLimit absolute credits
        if "used" in cfg and "monthlyLimit" in cfg:
            try:
                u = float((cfg["used"] or {}).get("val", cfg["used"]))
                lim = float((cfg["monthlyLimit"] or {}).get("val", cfg["monthlyLimit"]))
                if lim > 0:
                    used_f = (u / lim) * 100.0
                    rem_abs = max(0.0, lim - u)
                    return QuotaRecord(
                        name=name,
                        status="ok",
                        used_percent=used_f,
                        remaining_percent=max(0.0, 100.0 - used_f),
                        used=u,
                        remaining=rem_abs,
                        limit=lim,
                        reset_at=_iso_from_any(cfg.get("billingPeriodEnd") or period.get("end")),
                        detail="monthly credits",
                    )
            except Exception:
                pass
    rem = max(0.0, 100.0 - used_f)
    reset_at = _iso_from_any(period.get("end") or cfg.get("billingPeriodEnd"))
    kind = str(period.get("type") or "")
    detail = "weekly" if "WEEK" in kind.upper() else ("monthly" if "MONTH" in kind.upper() else "period")
    products = cfg.get("productUsage") or []
    windows = []
    for p in products:
        if isinstance(p, dict):
            windows.append(
                {
                    "label": p.get("product"),
                    "used_percent": p.get("usagePercent"),
                }
            )
    return QuotaRecord(
        name=name,
        status="ok",
        used_percent=used_f,
        remaining_percent=rem,
        reset_at=reset_at,
        detail=detail,
        windows=windows,
    )


def normalize_kimi_usages(payload: dict[str, Any], name: str = "kimi") -> QuotaRecord:
    """Normalize Kimi Coding Plan `/usages` (or `/usage`) payload."""
    rows: list[dict[str, Any]] = []

    def row_from(item: dict[str, Any], default_label: str) -> Optional[dict[str, Any]]:
        limit = item.get("limit") if item.get("limit") is not None else item.get("limit_amount")
        used = item.get("used") if item.get("used") is not None else item.get("used_amount")
        remaining = item.get("remaining")
        try:
            limit_f = float(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit_f = None
        try:
            used_f = float(used) if used is not None else None
        except (TypeError, ValueError):
            used_f = None
        try:
            rem_f = float(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            rem_f = None
        if used_f is None and rem_f is not None and limit_f is not None:
            used_f = limit_f - rem_f
        if used_f is None and limit_f is None and rem_f is None:
            # percent-only
            up = item.get("used_percent") or item.get("usagePercent")
            if up is None:
                return None
            try:
                up_f = float(up)
            except (TypeError, ValueError):
                return None
            return {
                "label": default_label,
                "used_percent": up_f,
                "remaining_percent": max(0.0, 100.0 - up_f),
                "reset_at": _iso_from_any(
                    item.get("resetTime") or item.get("reset_at") or item.get("reset_time")
                ),
            }
        used_pct = None
        rem_pct = None
        if limit_f and limit_f > 0 and used_f is not None:
            used_pct = (used_f / limit_f) * 100.0
            rem_pct = max(0.0, 100.0 - used_pct)
        elif rem_f is not None and limit_f and limit_f > 0:
            rem_pct = (rem_f / limit_f) * 100.0
            used_pct = max(0.0, 100.0 - rem_pct)
        return {
            "label": default_label,
            "used": used_f,
            "limit": limit_f,
            "remaining": rem_f if rem_f is not None else (
                (limit_f - used_f) if limit_f is not None and used_f is not None else None
            ),
            "used_percent": used_pct,
            "remaining_percent": rem_pct,
            "reset_at": _iso_from_any(
                item.get("resetTime") or item.get("reset_at") or item.get("reset_time")
            ),
        }

    def _kimi_window_seconds(window: Any) -> Optional[float]:
        if not isinstance(window, dict):
            return None
        dur = window.get("duration")
        unit = str(window.get("timeUnit") or window.get("time_unit") or "").upper()
        try:
            d = float(dur)
        except (TypeError, ValueError):
            return None
        if "SECOND" in unit:
            return d
        if "MINUTE" in unit:
            return d * 60.0
        if "HOUR" in unit:
            return d * 3600.0
        if "DAY" in unit:
            return d * 86400.0
        return d

    def _kimi_label_from_window(window: Any, fallback: str) -> str:
        secs = _kimi_window_seconds(window)
        if secs is not None:
            if 3600 <= secs <= 8 * 3600:
                return "5h"
            if secs >= 5 * 86400:
                return "week"
        return fallback

    data_list = payload.get("data")
    if isinstance(data_list, list):
        for item in data_list:
            if isinstance(item, dict):
                model = str(item.get("model_name") or "").lower()
                if model in ("all", "weekly", "week"):
                    label = "week"
                elif model in ("5h", "rolling", "five"):
                    label = "5h"
                else:
                    label = model or "limit"
                r = row_from(item, label)
                if r:
                    if label == "5h":
                        r["window_seconds"] = r.get("window_seconds") or 5 * 3600
                    elif label == "week":
                        r["window_seconds"] = r.get("window_seconds") or 7 * 86400
                    rows.append(r)
    else:
        usage = payload.get("usage")
        if isinstance(usage, dict):
            r = row_from(usage, "week")
            if r:
                r["window_seconds"] = 7 * 86400
                rows.append(r)
        limits = payload.get("limits")
        if isinstance(limits, list):
            for idx, item in enumerate(limits):
                if not isinstance(item, dict):
                    continue
                detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
                if isinstance(detail, dict):
                    label = _kimi_label_from_window(item.get("window"), f"limit#{idx + 1}")
                    r = row_from(detail, label)
                    if r:
                        secs = _kimi_window_seconds(item.get("window"))
                        if secs is not None:
                            r["window_seconds"] = secs
                        rows.append(r)

    if not rows:
        return error(name, "could not parse kimi usages payload")

    # Prefer week aggregate / first row with percent
    primary = next(
        (
            r
            for r in rows
            if str(r.get("label") or "").lower() in ("week", "weekly", "all", "weekly usage")
        ),
        rows[0],
    )
    used_pct = primary.get("used_percent")
    rem_pct = primary.get("remaining_percent")
    if used_pct is None and primary.get("used") is not None and primary.get("limit"):
        used_pct = (float(primary["used"]) / float(primary["limit"])) * 100.0
        rem_pct = max(0.0, 100.0 - used_pct)
    return QuotaRecord(
        name=name,
        status="ok",
        used_percent=float(used_pct) if used_pct is not None else None,
        remaining_percent=float(rem_pct) if rem_pct is not None else None,
        used=primary.get("used"),
        remaining=primary.get("remaining"),
        limit=primary.get("limit"),
        reset_at=primary.get("reset_at"),
        detail=f"{len(rows)} window(s)",
        windows=rows,
    )


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-None value from a mapping."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _ollama_usage_percent(window: dict[str, Any]) -> Optional[float]:
    """Convert Ollama's usage fraction/percentage into a 0–100 percentage."""
    raw = _first_value(
        window,
        "usage",
        "used_percentage",
        "usage_percentage",
        "usage_percent",
        "usedPercent",
        "usagePercent",
        "percent",
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # /api/usage currently returns usage as a fraction (0–1). Accept the
    # percentage spelling as well so a backend response change is harmless.
    if 0.0 <= value <= 1.0:
        value *= 100.0
    return max(0.0, min(100.0, value))


def _ollama_reset_at(window: dict[str, Any]) -> Optional[str]:
    reset = _first_value(
        window,
        "resets_at",
        "reset_at",
        "resetAt",
        "resetsAt",
        "reset_time",
        "resetTime",
    )
    parsed = _iso_from_any(reset)
    if parsed:
        return parsed
    seconds = _first_value(window, "reset_in_seconds", "reset_after_seconds", "resetInSec")
    try:
        if seconds is not None:
            return (datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat()
    except (TypeError, ValueError):
        pass
    return None


def normalize_ollama_usage(payload: dict[str, Any], name: str = "ollama-pro") -> QuotaRecord:
    """Normalize Ollama Cloud ``GET /api/usage``.

    The current response places ``usage`` fractions under
    ``limits.session`` and ``limits.weekly``.  The endpoint is not a token
    meter: it reports consumption against each plan cap, so the dashboard
    deliberately presents the result as remaining percentages.
    """
    raw_limits = payload.get("limits")
    limits = raw_limits if isinstance(raw_limits, dict) else {}
    # Keep a tolerant fallback for equivalent response shapes used by early
    # versions of the endpoint and by local fixtures.
    if not limits:
        limits = {
            key: payload.get(key)
            for key in ("session", "weekly")
            if isinstance(payload.get(key), dict)
        }

    windows: list[dict[str, Any]] = []
    for source_key, label, seconds in (
        ("session", "5h", 5 * 3600),
        ("weekly", "week", 7 * 86400),
    ):
        raw_window = limits.get(source_key)
        if not isinstance(raw_window, dict):
            continue
        used = _ollama_usage_percent(raw_window)
        remaining_raw = _first_value(
            raw_window,
            "remaining_percentage",
            "remaining_percent",
            "remainingPercent",
        )
        remaining: Optional[float] = None
        try:
            remaining = float(remaining_raw)
            if 0.0 <= remaining <= 1.0:
                remaining *= 100.0
            remaining = max(0.0, min(100.0, remaining))
        except (TypeError, ValueError):
            if used is not None:
                remaining = max(0.0, 100.0 - used)
        if used is None and remaining is None:
            continue
        if used is None:
            used = max(0.0, 100.0 - remaining) if remaining is not None else None
        if remaining is None and used is not None:
            remaining = max(0.0, 100.0 - used)
        window = {
            "label": label,
            "used_percent": used,
            "remaining_percent": remaining,
            "reset_at": _ollama_reset_at(raw_window),
            "window_seconds": seconds,
        }
        # Preserve harmless metadata useful to JSON consumers without ever
        # copying credentials or request contents into the record.
        if isinstance(raw_window.get("models"), list):
            window["models"] = raw_window["models"]
        windows.append(window)

    if not windows:
        return error(name, "usage payload missing session/weekly limits")

    primary = next((w for w in windows if w["label"] == "5h"), windows[0])
    plan = _first_value(payload, "plan", "plan_type", "planType")
    plan_text = str(plan).strip().lower() if plan is not None else "pro"
    detail = f"{plan_text} · cloud" if plan_text else "cloud"
    return QuotaRecord(
        name=name,
        status="ok",
        used_percent=primary.get("used_percent"),
        remaining_percent=primary.get("remaining_percent"),
        reset_at=primary.get("reset_at"),
        detail=detail,
        windows=windows,
    )


def _windsurf_nested(mapping: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        return {}
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _windsurf_first(mappings: list[dict[str, Any]], *keys: str) -> Any:
    for mapping in mappings:
        value = _first_value(mapping, *keys)
        if value is not None:
            return value
    return None


def _windsurf_percent(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return max(0.0, min(100.0, number))


def _windsurf_bucket(
    sources: list[dict[str, Any]],
    quota_usage: dict[str, Any],
    prefix: str,
    label: str,
    window_seconds: int,
) -> Optional[dict[str, Any]]:
    """Normalize one Windsurf daily/weekly quota bucket."""
    bucket_names = (
        ("daily", "day", "dailyQuota", "daily_quota")
        if prefix == "daily"
        else ("weekly", "week", "weeklyQuota", "weekly_quota")
    )
    bucket = _windsurf_nested(quota_usage, *bucket_names)
    if not bucket and quota_usage:
        # Cached clients store message/flow-action counters directly under
        # ``usage`` rather than nesting separate daily/weekly objects.
        bucket = quota_usage
    all_sources = [*sources, bucket]
    remaining = _windsurf_percent(
        _windsurf_first(
            all_sources,
            f"{prefix}QuotaRemainingPercent",
            f"{prefix}_quota_remaining_percent",
            f"{prefix}QuotaRemainingPercentage",
            f"{prefix}_quota_remaining_percentage",
            f"{prefix}RemainingPercent",
            f"{prefix}_remaining_percent",
            f"{prefix}RemainingPercentage",
            f"{prefix}_remaining_percentage",
            "remainingQuotaPercent",
            "remaining_quota_percent",
            "remainingPercent",
            "remaining_percent",
            "availablePercent",
            "available_percent",
        )
    )
    direct_devin_used = _windsurf_first(
        all_sources,
        f"{prefix}Percentage",
        f"{prefix}_percentage",
    )
    if direct_devin_used is not None:
        try:
            direct_value = float(direct_devin_used)
            if 0.0 <= direct_value < 1.0:
                direct_value *= 100.0
            used = _windsurf_percent(direct_value)
        except (TypeError, ValueError):
            used = None
    else:
        used = _windsurf_percent(
            _windsurf_first(
                all_sources,
                f"{prefix}QuotaUsedPercent",
                f"{prefix}_quota_used_percent",
                f"{prefix}QuotaUsagePercent",
                f"{prefix}_quota_usage_percent",
                f"{prefix}UsagePercent",
                f"{prefix}_usage_percent",
                "usedPercent",
                "used_percent",
                "usagePercent",
                "usage_percent",
            )
        )
    if remaining is None and used is None and bucket:
        if prefix == "daily":
            raw_used = _windsurf_first(
                [bucket],
                "usedMessages",
                "used_messages",
                "used",
                "usedAmount",
                "used_amount",
            )
            raw_limit = _windsurf_first(
                [bucket],
                "messages",
                "messageLimit",
                "message_limit",
                "limit",
                "max",
                "quota",
                "total",
            )
        else:
            raw_used = _windsurf_first(
                [bucket],
                "usedFlowActions",
                "used_flow_actions",
                "used",
                "usedAmount",
                "used_amount",
            )
            raw_limit = _windsurf_first(
                [bucket],
                "flowActions",
                "flow_actions",
                "flowActionLimit",
                "flow_action_limit",
                "limit",
                "max",
                "quota",
                "total",
            )
        try:
            if raw_used is not None and raw_limit is not None and float(raw_limit) > 0:
                used = _windsurf_percent(float(raw_used) / float(raw_limit) * 100.0)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if remaining is None and used is not None:
        remaining = max(0.0, 100.0 - used)
    if used is None and remaining is not None:
        used = max(0.0, 100.0 - remaining)
    if remaining is None or used is None:
        return None

    reset_raw = _windsurf_first(
        all_sources,
        f"{prefix}QuotaResetAtUnix",
        f"{prefix}_quota_reset_at_unix",
        f"{prefix}QuotaResetAt",
        f"{prefix}_quota_reset_at",
        f"{prefix}ResetAtUnix",
        f"{prefix}_reset_at_unix",
        f"{prefix}ResetAt",
        f"{prefix}_reset_at",
        "resetAtUnix",
        "reset_at_unix",
        "resetAt",
        "reset_at",
        "nextResetAtUnix",
        "next_reset_at_unix",
        "nextResetAt",
        "next_reset_at",
    )
    return {
        "label": label,
        # The renderer has two aligned slots. ``day`` occupies the short
        # slot while retaining its truthful label instead of calling it 5h.
        "display_label": "day" if prefix == "daily" else None,
        "used_percent": used,
        "remaining_percent": remaining,
        "reset_at": _iso_from_any(reset_raw),
        "window_seconds": window_seconds,
    }


def normalize_windsurf_usage(payload: dict[str, Any], name: str = "windsurf") -> QuotaRecord:
    """Normalize Windsurf/Devin shared daily and weekly quota JSON.

    The current desktop endpoint returns ``userStatus.planStatus`` in camel
    case.  The normalizer also accepts snake_case and the cached plan shapes
    written by older desktop clients so a client update does not blank the
    panel unnecessarily.
    """
    user_status = _windsurf_nested(payload, "userStatus", "user_status", "status")
    if not user_status:
        user_status = payload
    plan_status = _windsurf_nested(user_status, "planStatus", "plan_status", "plan")
    if not plan_status:
        plan_status = _windsurf_nested(payload, "planStatus", "plan_status") or user_status
    plan_info = _windsurf_nested(plan_status, "planInfo", "plan_info")
    if not plan_info:
        plan_info = _windsurf_nested(user_status, "planInfo", "plan_info")
    quota_usage = _windsurf_nested(
        plan_status,
        "quotaUsage",
        "quota_usage",
        "usage",
        "usageQuota",
        "usage_quota",
    )
    sources = [plan_status, quota_usage, user_status, payload]
    windows: list[dict[str, Any]] = []
    for prefix, label, seconds in (
        ("daily", "day", 86400),
        ("weekly", "week", 7 * 86400),
    ):
        window = _windsurf_bucket(sources, quota_usage, prefix, label, seconds)
        if window is not None:
            windows.append(window)
    if not windows:
        return error(name, "quota payload missing daily/weekly remaining percent")

    hide_daily = bool(
        _windsurf_first(
            [plan_info, plan_status, user_status, payload],
            "hideDailyQuota",
            "hide_daily_quota",
        )
    )
    if hide_daily and not any(w["label"] == "week" for w in windows):
        # Devin Max responses can put the weekly percentage in the daily
        # field while explicitly hiding the daily bucket.
        for window in windows:
            if window["label"] == "day":
                window["label"] = "week"
                window["display_label"] = None
                window["reset_at"] = _iso_from_any(
                    _windsurf_first(
                        [plan_status, quota_usage, user_status, payload],
                        "weeklyQuotaResetAtUnix",
                        "weekly_quota_reset_at_unix",
                        "weeklyResetAtUnix",
                        "weekly_reset_at_unix",
                        "weeklyQuotaResetAt",
                        "weekly_quota_reset_at",
                        "weeklyResetAt",
                        "weekly_reset_at",
                    )
                ) or window.get("reset_at")
                break

    primary = next((w for w in windows if w["label"] == "day"), windows[0])
    plan_name = _windsurf_first(
        [plan_info, plan_status, user_status, payload],
        "planName",
        "plan_name",
        "plan",
        "tier",
        "subscriptionTier",
        "subscription_tier",
        "name",
    )
    plan_text = str(plan_name).strip() if plan_name is not None else ""
    overage_micros = _windsurf_first(
        [plan_status, user_status, payload],
        "overageBalanceMicros",
        "overage_balance_micros",
        "extraUsageBalanceMicros",
        "extra_usage_balance_micros",
    )
    overage_dollars = _windsurf_first(
        [plan_status, user_status, payload],
        "overageBalance",
        "overage_balance",
        "extraUsageBalance",
        "extra_usage_balance",
    )
    overage_cents = _windsurf_first(
        [plan_status, user_status, payload],
        "overageBalanceCents",
        "overage_balance_cents",
        "extraUsageBalanceCents",
        "extra_usage_balance_cents",
    )
    detail_parts = [plan_text] if plan_text else []
    try:
        if overage_micros is not None:
            overage = float(overage_micros) / 1_000_000.0
        elif overage_cents is not None:
            overage = float(overage_cents) / 100.0
        else:
            overage = float(overage_dollars) if overage_dollars is not None else None
        if overage is not None:
            if math.isfinite(overage):
                detail_parts.append(f"extra=${overage:.2f}")
    except (TypeError, ValueError):
        pass
    return QuotaRecord(
        name=name,
        status="ok",
        used_percent=primary["used_percent"],
        remaining_percent=primary["remaining_percent"],
        reset_at=primary.get("reset_at"),
        detail=" · ".join(detail_parts) or "subscription",
        windows=windows,
    )


def _refresh_codex(cred: Credential, http: HttpFn) -> Optional[str]:
    if not cred.refresh_token:
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": CODEX_CLIENT_ID,
        }
    ).encode()
    status, _, raw = _http_with_retry(
        http,
        CODEX_TOKEN_URL,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        data=body,
    )
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode())
    except Exception:
        return None
    access = data.get("access_token")
    if not access:
        return None
    # Best-effort persist (local only)
    if cred.path:
        try:
            p = Path(cred.path)
            doc = json.loads(p.read_text(encoding="utf-8"))
            tokens = doc.setdefault("tokens", {})
            tokens["access_token"] = access
            if data.get("refresh_token"):
                tokens["refresh_token"] = data["refresh_token"]
            if data.get("id_token"):
                tokens["id_token"] = data["id_token"]
            doc["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    cred.access_token = access
    if data.get("refresh_token"):
        cred.refresh_token = data["refresh_token"]
    return access


def _refresh_grok(cred: Credential, http: HttpFn) -> Optional[str]:
    if not cred.refresh_token or not cred.oidc_client_id:
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": cred.oidc_client_id,
        }
    ).encode()
    status, _, raw = _http_with_retry(
        http,
        GROK_TOKEN_URL,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        data=body,
    )
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode())
    except Exception:
        return None
    access = data.get("access_token")
    if not access:
        return None
    if cred.path and cred.auth_entry_key:
        try:
            p = Path(cred.path)
            doc = json.loads(p.read_text(encoding="utf-8"))
            entry = doc.get(cred.auth_entry_key) or {}
            entry["key"] = access
            if data.get("refresh_token"):
                entry["refresh_token"] = data["refresh_token"]
            exp = data.get("expires_in")
            if exp:
                entry["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=int(exp))
                ).isoformat()
            doc[cred.auth_entry_key] = entry
            p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    cred.access_token = access
    if data.get("refresh_token"):
        cred.refresh_token = data["refresh_token"]
    return access


def fetch_codex(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    if not cred.present or not cred.access_token:
        return unavailable("codex", "no local ~/.codex/auth.json tokens")
    # With the normal transport, delegate auth and quota compatibility to the
    # installed first-party Codex app-server. Injected HTTP functions in tests
    # intentionally retain the direct path below.
    if http is _default_http:
        # Never fall back to the private chatgpt.com web endpoint in normal
        # operation. Cloudflare frequently rejects that endpoint with 403,
        # while the first-party app-server owns token refresh and compatibility.
        for attempt in range(2):
            official = _fetch_codex_app_server(timeout=35.0)
            if official is not None:
                return official
            if attempt == 0:
                time.sleep(1.0)
        return error("codex", "official app-server unavailable after retry")
    headers = {
        "Authorization": f"Bearer {cred.access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if cred.account_id:
        headers["ChatGPT-Account-Id"] = cred.account_id
    status, _, raw = _http_with_retry(http, CODEX_USAGE_URL, headers=headers)
    if status == 401:
        new = _refresh_codex(cred, http)
        if new:
            headers["Authorization"] = f"Bearer {new}"
            status, _, raw = _http_with_retry(http, CODEX_USAGE_URL, headers=headers)
    if status != 200:
        return error("codex", f"HTTP {status}: {raw[:180].decode(errors='replace')}")
    try:
        payload = json.loads(raw.decode())
    except Exception as e:
        return error("codex", f"invalid JSON: {e}")
    return normalize_codex_usage(payload)


def fetch_grok(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    if not cred.present or not cred.access_token:
        return unavailable("grok", "no local ~/.grok/auth.json OIDC token")
    headers = {
        "Authorization": f"Bearer {cred.access_token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-grok-client-surface": "grok-build",
        "x-grok-client-version": "1.0.0",
    }
    try:
        status, _, raw = _http_with_retry(http, GROK_BILLING_URL, headers=headers)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        if http is not _default_http:
            raise
        status, _, raw = _native_curl_get(GROK_BILLING_URL, headers)
    if status == 401:
        new = _refresh_grok(cred, http)
        if new:
            headers["Authorization"] = f"Bearer {new}"
            try:
                status, _, raw = _http_with_retry(http, GROK_BILLING_URL, headers=headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                status, _, raw = _native_curl_get(GROK_BILLING_URL, headers)
    if status != 200:
        return error("grok", f"HTTP {status}: {raw[:180].decode(errors='replace')}")
    try:
        payload = json.loads(raw.decode())
    except Exception as e:
        return error("grok", f"invalid JSON: {e}")
    return normalize_grok_billing(payload)


def _kimi_token_expired(cred: Credential, *, skew_sec: int = 60) -> bool:
    """True when kimi-code OAuth access_token is past expires_at (unix seconds)."""
    if not cred.expires_at:
        return False
    try:
        exp = float(cred.expires_at)
    except (TypeError, ValueError):
        # ISO string fallback
        try:
            s = str(cred.expires_at).replace("Z", "+00:00")
            exp_dt = datetime.fromisoformat(s)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            exp = exp_dt.timestamp()
        except Exception:
            return False
    return exp <= (datetime.now(timezone.utc).timestamp() + skew_sec)


def _refresh_kimi(cred: Credential, http: HttpFn) -> Optional[str]:
    """Refresh kimi-code OAuth access_token via auth.kimi.com device-code client."""
    if not cred.refresh_token:
        return None
    client_id = cred.oidc_client_id or KIMI_CODE_OAUTH_CLIENT_ID
    oauth_host = (cred.extra.get("oauth_host") or KIMI_CODE_OAUTH_HOST).rstrip("/")
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": cred.refresh_token,
            "client_id": client_id,
        }
    ).encode()
    status, _, raw = http(
        f"{oauth_host}/api/oauth/token",
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "KimiCLI/1.6",
        },
        data=body,
    )
    if status != 200:
        return None
    try:
        data = json.loads(raw.decode())
    except Exception:
        return None
    access = data.get("access_token")
    if not access:
        return None
    expires_in = data.get("expires_in")
    expires_at_unix: Optional[int] = None
    if expires_in is not None:
        try:
            expires_at_unix = int(datetime.now(timezone.utc).timestamp()) + int(expires_in)
        except (TypeError, ValueError):
            expires_at_unix = None
    # Best-effort persist back into ~/.kimi-code/credentials/kimi-code.json
    if cred.path:
        try:
            p = Path(cred.path)
            doc = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                doc = {}
            doc["access_token"] = access
            if data.get("refresh_token"):
                doc["refresh_token"] = data["refresh_token"]
            if data.get("token_type"):
                doc["token_type"] = data["token_type"]
            if data.get("scope"):
                doc["scope"] = data["scope"]
            if expires_in is not None:
                doc["expires_in"] = int(expires_in)
            if expires_at_unix is not None:
                doc["expires_at"] = expires_at_unix
            p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    cred.access_token = access
    if data.get("refresh_token"):
        cred.refresh_token = data["refresh_token"]
    if expires_at_unix is not None:
        cred.expires_at = str(expires_at_unix)
    return access


def fetch_kimi(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    # Prefer static API key; otherwise kimi-code OAuth access_token.
    token = cred.api_key or cred.access_token
    if not cred.present or not token:
        if cred.present and cred.refresh_token:
            token = _refresh_kimi(cred, http)
        if not token:
            return unavailable(
                "kimi",
                "no KIMI_API_KEY / KIMI_CODING_API_KEY, local key, or ~/.kimi-code OAuth",
            )
    # Proactively refresh short-lived OAuth tokens before calling usages.
    if not cred.api_key and cred.refresh_token and _kimi_token_expired(cred):
        refreshed = _refresh_kimi(cred, http)
        if refreshed:
            token = refreshed
    base = (cred.extra.get("base_url") or KIMI_DEFAULT_BASE).rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "KimiCLI/1.6",
    }
    status, _, raw = http(f"{base}/usages", headers=headers)
    if status == 404:
        status, _, raw = http(f"{base}/usage", headers=headers)
    if status == 401 and not cred.api_key and cred.refresh_token:
        new = _refresh_kimi(cred, http)
        if new:
            headers["Authorization"] = f"Bearer {new}"
            status, _, raw = http(f"{base}/usages", headers=headers)
            if status == 404:
                status, _, raw = http(f"{base}/usage", headers=headers)
    if status != 200:
        return error("kimi", f"HTTP {status}: {raw[:180].decode(errors='replace')}")
    try:
        payload = json.loads(raw.decode())
    except Exception as e:
        return error("kimi", f"invalid JSON: {e}")
    return normalize_kimi_usages(payload)


_RE_NUM = r"(-?\d+(?:\.\d+)?)"
_RE_ROLLING = [
    re.compile(rf"rollingUsage:\$R\[\d+\]=\{{[^}}]*usagePercent:{_RE_NUM}[^}}]*resetInSec:{_RE_NUM}[^}}]*\}}"),
    re.compile(rf"rollingUsage:\$R\[\d+\]=\{{[^}}]*resetInSec:{_RE_NUM}[^}}]*usagePercent:{_RE_NUM}[^}}]*\}}"),
]
_RE_WEEKLY = [
    re.compile(rf"weeklyUsage:\$R\[\d+\]=\{{[^}}]*usagePercent:{_RE_NUM}[^}}]*resetInSec:{_RE_NUM}[^}}]*\}}"),
    re.compile(rf"weeklyUsage:\$R\[\d+\]=\{{[^}}]*resetInSec:{_RE_NUM}[^}}]*usagePercent:{_RE_NUM}[^}}]*\}}"),
]
_RE_MONTHLY = [
    re.compile(rf"monthlyUsage:\$R\[\d+\]=\{{[^}}]*usagePercent:{_RE_NUM}[^}}]*resetInSec:{_RE_NUM}[^}}]*\}}"),
    re.compile(rf"monthlyUsage:\$R\[\d+\]=\{{[^}}]*resetInSec:{_RE_NUM}[^}}]*usagePercent:{_RE_NUM}[^}}]*\}}"),
]


def _parse_ssr_window(html: str, patterns: list[re.Pattern[str]], pct_first: bool) -> Optional[dict[str, float]]:
    for i, pat in enumerate(patterns):
        m = pat.search(html)
        if not m:
            continue
        a, b = float(m.group(1)), float(m.group(2))
        if i == 0:  # usage then reset
            return {"usagePercent": a, "resetInSec": b}
        return {"usagePercent": b, "resetInSec": a}
    return None


def parse_opencode_go_dashboard(html: str) -> dict[str, dict[str, float]]:
    """Parse OpenCode Go workspace dashboard HTML for usage windows."""
    out: dict[str, dict[str, float]] = {}
    for key, pats in (
        ("rolling", _RE_ROLLING),
        ("weekly", _RE_WEEKLY),
        ("monthly", _RE_MONTHLY),
    ):
        w = _parse_ssr_window(html, pats, True)
        if w:
            out[key] = w
    if out:
        return out
    # data-slot fallback
    parts = html.split('data-slot="usage-item"')
    for chunk in parts[1:]:
        lm = re.search(r'data-slot="usage-label">([^<]+)<', chunk)
        if not lm:
            continue
        label = lm.group(1).strip().lower()
        um = re.search(r'data-slot="usage-value">[^0-9]*(\d+(?:\.\d+)?)', chunk)
        if not um:
            continue
        usage = float(um.group(1))
        rm = re.search(r'data-slot="(reset-time|reset-now)">([\s\S]*?)</span>', chunk)
        if not rm:
            continue
        if rm.group(1) == "reset-now":
            reset_in = 0.0
        else:
            text = re.sub(r"<!--.*?-->", "", rm.group(2))
            text = re.sub(r"Resets?\s*in\s*", "", text, flags=re.I).strip().lower()
            reset_in = 0.0
            dm = re.search(r"(\d+(?:\.\d+)?)\s*days?", text)
            hm = re.search(r"(\d+(?:\.\d+)?)\s*hours?", text)
            mm = re.search(r"(\d+(?:\.\d+)?)\s*minutes?", text)
            sm = re.search(r"(\d+(?:\.\d+)?)\s*seconds?", text)
            if not (dm or hm or mm or sm):
                continue
            if dm:
                reset_in += float(dm.group(1)) * 86400
            if hm:
                reset_in += float(hm.group(1)) * 3600
            if mm:
                reset_in += float(mm.group(1)) * 60
            if sm:
                reset_in += float(sm.group(1))
        key = None
        if "rolling" in label or "5h" in label:
            key = "rolling"
        elif "weekly" in label:
            key = "weekly"
        elif "monthly" in label:
            key = "monthly"
        if key:
            out[key] = {"usagePercent": usage, "resetInSec": reset_in}
    return out


def fetch_opencode_go(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    if not cred.present:
        return unavailable(
            "opencode-go",
            "no ~/.local/share/opencode/auth.json key or dashboard cookie",
        )
    # Prefer dashboard scrape when workspace + cookie available
    wid = cred.extra.get("workspace_id")
    cookie = cred.extra.get("auth_cookie")
    if wid and cookie:
        url = OPENCODE_GO_DASHBOARD.format(wid=urllib.parse.quote(str(wid), safe=""))
        status, _, raw = http(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": USER_AGENT,
                "Cookie": f"auth={cookie}",
            },
        )
        if status != 200:
            return error("opencode-go", f"dashboard HTTP {status}")
        html = raw.decode(errors="replace")
        windows = parse_opencode_go_dashboard(html)
        if not windows:
            return error("opencode-go", "dashboard HTML had no usage windows")
        # Prefer rolling (5h), else weekly, else monthly
        primary_key = next((k for k in ("rolling", "weekly", "monthly") if k in windows), None)
        assert primary_key is not None
        primary = windows[primary_key]
        used = float(primary["usagePercent"])
        rem = max(0.0, 100.0 - used)
        reset_at = (
            datetime.now(timezone.utc) + timedelta(seconds=float(primary["resetInSec"]))
        ).isoformat()
        win_list = []
        for k, v in windows.items():
            win_list.append(
                {
                    "label": k,
                    "used_percent": v["usagePercent"],
                    "reset_at": (
                        datetime.now(timezone.utc) + timedelta(seconds=float(v["resetInSec"]))
                    ).isoformat(),
                }
            )
        # Canonical labels for layout: rolling → 5h, weekly → week
        label_map = {"rolling": "5h", "weekly": "week", "monthly": "monthly"}
        for w in win_list:
            raw = str(w.get("label") or "")
            w["label"] = label_map.get(raw, raw)
            if w["label"] == "5h":
                w["window_seconds"] = 5 * 3600
            elif w["label"] == "week":
                w["window_seconds"] = 7 * 86400
            if w.get("used_percent") is not None:
                try:
                    w["remaining_percent"] = max(0.0, 100.0 - float(w["used_percent"]))
                except (TypeError, ValueError):
                    pass
        return QuotaRecord(
            name="opencode-go",
            status="ok",
            used_percent=used,
            remaining_percent=rem,
            reset_at=reset_at,
            detail=f"window={primary_key}",
            windows=win_list,
        )

    # API key alone: verify it works, then honest unavailable for windows
    if cred.api_key:
        status, _, raw = http(
            OPENCODE_GO_MODELS_URL,
            headers={
                "Authorization": f"Bearer {cred.api_key}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        if status == 200:
            return QuotaRecord(
                name="opencode-go",
                status="key ok",
                detail="Go key verified; usage needs console login",
            )
        return error("opencode-go", f"API key rejected HTTP {status}")
    return unavailable("opencode-go", "no usable credential")


def fetch_ollama_pro(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    """Fetch Ollama Cloud session and weekly usage with an API key."""
    if not cred.present or not cred.api_key:
        return unavailable(
            "ollama-pro",
            "no OLLAMA_API_KEY / OLLAMA_PRO_API_KEY",
        )
    headers = {
        "Authorization": f"Bearer {cred.api_key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    status, _, raw = _http_with_retry(http, OLLAMA_USAGE_URL, headers=headers)
    if status != 200:
        return error("ollama-pro", f"HTTP {status}: {raw[:180].decode(errors='replace')}")
    try:
        payload = json.loads(raw.decode())
    except Exception as e:
        return error("ollama-pro", f"invalid JSON: {e}")
    if not isinstance(payload, dict):
        return error("ollama-pro", "usage response is not a JSON object")
    return normalize_ollama_usage(payload)


def _devin_quota_paths(cred: Credential) -> tuple[list[str], Optional[str]]:
    """Build the candidate app.devin.ai quota paths for a web session."""
    raw = str(cred.extra.get("organization") or "").strip()
    internal = str(cred.extra.get("internal_organization_id") or "").strip() or None
    if raw:
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme and parsed.netloc and parsed.netloc.lower().endswith("devin.ai"):
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0] in ("org", "organizations"):
                raw = f"{parts[0]}/{parts[1]}"
        raw = raw.strip("/")

    if raw.startswith("organizations/"):
        normalized = raw
        internal = internal or raw.split("/", 1)[1]
    elif raw.startswith("org/"):
        normalized = raw
    elif raw.startswith(("org-", "org_")):
        normalized = f"organizations/{raw}"
        internal = internal or raw
    elif raw:
        normalized = f"org/{raw}"
    else:
        normalized = ""

    paths: list[str] = []
    if internal:
        paths.append(f"{internal}/billing/quota/usage")
    if normalized:
        paths.append(f"{normalized}/billing/quota/usage")
        if normalized.startswith("org/"):
            paths.append(f"{normalized[4:]}/billing/quota/usage")
        elif not normalized.startswith("organizations/"):
            paths.append(f"org/{normalized}/billing/quota/usage")
        if internal:
            paths.append(f"organizations/{internal}/billing/quota/usage")
    return list(dict.fromkeys(paths)), internal


def fetch_devin_web_quota(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    """Fetch Devin's self-serve quota from its authenticated web endpoint."""
    token = cred.access_token
    if not token:
        return unavailable("windsurf", "no Devin web Bearer token")
    paths, internal = _devin_quota_paths(cred)
    if not paths:
        return unavailable(
            "windsurf",
            "set DEVIN_ORG/DEVIN_ORGANIZATION for the Devin quota endpoint",
        )
    base_url = os.environ.get("DEVIN_QUOTA_BASE_URL", DEVIN_WEB_QUOTA_BASE_URL).strip().rstrip("/")
    if not base_url.lower().startswith("https://"):
        base_url = DEVIN_WEB_QUOTA_BASE_URL
    last_status: Optional[int] = None
    for path in paths:
        url = f"{base_url}/{path}"
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        }
        if internal:
            headers["x-cog-org-id"] = internal
        try:
            status, _, raw = _http_with_retry(
                http,
                url,
                attempts=2,
                base_delay=0.2,
                headers=headers,
                timeout=12.0,
            )
        except Exception as exc:
            return error("windsurf", f"Devin quota request failed: {type(exc).__name__}: {exc}")
        last_status = status
        if status in (401, 403):
            return error("windsurf", f"Devin web session expired HTTP {status}; sign in again")
        if status != 200:
            continue
        try:
            payload = json.loads(raw.decode())
        except Exception as exc:
            return error("windsurf", f"invalid Devin quota JSON: {exc}")
        if not isinstance(payload, dict):
            return error("windsurf", "Devin quota response is not a JSON object")
        record = normalize_windsurf_usage(payload)
        if record.status == "ok":
            return record
        return error("windsurf", record.detail or "invalid Devin quota payload")
    return error("windsurf", f"Devin quota endpoint HTTP {last_status or 0}")


def _windsurf_cached_record(cred: Credential) -> Optional[QuotaRecord]:
    cached = cred.extra.get("cached_plan_info") if isinstance(cred.extra, dict) else None
    if not isinstance(cached, dict):
        return None
    record = normalize_windsurf_usage(cached)
    if record.status == "ok":
        record.detail = f"{record.detail} · cached"
        return record
    return None


def fetch_windsurf(cred: Credential, http: HttpFn = _default_http) -> QuotaRecord:
    """Fetch Windsurf/Devin shared daily and weekly subscription quota."""
    if not cred.present:
        if cred.kind == "unsupported":
            return unavailable(
                "windsurf",
                "DEVIN_API_KEY is a REST session key; use Devin desktop/CLI login or DEVIN_BEARER_TOKEN + DEVIN_ORG",
            )
        return unavailable(
            "windsurf",
            "no WINDSURF_API_KEY or local Windsurf login",
        )

    if cred.kind == "bearer" or cred.extra.get("quota_source") == "devin-web":
        return fetch_devin_web_quota(cred, http=http)

    cached = _windsurf_cached_record(cred)
    if not cred.api_key:
        return cached or unavailable(
            "windsurf",
            "no usable Windsurf API key or cached quota",
        )

    variant = str(cred.extra.get("variant") or "windsurf")
    ide_name = "devin" if variant.startswith("devin") else (
        "windsurf-next" if variant == "windsurf-next" else "windsurf"
    )
    version = os.environ.get("WINDSURF_CLIENT_VERSION", WINDSURF_CLIENT_VERSION).strip()
    if not version:
        version = WINDSURF_CLIENT_VERSION
    body = json.dumps(
        {
            "metadata": {
                "apiKey": cred.api_key,
                "ideName": ide_name,
                "ideVersion": version,
                "extensionName": ide_name,
                "extensionVersion": version,
                "locale": "en",
            }
        },
        separators=(",", ":"),
    ).encode()
    configured_server = str(cred.extra.get("api_server_url") or "").strip().rstrip("/")
    if configured_server and not configured_server.lower().startswith("https://"):
        configured_server = ""
    if configured_server:
        usage_urls = [f"{configured_server}/exa.seat_management_pb.SeatManagementService/GetUserStatus"]
    elif variant.startswith("devin"):
        usage_urls = [DEVIN_USAGE_URL, WINDSURF_USAGE_URL]
    else:
        usage_urls = [WINDSURF_USAGE_URL, DEVIN_USAGE_URL]

    last_status: Optional[int] = None
    last_raw = b""
    for usage_url in usage_urls:
        try:
            status, _, raw = _http_with_retry(
                http,
                usage_url,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                    "User-Agent": USER_AGENT,
                },
                data=body,
            )
        except Exception as exc:
            if cached is not None:
                return cached
            return error("windsurf", f"{type(exc).__name__}: {exc}")
        last_status, last_raw = status, raw
        if status != 200:
            if status in (401, 403) and configured_server:
                break
            continue
        try:
            payload = json.loads(raw.decode())
        except Exception as exc:
            return cached or error("windsurf", f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return cached or error("windsurf", "usage response is not a JSON object")
        record = normalize_windsurf_usage(payload)
        if record.status == "ok":
            return record
        break

    if cached is not None:
        return cached
    hint = " (sign in to Windsurf/Devin again)" if last_status in (401, 403) else ""
    return error(
        "windsurf",
        f"HTTP {last_status or 0}{hint}: {last_raw[:180].decode(errors='replace')}",
    )


_FETCHERS = {
    "codex": fetch_codex,
    "grok": fetch_grok,
    "kimi": fetch_kimi,
    "opencode-go": fetch_opencode_go,
    "ollama-pro": fetch_ollama_pro,
    "windsurf": fetch_windsurf,
}


def fetch_all_quotas(
    creds: Optional[dict[str, Credential]] = None,
    http: HttpFn = _default_http,
) -> list[QuotaRecord]:
    """Fetch all six services; failures become per-service error records."""
    if creds is None:
        creds = discover_credentials()
    out: list[QuotaRecord] = []
    for name in SERVICE_NAMES:
        cred = creds.get(name) or Credential(service=name, present=False, kind="missing")
        try:
            rec = _FETCHERS[name](cred, http=http)
        except Exception as e:
            rec = error(name, f"{type(e).__name__}: {e}")
        out.append(rec)
    return out
