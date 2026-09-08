"""Discover local account credentials for quota providers (secrets never logged)."""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Credential:
    """Opaque credential handle for one service."""

    service: str
    present: bool
    kind: str = ""  # oauth | api_key | bearer | cache | missing | unsupported
    path: Optional[str] = None
    # raw secret material — never print
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    account_id: Optional[str] = None
    api_key: Optional[str] = None
    expires_at: Optional[str] = None
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    auth_entry_key: Optional[str] = None  # for grok multi-entry auth.json
    extra: dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Safe summary for logs/tests (no secrets)."""
        return {
            "service": self.service,
            "present": self.present,
            "kind": self.kind,
            "path": self.path,
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "has_api_key": bool(self.api_key),
            "has_account_id": bool(self.account_id),
            "expires_at": self.expires_at,
            "oidc_issuer": self.oidc_issuer,
            "oidc_client_id": self.oidc_client_id,
            "extra_keys": sorted(self.extra.keys()),
        }


@functools.lru_cache(maxsize=1)
def _wsl_home() -> Optional[Path]:
    """On Windows, resolve the Linux home dir of the default WSL distro.

    The CLI tools (codex/grok/kimi/opencode-go) may live inside WSL, so their
    credential files (~/.codex/auth.json, ...) are reached over the
    \\\\wsl.localhost UNC share rather than the Windows profile.
    """
    try:
        out = subprocess.run(
            ["wsl", "-e", "sh", "-c", 'echo "$HOME"'],
            capture_output=True,
            text=True,
            timeout=10,
        )
        posix = (out.stdout or "").strip()
        if not posix.startswith("/"):
            return None
        win = subprocess.run(
            ["wsl", "-e", "wslpath", "-w", posix],
            capture_output=True,
            text=True,
            timeout=10,
        )
        wp = (win.stdout or "").strip()
        if wp:
            return Path(wp)
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _home() -> Path:
    for env in ("EPAPER_HOME", "EPAPER_CRED_HOME"):
        val = os.environ.get(env, "").strip()
        if val:
            return Path(val)
    if os.name == "nt":
        wsl = _wsl_home()
        if wsl is not None and wsl.is_dir():
            return wsl
    return Path(os.path.expanduser("~"))


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def discover_codex(home: Optional[Path] = None) -> Credential:
    home = home or _home()
    path = home / ".codex" / "auth.json"
    if not path.is_file():
        return Credential(service="codex", present=False, kind="missing", path=str(path))
    try:
        data = _read_json(path)
    except Exception:
        return Credential(service="codex", present=False, kind="missing", path=str(path))
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    refresh = tokens.get("refresh_token") if isinstance(tokens, dict) else None
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    if not access:
        return Credential(service="codex", present=False, kind="missing", path=str(path))
    return Credential(
        service="codex",
        present=True,
        kind="oauth",
        path=str(path),
        access_token=access,
        refresh_token=refresh,
        account_id=account_id,
        expires_at=data.get("last_refresh"),
        extra={"auth_mode": data.get("auth_mode")},
    )


def discover_grok(home: Optional[Path] = None) -> Credential:
    home = home or _home()
    path = home / ".grok" / "auth.json"
    if not path.is_file():
        return Credential(service="grok", present=False, kind="missing", path=str(path))
    try:
        data = _read_json(path)
    except Exception:
        return Credential(service="grok", present=False, kind="missing", path=str(path))
    if not isinstance(data, dict) or not data:
        return Credential(service="grok", present=False, kind="missing", path=str(path))
    # Prefer entries with a usable access token ("key")
    best_key = None
    best = None
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        token = v.get("key") or v.get("access_token") or v.get("access")
        if token:
            best_key, best = k, v
            break
    if not best:
        return Credential(service="grok", present=False, kind="missing", path=str(path))
    return Credential(
        service="grok",
        present=True,
        kind="oauth",
        path=str(path),
        access_token=best.get("key") or best.get("access_token") or best.get("access"),
        refresh_token=best.get("refresh_token"),
        expires_at=best.get("expires_at") or (
            str(best["expires"]) if isinstance(best.get("expires"), (int, float)) else None
        ),
        oidc_issuer=best.get("oidc_issuer") or "https://auth.x.ai",
        oidc_client_id=best.get("oidc_client_id"),
        auth_entry_key=best_key,
        extra={
            "auth_mode": best.get("auth_mode"),
            "email": best.get("email"),
            "user_id": best.get("user_id"),
        },
    )


def _kimi_key_from_env() -> Optional[str]:
    for name in ("KIMI_CODING_API_KEY", "KIMI_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return None


def _kimi_key_from_files(home: Path) -> tuple[Optional[str], Optional[str]]:
    """Search common local config files for a Coding Plan key (sk-kimi-...)."""
    candidates = [
        home / ".config" / "kimi" / "credentials.json",
        home / ".config" / "kimi-code" / "credentials.json",
        home / ".kimi" / "credentials.json",
        home / ".kimi" / "config.json",
        home / ".config" / "kimi" / "config.toml",
        home / ".config" / "kimi-cli" / "config.toml",
        home / ".kimi" / "config.toml",
        home / ".kimi-code" / "config.toml",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # JSON
        if path.suffix == ".json":
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                for k in ("api_key", "apiKey", "KIMI_API_KEY", "KIMI_CODING_API_KEY", "key"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip(), str(path)
        # TOML-ish / plain: look for sk-kimi- or key = "..."
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            if "sk-kimi-" in s:
                # extract quoted or bare token
                for part in s.replace("=", " ").replace(":", " ").split():
                    tok = part.strip("\"'")
                    if tok.startswith("sk-kimi-"):
                        return tok, str(path)
            low = s.lower()
            if "api_key" in low or "apikey" in low:
                for part in s.replace("=", " ").replace(":", " ").split():
                    tok = part.strip("\"'")
                    if tok.startswith("sk-") and len(tok) > 10:
                        return tok, str(path)
    return None, None


def _kimi_oauth_from_kimi_code(home: Path) -> Optional[Credential]:
    """Load device-login OAuth tokens written by the official kimi-code CLI."""
    path = home / ".kimi-code" / "credentials" / "kimi-code.json"
    if not path.is_file():
        return None
    try:
        data = _read_json(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access and not refresh:
        return None
    expires_at = data.get("expires_at")
    if isinstance(expires_at, (int, float)):
        expires_at = str(int(expires_at))
    elif expires_at is not None:
        expires_at = str(expires_at)
    return Credential(
        service="kimi",
        present=True,
        kind="oauth",
        path=str(path),
        access_token=access if isinstance(access, str) else None,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expires_at=expires_at,
        # Public kimi-code CLI OAuth client id (device-code login).
        oidc_client_id=os.environ.get(
            "KIMI_CODE_OAUTH_CLIENT_ID", "17e5f671-d194-4dfb-9706-5516cb48c098"
        ),
        extra={
            "base_url": os.environ.get(
                "KIMI_BASE_URL",
                os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1"),
            ),
            "oauth_host": os.environ.get(
                "KIMI_CODE_OAUTH_HOST",
                os.environ.get("KIMI_OAUTH_HOST", "https://auth.kimi.com"),
            ),
            "scope": data.get("scope"),
            "token_type": data.get("token_type"),
        },
    )


def discover_kimi(home: Optional[Path] = None) -> Credential:
    home = home or _home()
    # 1) Explicit API keys win (env / config files)
    key = _kimi_key_from_env()
    path = None
    if key:
        path = "env:KIMI_CODING_API_KEY|KIMI_API_KEY"
    else:
        key, path = _kimi_key_from_files(home)
    if key:
        return Credential(
            service="kimi",
            present=True,
            kind="api_key",
            path=path,
            api_key=key,
            extra={"base_url": os.environ.get("KIMI_BASE_URL", "https://api.kimi.com/coding/v1")},
        )
    # 2) Official kimi-code CLI login (~/.kimi-code/credentials/kimi-code.json)
    oauth = _kimi_oauth_from_kimi_code(home)
    if oauth is not None:
        return oauth
    return Credential(service="kimi", present=False, kind="missing", path=None)


def discover_opencode_go(home: Optional[Path] = None) -> Credential:
    home = home or _home()
    auth_path = home / ".local" / "share" / "opencode" / "auth.json"
    account_path = home / ".local" / "share" / "opencode" / "account.json"
    key = None
    path = None
    if auth_path.is_file():
        try:
            data = _read_json(auth_path)
            entry = data.get("opencode-go") if isinstance(data, dict) else None
            if isinstance(entry, dict):
                key = entry.get("key") or entry.get("api_key")
                path = str(auth_path)
        except Exception:
            pass
    if not key and account_path.is_file():
        try:
            data = _read_json(account_path)
            accounts = data.get("accounts") if isinstance(data, dict) else None
            active = (data.get("active") or {}).get("opencode-go") if isinstance(data, dict) else None
            if isinstance(accounts, dict):
                if active and active in accounts:
                    cred = accounts[active].get("credential") or {}
                    key = cred.get("key")
                    path = str(account_path)
                else:
                    for acc in accounts.values():
                        if isinstance(acc, dict) and acc.get("serviceID") == "opencode-go":
                            cred = acc.get("credential") or {}
                            key = cred.get("key")
                            path = str(account_path)
                            break
        except Exception:
            pass
    # Optional dashboard scrape material
    workspace_id = os.environ.get("OPENCODE_GO_WORKSPACE_ID", "").strip()
    auth_cookie = os.environ.get("OPENCODE_GO_AUTH_COOKIE", "").strip()
    go_cfg_paths = [
        home / ".config" / "opencode" / "opencode-quota" / "opencode-go.json",
        home / ".config" / "opencode-quota" / "opencode-go.json",
    ]
    for cfg_path in go_cfg_paths:
        if workspace_id and auth_cookie:
            break
        if not cfg_path.is_file():
            continue
        try:
            cfg = _read_json(cfg_path)
            if isinstance(cfg, dict):
                workspace_id = workspace_id or str(cfg.get("workspaceId") or "").strip()
                auth_cookie = auth_cookie or str(cfg.get("authCookie") or "").strip()
        except Exception:
            pass

    if not key and not (workspace_id and auth_cookie):
        return Credential(service="opencode-go", present=False, kind="missing", path=str(auth_path))
    return Credential(
        service="opencode-go",
        present=True,
        kind="api_key" if key else "dashboard",
        path=path or str(auth_path),
        api_key=key,
        extra={
            "workspace_id": workspace_id or None,
            "auth_cookie": auth_cookie or None,
            "has_dashboard": bool(workspace_id and auth_cookie),
        },
    )


def discover_ollama_pro(home: Optional[Path] = None) -> Credential:
    """Discover the Ollama Cloud API key from the process environment.

    Ollama documents ``OLLAMA_API_KEY`` as the standard programmatic
    credential.  ``OLLAMA_PRO_API_KEY`` is accepted as an explicit alias so
    a local Ollama key can coexist with this dashboard's provider setting.
    """
    del home  # kept for a consistent discover_* signature
    for env_name in ("OLLAMA_API_KEY", "OLLAMA_PRO_API_KEY"):
        key = os.environ.get(env_name, "").strip()
        if key:
            return Credential(
                service="ollama-pro",
                present=True,
                kind="api_key",
                path=f"env:{env_name}",
                api_key=key,
            )
    return Credential(
        service="ollama-pro",
        present=False,
        kind="missing",
        path="env:OLLAMA_API_KEY|OLLAMA_PRO_API_KEY",
    )


def _windsurf_decode_value(value: Any) -> Any:
    """Decode a SQLite value that may contain JSON encoded more than once."""
    current = value
    for _ in range(3):
        if not isinstance(current, (str, bytes, bytearray)):
            break
        try:
            decoded = json.loads(current)
        except (TypeError, ValueError, json.JSONDecodeError):
            break
        if decoded == current:
            break
        current = decoded
    return current


def _windsurf_find_api_key(value: Any) -> Optional[str]:
    """Find only the API-key field from a Windsurf auth payload."""
    value = _windsurf_decode_value(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("sk-ws-", "sk-windsurf-")):
            return text
        return None
    if isinstance(value, dict):
        for key in ("apiKey", "api_key"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _windsurf_find_api_key(child)
            if found:
                return found
    return None


def _read_sqlite_item(path: Path, key: str) -> Any:
    """Read one VS Code state.vscdb item without writing to the live DB."""
    db = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=0.8)
        row = db.execute(
            "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row else None
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if db is not None:
            db.close()


def _strip_bearer(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token or None


def _devin_org_details(raw: Any) -> tuple[Optional[str], Optional[str]]:
    """Normalize a Devin org slug/URL and retain internal org IDs when given."""
    if not isinstance(raw, str):
        return None, None
    value = raw.strip()
    if not value:
        return None, None
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc and parsed.netloc.lower().endswith("devin.ai"):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in ("org", "organizations"):
            value = f"{parts[0]}/{parts[1]}"
    value = value.strip("/")
    if value.startswith("organizations/"):
        return value, value.split("/", 1)[1]
    if value.startswith("org/"):
        return value, None
    if value.startswith(("org-", "org_")):
        return f"organizations/{value}", value
    return f"org/{value}", None


def _read_toml_string(path: Path, key: str) -> Optional[str]:
    """Read one simple string assignment from Devin's credentials.toml."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        lhs, value = line.split("=", 1)
        if lhs.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            return value[1:end] if end > 0 else None
        return value.split("#", 1)[0].strip() or None
    return None


