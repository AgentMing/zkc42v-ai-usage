"""Per-provider quota fetch + normalize into QuotaRecord."""

from __future__ import annotations

import json
import os
import re
import queue
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
        # Use native Codex on Windows. WSL may have no route to chatgpt.com
        # even while the Windows host is online, causing every RPC to time out.
        cmd = ["cmd", "/d", "/c", "codex.cmd", "app-server", "--listen", "stdio://"]
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


_FETCHERS = {
    "codex": fetch_codex,
    "grok": fetch_grok,
    "kimi": fetch_kimi,
    "opencode-go": fetch_opencode_go,
}


def fetch_all_quotas(
    creds: Optional[dict[str, Credential]] = None,
    http: HttpFn = _default_http,
) -> list[QuotaRecord]:
    """Fetch all four services; failures become per-service error records."""
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
