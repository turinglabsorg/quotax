from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import http, jsonutil, paths, process
from .formatting import capitalized
from .i18n import _
from .models import Account, AccountIdentity, ProviderIssue, ProviderSnapshot, UsageWindow

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CREDENTIALS_FILE = ".credentials.json"
MODEL_WEEKLY_KEYS = ("opus", "sonnet", "haiku", "fable")


@dataclass
class ClaudeCredentials:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    plan: str | None = None

    def is_expiring(self, now: float) -> bool:
        return self.expires_at is not None and self.expires_at - now < 5 * 60


def parse_credentials(data: bytes | str | None) -> ClaudeCredentials | None:
    root = jsonutil.try_obj(data)
    oauth = jsonutil.mapping(root.get("claudeAiOauth")) if root else None
    token = jsonutil.string(oauth.get("accessToken")) if oauth else None
    if not token:
        return None
    expires_at = jsonutil.number(oauth.get("expiresAt"))
    return ClaudeCredentials(
        access_token=token,
        refresh_token=jsonutil.string(oauth.get("refreshToken")),
        expires_at=expires_at / 1_000 if expires_at is not None else None,
        plan=plan_label(jsonutil.string(oauth.get("subscriptionType")), jsonutil.string(oauth.get("rateLimitTier"))),
    )


def plan_label(subscription_type: str | None, tier: str | None) -> str | None:
    if not subscription_type:
        return None
    kind = subscription_type.lower()
    if kind == "max" and tier:
        if "20x" in tier.lower():
            return "Max 20x"
        if "5x" in tier.lower():
            return "Max 5x"
    return capitalized(kind)


def identity_from_status(data: bytes | str) -> AccountIdentity | None:
    root = jsonutil.try_obj(data)
    if not root or root.get("loggedIn") is not True:
        return None
    return AccountIdentity(
        email=jsonutil.string(root.get("email")),
        plan=plan_label(jsonutil.string(root.get("subscriptionType")), None),
    )


def applying_refresh(response: bytes | str, stored: bytes | str, now: float | None = None) -> bytes | None:
    root = jsonutil.try_obj(stored)
    oauth = jsonutil.mapping(root.get("claudeAiOauth")) if root else None
    refreshed = jsonutil.try_obj(response)
    access_token = jsonutil.string(refreshed.get("access_token")) if refreshed else None
    if oauth is None or not access_token:
        return None
    now = time.time() if now is None else now
    oauth["accessToken"] = access_token
    if (expires_in := jsonutil.number(refreshed.get("expires_in"))) is not None:
        oauth["expiresAt"] = int((now + expires_in) * 1_000)
    if refresh_token := jsonutil.string(refreshed.get("refresh_token")):
        oauth["refreshToken"] = refresh_token
    if scope := jsonutil.string(refreshed.get("scope")):
        oauth["scopes"] = scope.split(" ")
    root["claudeAiOauth"] = oauth
    return json.dumps(root).encode()


def parse_windows(data: bytes | str) -> list[UsageWindow]:
    root = jsonutil.obj(data)
    windows = [
        window
        for window in (_window(root.get("five_hour"), "session"), _window(root.get("seven_day"), "weekly"))
        if window
    ]

    model_windows: list[UsageWindow] = []

    def add_model(raw_name: str, used_percent: float, resets_at: float | None):
        name = capitalized(raw_name)
        exists = any(window.model.casefold() == name.casefold() for window in model_windows)
        if name and not exists:
            model_windows.append(UsageWindow("weeklyModel", used_percent, resets_at, model=name))

    limits = root.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict) or jsonutil.string(limit.get("kind")) != "weekly_scoped":
            continue
        model = jsonutil.mapping((jsonutil.mapping(limit.get("scope")) or {}).get("model")) or {}
        name = jsonutil.string(model.get("display_name"))
        percent = jsonutil.number(limit.get("percent"))
        if name and percent is not None:
            add_model(name, percent, jsonutil.timestamp(limit.get("resets_at")))

    for key in ("fable_weekly", "fable_seven_day"):
        if fable := _window(root.get(key), "weeklyModel", "Fable"):
            add_model("Fable", fable.used_percent, fable.resets_at)

    for model in MODEL_WEEKLY_KEYS:
        if scoped := _window(root.get(f"seven_day_{model}"), "weeklyModel", model):
            add_model(model, scoped.used_percent, scoped.resets_at)

    return windows + model_windows


def _window(value, kind: str, model: str | None = None) -> UsageWindow | None:
    raw = jsonutil.mapping(value)
    if raw is None:
        return None
    used = jsonutil.number(raw.get("utilization"))
    if used is None:
        used = jsonutil.number(raw.get("used_percentage"))
    if used is None:
        return None
    return UsageWindow(kind, used, jsonutil.timestamp(raw.get("resets_at")), model=model)


def shared_credentials_file() -> Path:
    return Path.home() / ".claude" / CREDENTIALS_FILE


