"""Shared quota record model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SERVICE_NAMES = ("codex", "grok", "kimi", "opencode-go", "ollama-pro", "windsurf")


@dataclass
class QuotaRecord:
    """Normalized per-service quota snapshot for layout + CLI."""

    name: str
    status: str  # ok | unavailable | error
    used_percent: Optional[float] = None  # 0-100 when known
    remaining_percent: Optional[float] = None
    used: Optional[float] = None  # absolute used when known
    remaining: Optional[float] = None
    limit: Optional[float] = None
    reset_at: Optional[str] = None  # ISO-8601 or human short string
    detail: str = ""
    windows: list[dict[str, Any]] = field(default_factory=list)
    # Change in remaining percentage points since the previous successful
    # refresh. This is presentation metadata, not provider API data.
    delta_percent: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def unavailable(name: str, detail: str) -> QuotaRecord:
    return QuotaRecord(name=name, status="unavailable", detail=detail)


def error(name: str, detail: str) -> QuotaRecord:
    return QuotaRecord(name=name, status="error", detail=detail)
