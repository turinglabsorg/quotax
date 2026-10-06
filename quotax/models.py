from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .formatting import duration, rounded
from .i18n import _

PROVIDERS = ("claude", "codex", "grok", "ollama")
DISPLAY_NAMES = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "ollama": "Ollama Cloud"}
CLI_NAMES = {"claude": "Claude Code", "codex": "Codex", "grok": "Grok", "ollama": "Ollama"}


@dataclass
class Account:
    provider: str
    source: str  # "cli" reuses the CLI's own login, "managed" was signed in by Quotax
    email: str | None = None
    plan: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()).upper())

    @property
    def home(self) -> Path | None:
        return paths.account_home(self.provider, self.id) if self.source == "managed" else None

    @property
    def source_label(self) -> str:
        if self.source == "cli":
            return _("{name} login", name=CLI_NAMES[self.provider])
        return _("linked by Quotax")

    def to_json(self) -> dict:
        return {"id": self.id, "provider": self.provider, "source": self.source, "email": self.email, "plan": self.plan}

    @classmethod
    def from_json(cls, raw: dict) -> Account | None:
        if raw.get("provider") not in PROVIDERS or raw.get("source") not in ("cli", "managed"):
            return None
        try:
            account_id = str(uuid.UUID(str(raw.get("id")))).upper()
        except ValueError:
            return None
        plan = raw.get("plan")
        return cls(
            id=account_id,
            provider=raw["provider"],
            source=raw["source"],
            email=raw.get("email"),
            plan=None if isinstance(plan, str) and plan.lower() == "unknown" else plan,
        )


@dataclass
class AccountIdentity:
    email: str | None
    plan: str | None

    def to_json(self) -> dict:
        return {"email": self.email, "plan": self.plan}


@dataclass
class UsageWindow:
    kind: str  # session | weekly | weeklyModel | monthly | custom
    used_percent: float
    resets_at: float | None = None  # seconds since the epoch
    model: str | None = None
    minutes: int | None = None

    def __post_init__(self):
        self.used_percent = min(100.0, max(0.0, float(self.used_percent)))

    @property
    def label(self) -> str:
        match self.kind:
            case "session":
                return _("5-hour session")
            case "weekly":
                return _("Weekly")
            case "weeklyModel":
                return _("Weekly · {model}", model=self.model)
            case "monthly":
                return _("Monthly")
            case _:
                return _("{duration} window", duration=duration(self.minutes or 0))

    def has_reset(self, now: float) -> bool:
        return self.resets_at is not None and self.resets_at <= now

    def used_at(self, now: float) -> int:
        return 0 if self.has_reset(now) else rounded(self.used_percent)

    def remaining_at(self, now: float) -> int:
        return 100 - self.used_at(now)

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "model": self.model,
            "minutes": self.minutes,
            "label": self.label,
            "usedPercent": self.used_percent,
            "resetsAt": self.resets_at,
        }


@dataclass
class ProviderSnapshot:
    provider: str
    plan: str | None
    windows: list[UsageWindow]
    account: str | None = None
    fetched_at: float = field(default_factory=time.time)

    def tightest_window(self, now: float) -> UsageWindow | None:
        """Model-scoped limits do not block the whole account, so they only count when nothing else is reported."""
        account_wide = [window for window in self.windows if window.kind != "weeklyModel"]
        candidates = account_wide or self.windows
        return min(candidates, key=lambda window: window.remaining_at(now), default=None)

    def to_json(self) -> dict:
        return {
            "provider": self.provider,
            "plan": self.plan,
            "account": self.account,
            "fetchedAt": self.fetched_at,
            "windows": [window.to_json() for window in self.windows],
        }


class ProviderIssue(Exception):
    """A user-facing reason why usage could not be read."""

    def __init__(self, kind: str, message: str | None = None, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self._message = message

    @classmethod
    def signed_out(cls, message: str) -> ProviderIssue:
        return cls("signedOut", message)

    @classmethod
    def session_expired(cls, message: str) -> ProviderIssue:
        return cls("sessionExpired", message)

    @classmethod
    def no_quota(cls, message: str) -> ProviderIssue:
        return cls("noQuota", message)

    @property
    def message(self) -> str:
        if self._message:
            return self._message
        match self.kind:
            case "rateLimited":
                return _("Too many requests: retrying on the next refresh.")
            case "http":
                return _("Server error (HTTP {status}).", status=self.status)
            case "network":
                return _("Network unreachable.")
            case _:
                return _("Unrecognized response.")

    @property
    def keeps_last_snapshot(self) -> bool:
        return self.kind not in ("signedOut", "noQuota")

    @property
    def is_auth_failure(self) -> bool:
        return self.kind in ("signedOut", "sessionExpired")

    def __eq__(self, other):
        return isinstance(other, ProviderIssue) and (self.kind, self.status) == (other.kind, other.status)

    def __hash__(self):
        return hash((self.kind, self.status))

    def to_json(self) -> dict:
        return {"kind": self.kind, "message": self.message, "keepsLastSnapshot": self.keeps_last_snapshot}


def usage_level(remaining_percent: int) -> str:
    if remaining_percent <= 5:
        return "critical"
    if remaining_percent <= 20:
        return "warning"
    return "normal"
