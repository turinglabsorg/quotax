"""Muse Code usage, read from the session logs the `muse` CLI stores on this computer.

Muse has no subscription-quota endpoint: the local session logs are the only source.
Each model call is recorded as a `model_completed` event whose `usage` reports the step's
input, output, cached and reasoning tokens (input already includes the cached share, and
reasoning is part of output). Quotax only reads these logs; nothing is sent anywhere.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .i18n import _
from .models import Account, ProviderIssue, ProviderSnapshot, TokenUsage

SESSIONS_DIR = "sessions"
LOG_NAME = "session.jsonl"


def sessions_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "muse" / SESSIONS_DIR


def _local_midnight(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def _shard_dates(start: float) -> set[str]:
    """Date-shard names (YYYY/MM/DD) that can hold records at or after `start`."""
    first = date.fromtimestamp(start - 86_400)  # a session may start the day before and run past midnight
    last = date.fromtimestamp(time.time())
    names = set()
    current = first
    while current <= last:
        names.add(f"{current.year:04d}/{current.month:02d}/{current.day:02d}")
        current += timedelta(days=1)
    return names


def session_logs(root: Path, start: float) -> list[Path]:
    shards = _shard_dates(start)
    logs: list[Path] = []
    try:
        date_dirs = sorted(root.glob("*/*/*"))
    except OSError:
        return logs
    for date_dir in date_dirs:
        relative = "/".join(date_dir.parts[-3:])
        if relative not in shards or not date_dir.is_dir():
            continue
        for session_dir in sorted(date_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            main_log = session_dir / LOG_NAME
            if main_log.is_file():
                logs.append(main_log)
            subagents = session_dir / "subagent"
            if subagents.is_dir():
                logs.extend(sorted(subagents.glob(f"*/{LOG_NAME}")))
    return logs


@dataclass
class _Bucket:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    steps: int = 0
    sessions: set[str] = field(default_factory=set)

    def add(self, usage: dict, session_id: str) -> None:
        self.input_tokens += _count(usage.get("input_tokens"))
        self.output_tokens += _count(usage.get("output_tokens"))
        cached = usage.get("cached_tokens")
        if cached is None:
            cached = usage.get("cache_read_tokens")
        self.cached_tokens += _count(cached)
        self.cache_write_tokens += _count(usage.get("cache_write_tokens"))
        self.reasoning_tokens += _count(usage.get("reasoning_tokens"))
        self.steps += 1
        if session_id:
            self.sessions.add(session_id)

    def to_usage(self, period: str, resets_at: float | None) -> TokenUsage:
        return TokenUsage(
            period=period,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
            sessions=len(self.sessions),
            steps=self.steps,
            resets_at=resets_at,
        )


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _records(path: Path):
    """Yields (recorded_at seconds, session id, model, usage) for each completed model call."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                envelope = json.loads(line)
            except ValueError:
                continue
            if not isinstance(envelope, dict):
                continue
            payload = envelope.get("payload")
            event = payload.get("event") if isinstance(payload, dict) else None
            if not isinstance(event, dict) or event.get("kind") != "model_completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            recorded = envelope.get("recorded_at")
            if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
                continue
            # Subagent logs live in <session>/subagent/<child>/: they belong to the parent session.
            session_dir = path.parent.parent.parent if path.parent.parent.name == "subagent" else path.parent
            model = event.get("model")
            yield recorded / 1_000_000, session_dir.name, model if isinstance(model, str) else None, usage


def aggregate(root: Path, now: float | None = None) -> tuple[list[TokenUsage], str | None]:
    """Today and last-7-days token usage from the logs under `root`, plus the most used model."""
    now = time.time() if now is None else now
    today_start = _local_midnight(now)
    week_start = today_start - 6 * 86_400
    today, week = _Bucket(), _Bucket()
    models: Counter[str] = Counter()
    for log in session_logs(root, week_start):
        for recorded_at, session_id, model, usage in _records(log):
            if recorded_at < week_start or recorded_at > now:
                continue
            week.add(usage, session_id)
            if recorded_at >= today_start:
                today.add(usage, session_id)
            if model:
                models[model] += 1
    usage = [
        today.to_usage("today", today_start + 86_400),
        week.to_usage("last7Days", None),
    ]
    return usage, (models.most_common(1)[0][0] if models else None)


def detect_model(root: Path | None = None) -> str | None:
    """The model of recent Muse sessions, or None when there are no usable logs."""
    root = sessions_root() if root is None else root
    usage, model = aggregate(root)
    if not any(period.steps for period in usage):
        return None
    return model


class MuseCodeFetcher:
    def __init__(self, account: Account):
        self.account = account

    def fetch(self) -> ProviderSnapshot:
        usage, model = aggregate(sessions_root())
        if not any(period.steps for period in usage):
            raise ProviderIssue.signed_out(_("No Muse usage found on this computer."))
        return ProviderSnapshot("musecode", model or self.account.plan, [], account=self.account.email, token_usage=usage)