def _windsurf_credentials_candidates(home: Path, *, include_native: bool) -> list[tuple[str, Path]]:
    """Return Devin/Windsurf CLI credentials.toml locations in probe order."""
    candidates: list[tuple[str, Path]] = []
    for env_name in ("DEVIN_CREDENTIALS_FILE", "WINDSURF_CREDENTIALS_FILE"):
        configured = os.environ.get(env_name, "").strip().strip('"')
        if configured:
            candidates.append(("configured", Path(os.path.expandvars(os.path.expanduser(configured)))))

    roots = [
        home / "AppData" / "Roaming",
        home / "Library" / "Application Support",
        home / ".config",
        home / ".local" / "share",
        home,
    ]
    if include_native:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            roots.append(Path(os.path.expandvars(os.path.expanduser(appdata))))

    seen: set[str] = set()
    for root in roots:
        for variant, app_dir in (
            ("devin", "Devin"),
            ("devin-next", "Devin - Next"),
            ("windsurf", "Windsurf"),
            ("windsurf-next", "Windsurf - Next"),
        ):
            path = root / app_dir / "credentials.toml"
            marker = str(path).lower()
            if marker not in seen:
                seen.add(marker)
                candidates.append((variant, path))
        for variant, app_dir in (("devin", "devin"), ("windsurf", "windsurf")):
            path = root / app_dir / "credentials.toml"
            marker = str(path).lower()
            if marker not in seen:
                seen.add(marker)
                candidates.append((variant, path))
    return candidates