def read_file(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.quota-tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


class ClaudeFetcher:
    def __init__(self, account: Account):
        self.account = account

    def fetch(self) -> ProviderSnapshot:
        home = self.account.home
        return self._fetch_managed(home) if home else self._fetch_shared_login()

    def _fetch_shared_login(self) -> ProviderSnapshot:
        credentials = SharedClaudeLogin.read()
        if credentials is None:
            raise ProviderIssue.signed_out(_("Not signed in to Claude Code. Run `claude` in a terminal."))
        expired = ProviderIssue.session_expired(
            _("The Claude Code session expired and could not be renewed automatically. Run `claude` in a terminal.")
        )
        if credentials.is_expiring(time.time()):
            credentials = SharedClaudeLogin.renew(replacing=credentials.access_token)
            if credentials is None:
                raise expired
        try:
            return self._snapshot(credentials)
        except ProviderIssue as issue:
            if issue.kind != "http" or issue.status not in (401, 403):
                raise
        renewed = SharedClaudeLogin.renew(replacing=credentials.access_token)
        if renewed is None:
            raise expired
        try:
            return self._snapshot(renewed)
        except ProviderIssue as issue:
            if issue.kind == "http" and issue.status in (401, 403):
                raise expired from None
            raise

    def _fetch_managed(self, home: Path) -> ProviderSnapshot:
        file = home / CREDENTIALS_FILE
        stored = read_file(file)
        credentials = parse_credentials(stored)
        if credentials is None:
            raise ProviderIssue.signed_out(_("Credentials not found: unlink and relink the account."))
        if credentials.is_expiring(time.time()) and (refreshed := self._refresh(stored, file)):
            stored, credentials = refreshed
        try:
            return self._snapshot(credentials)
        except ProviderIssue as issue:
            if issue.kind != "http" or issue.status not in (401, 403):
                raise
        refreshed = self._refresh(stored, file)
        if refreshed is None:
            raise ProviderIssue.session_expired(_("Session expired: unlink and relink the account."))
        return self._snapshot(refreshed[1])

    # Accounts signed in by Quotax are not used by any running CLI, so Quotax can rotate their tokens itself.
    def _refresh(self, stored: bytes, file: Path) -> tuple[bytes, ClaudeCredentials] | None:
        current = parse_credentials(stored)
        if current is None or not current.refresh_token:
            return None
        try:
            response = http.post_form(
                TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": current.refresh_token, "client_id": CLIENT_ID}
            )
        except ProviderIssue:
            return None
        updated = applying_refresh(response, stored)
        credentials = parse_credentials(updated)
        if updated is None or credentials is None:
            return None
        with contextlib.suppress(OSError):
            write_private(file, updated)
        return updated, credentials

    def _snapshot(self, credentials: ClaudeCredentials) -> ProviderSnapshot:
        data = http.get(
            USAGE_URL,
            {
                "Authorization": f"Bearer {credentials.access_token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )
        windows = parse_windows(data)
        if not windows:
            raise ProviderIssue.no_quota(_("This account has no subscription limits."))
        return ProviderSnapshot("claude", credentials.plan or self.account.plan, windows, account=self.account.email)


class SharedClaudeLogin:
    """The shared Claude Code login is owned by the CLI.

    Quotax never refreshes it itself, because the refresh token rotates and a running Claude Code
    would be signed out. Instead it briefly starts `claude`, which renews its own token on disk,
    and waits for the new token to appear. One attempt at a time across Quotax processes, with a
    10-minute cooldown after a failure.
    """

    COOLDOWN = 10 * 60

    @staticmethod
    def read() -> ClaudeCredentials | None:
        return parse_credentials(read_file(shared_credentials_file()))

    @classmethod
    def renew(cls, replacing: str) -> ClaudeCredentials | None:
        state = paths.state_dir()
        state.mkdir(parents=True, exist_ok=True)
        with open(state / "claude-renewal.lock", "a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another Quotax process is renewing: wait for it and use what it produced.
                fcntl.flock(lock, fcntl.LOCK_EX)
                return cls._renewed(replacing)
            failure_file = state / "claude-renewal-failed"
            with contextlib.suppress(OSError):
                if time.time() - failure_file.stat().st_mtime < cls.COOLDOWN:
                    return None
            renewed = cls._run_cli(replacing)
            if renewed is None:
                failure_file.touch()
            else:
                failure_file.unlink(missing_ok=True)
            return renewed

    @classmethod
    def _run_cli(cls, replacing: str) -> ClaudeCredentials | None:
        executable = process.locate("claude")
        if executable is None:
            return None
        with process.background_tty(executable, [], env={"CLAUDE_CONFIG_DIR": None}):
            for _attempt in range(30):
                time.sleep(1)
                if renewed := cls._renewed(replacing):
                    return renewed
        return None

    @classmethod
    def _renewed(cls, replacing: str) -> ClaudeCredentials | None:
        credentials = cls.read()
        if credentials and credentials.access_token != replacing and not credentials.is_expiring(time.time()):
            return credentials
        return None