def _windsurf_state_candidates(home: Path, *, include_native: bool) -> list[tuple[str, Path]]:
    """Return likely Windsurf desktop state DBs in probe order."""
    candidates: list[tuple[str, Path]] = []
    configured = os.environ.get("WINDSURF_STATE_DB", "").strip().strip('"')
    if configured:
        candidates.append(("configured", Path(os.path.expandvars(os.path.expanduser(configured)))))

    roots = [
        home / "AppData" / "Roaming",
        home / "Library" / "Application Support",
        home / ".config",
        home,
    ]
    # When the rest of the CLI is running from WSL, ``home`` is the Linux
    # home but Windsurf itself is still a native Windows application.
    if include_native:
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            roots.append(Path(os.path.expandvars(os.path.expanduser(appdata))))

    seen: set[str] = set()
    for root in roots:
        for variant, app_dir in (
            ("devin", "Devin"),
            ("devin-next", "Devin - Next"),
            ("windsurf", "Windsurf"),
            ("windsurf-next", "Windsurf - Next"),
        ):
            path = root / app_dir / "User" / "globalStorage" / "state.vscdb"
            marker = str(path).lower()
            if marker not in seen:
                seen.add(marker)
                candidates.append((variant, path))
    return candidates


def discover_windsurf(home: Optional[Path] = None) -> Credential:
    """Discover Windsurf's local API key or a cached plan snapshot.

    Windsurf stores the desktop login in VS Code's SQLite state database under
    ``windsurfAuthStatus``.  The cached plan item is retained as a read-only
    fallback when a running/updated client has not exposed the API key yet.
    """
    explicit_home = home is not None
    home = home or _home()

    # Devin's web billing page uses a short-lived Bearer/session token and an
    # organization path. This is separate from the public ``apk_user_*`` REST
    # key, which can create/list sessions but does not expose self-serve quota.
    organization_raw = next(
        (
            os.environ.get(name, "").strip()
            for name in ("DEVIN_ORGANIZATION", "DEVIN_ORG")
            if os.environ.get(name, "").strip()
        ),
        "",
    )
    organization, internal_org_id = _devin_org_details(organization_raw)
    for env_name in ("DEVIN_BEARER_TOKEN", "DEVIN_AUTHORIZATION"):
        token = _strip_bearer(os.environ.get(env_name))
        if token:
            return Credential(
                service="windsurf",
                present=True,
                kind="bearer",
                path=f"env:{env_name}",
                access_token=token,
                extra={
                    "quota_source": "devin-web",
                    "organization": organization,
                    "internal_organization_id": internal_org_id,
                },
            )

    env_names = ("WINDSURF_API_KEY", "DEVIN_WINDSURF_API_KEY")
    for env_name in env_names:
        key = os.environ.get(env_name, "").strip()
        if key:
            return Credential(
                service="windsurf",
                present=True,
                kind="api_key",
                path=f"env:{'|'.join(env_names)}",
                api_key=key,
                extra={"variant": "windsurf"},
            )

    candidates = _windsurf_credentials_candidates(home, include_native=not explicit_home)
    for variant, path in candidates:
        if not path.is_file():
            continue
        key = _read_toml_string(path, "windsurf_api_key") or _read_toml_string(path, "api_key")
        if not key:
            continue
        server = _read_toml_string(path, "api_server_url")
        if server and not server.lower().startswith("https://"):
            server = None
        return Credential(
            service="windsurf",
            present=True,
            kind="api_key",
            path=str(path),
            api_key=key,
            extra={
                "variant": variant,
                "api_server_url": server.rstrip("/") if server else None,
            },
        )

    cached_fallback: Optional[Credential] = None
    state_candidates = _windsurf_state_candidates(home, include_native=not explicit_home)
    for variant, path in state_candidates:
        if not path.is_file():
            continue
        auth_raw = _read_sqlite_item(path, "windsurfAuthStatus")
        cache_raw = _read_sqlite_item(path, "windsurf.settings.cachedPlanInfo")
        if cache_raw is None:
            cache_raw = _read_sqlite_item(path, "devin.settings.cachedPlanInfo")
        cache = _windsurf_decode_value(cache_raw)
        cache_payload = cache if isinstance(cache, dict) else None
        key = _windsurf_find_api_key(auth_raw)
        extra: dict[str, Any] = {
            "variant": variant,
            "state_db": str(path),
        }
        if cache_payload is not None:
            extra["cached_plan_info"] = cache_payload
        if key:
            return Credential(
                service="windsurf",
                present=True,
                kind="api_key",
                path=str(path),
                api_key=key,
                extra=extra,
            )
        if cache_payload is not None and cached_fallback is None:
            cached_fallback = Credential(
                service="windsurf",
                present=True,
                kind="cache",
                path=str(path),
                extra=extra,
            )

    if cached_fallback is not None:
        return cached_fallback
    # Do not silently send a public Devin REST key to the desktop quota
    # endpoint; the server accepts a different session token there.
    devin_api_key = os.environ.get("DEVIN_API_KEY", "").strip()
    if devin_api_key:
        return Credential(
            service="windsurf",
            present=False,
            kind="unsupported",
            path="env:DEVIN_API_KEY",
            extra={"reason": "REST key has no self-serve quota endpoint"},
        )
    paths = [str(path) for _, path in state_candidates if str(path)]
    return Credential(
        service="windsurf",
        present=False,
        kind="missing",
        path="env:WINDSURF_API_KEY|DEVIN_WINDSURF_API_KEY"
        + ("; " + "; ".join(paths) if paths else ""),
    )


def discover_credentials(home: Optional[Path] = None) -> dict[str, Credential]:
    """Return credentials for all six services."""
    provided_home = home
    home = home or _home()
    return {
        "codex": discover_codex(home),
        "grok": discover_grok(home),
        "kimi": discover_kimi(home),
        "opencode-go": discover_opencode_go(home),
        "ollama-pro": discover_ollama_pro(home),
        # Keep native APPDATA probing enabled for the normal no-argument call,
        # while explicit test/home roots remain hermetic.
        "windsurf": discover_windsurf() if provided_home is None else discover_windsurf(home),
    }
