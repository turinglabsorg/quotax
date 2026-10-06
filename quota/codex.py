from __future__ import annotations

import json
import math
import os
import select
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import http, jsonutil, process
from .formatting import capitalized
from .i18n import _
from .models import Account, AccountIdentity, ProviderIssue, ProviderSnapshot, UsageWindow

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNT_METHOD = "account/read"
RATE_LIMITS_METHOD = "account/rateLimits/read"
APP_SERVER_ARGUMENTS = [
    "-c", "approval_policy=never", "-c", "features.plugins=false", "-s", "read-only", "-a", "never", "app-server",
]

SESSION_MINUTES = 300
WEEKLY_MINUTES = 10_080
MONTHLY_MINUTES = 43_200


@dataclass(frozen=True)
class CodexAuth:
    kind: str  # "chatgpt" | "apiKey"
    access_token: str | None = None
    account_id: str | None = None


def parse_auth(data: bytes | str | None) -> CodexAuth | None:
    root = jsonutil.try_obj(data)
    if root is None:
        return None
    tokens = jsonutil.mapping(root.get("tokens"))
    if tokens and (access_token := jsonutil.string(tokens.get("access_token"))):
        return CodexAuth("chatgpt", access_token, jsonutil.string(tokens.get("account_id")))
    return CodexAuth("apiKey") if jsonutil.string(root.get("OPENAI_API_KEY")) else None


def parse_identity(data: bytes | str | None) -> AccountIdentity | None:
    root = jsonutil.try_obj(data)
    account = jsonutil.mapping(root.get("account")) if root else None
    if account is None:
        return None
    return AccountIdentity(email=jsonutil.string(account.get("email")), plan=_plan_label(account.get("planType")))


def parse_rpc_usage(data: bytes | str) -> tuple[str | None, list[UsageWindow]]:
    root = jsonutil.obj(data)
    limits = jsonutil.mapping(root.get("rateLimits"))
    if limits is None:
        raise ProviderIssue("invalidResponse")
    windows = []
    for key, fallback in (("primary", "session"), ("secondary", "weekly")):
        raw = jsonutil.mapping(limits.get(key))
        used = jsonutil.number(raw.get("usedPercent")) if raw else None
        if used is None:
            continue
        minutes = jsonutil.number(raw.get("windowDurationMins"))
        windows.append(_window(used, jsonutil.timestamp(raw.get("resetsAt")), _rounded_minutes(minutes), fallback))
    return _plan_label(limits.get("planType")), _sorted(windows)


def parse_usage(data: bytes | str, now: float | None = None) -> tuple[str | None, list[UsageWindow]]:
    now = time.time() if now is None else now
    root = jsonutil.obj(data)
    plan_type = jsonutil.string(root.get("plan_type"))
    if not plan_type:
        raise ProviderIssue("invalidResponse")
    rate_limit = jsonutil.mapping(root.get("rate_limit")) or {}
    windows = []
    for key, fallback in (("primary_window", "session"), ("secondary_window", "weekly")):
        raw = jsonutil.mapping(rate_limit.get(key))
        used = jsonutil.number(raw.get("used_percent")) if raw else None
        if used is None:
            continue
        resets_at = jsonutil.timestamp(raw.get("reset_at"))
        if resets_at is None and (after := jsonutil.number(raw.get("reset_after_seconds"))) is not None:
            resets_at = now + after
        seconds = jsonutil.number(raw.get("limit_window_seconds"))
        minutes = math.ceil(seconds / 60) if seconds is not None else None
        windows.append(_window(used, resets_at, minutes, fallback))
    return plan_name(plan_type), _sorted(windows)


def _plan_label(value) -> str | None:
    plan = jsonutil.string(value)
    if not plan or plan.lower() == "unknown":
        return None
    return plan_name(plan)


def plan_name(plan: str) -> str:
    """ChatGPT plan identifiers as people know them: `self_serve_business_prolite` is a Business seat."""
    if plan.lower().startswith("self_serve_business"):
        return "Business"
    return capitalized(plan.replace("_", " "))


def _rounded_minutes(value: float | None) -> int | None:
    return None if value is None else int(value + 0.5)


def _window(used: float, resets_at: float | None, minutes: int | None, fallback: str) -> UsageWindow:
    if not minutes or minutes <= 0:
        return UsageWindow(fallback, used, resets_at)
    if abs(minutes - SESSION_MINUTES) <= 1:
        return UsageWindow("session", used, resets_at)
    if abs(minutes - WEEKLY_MINUTES) <= 1:
        return UsageWindow("weekly", used, resets_at)
    if abs(minutes - MONTHLY_MINUTES) <= 1_440:
        return UsageWindow("monthly", used, resets_at)
    return UsageWindow("custom", used, resets_at, minutes=minutes)


def _sorted(windows: list[UsageWindow]) -> list[UsageWindow]:
    order = {"session": SESSION_MINUTES, "weekly": WEEKLY_MINUTES, "weeklyModel": WEEKLY_MINUTES, "monthly": MONTHLY_MINUTES}
    return sorted(windows, key=lambda window: window.minutes if window.kind == "custom" else order[window.kind])


@dataclass
class RPCResponse:
    results: dict[str, bytes] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def rpc_call(executable: Path, home: Path | None, methods: list[str], timeout: float = 30) -> RPCResponse:
    """Talks to `codex app-server` over JSON-RPC, so Codex refreshes its own session."""
    env = {"CODEX_HOME": str(home)} if home else {}
    child = process.spawn_pipes(executable, APP_SERVER_ARGUMENTS, env=env)
    response = RPCResponse()
    deadline = time.monotonic() + timeout

    def send(message: dict):
        try:
            child.stdin.write(json.dumps(message).encode() + b"\n")
            child.stdin.flush()
        except OSError:
            raise ProviderIssue("invalidResponse") from None

    try:
        send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"clientInfo": {"name": "quota", "version": "1.0.0"}}})
        buffer = b""
        fd = child.stdout.fileno()
        while len(response.results) + len(response.errors) < len(methods):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderIssue("network")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(fd, 65_536)
            if not chunk:
                raise ProviderIssue("invalidResponse")
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                message_id = jsonutil.number(message.get("id")) if isinstance(message, dict) else None
                if message_id is None:
                    continue
                if message_id == 0:
                    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                    for index, method in enumerate(methods):
                        send({"jsonrpc": "2.0", "id": index + 1, "method": method, "params": {}})
                    continue
                index = int(message_id) - 1
                if not 0 <= index < len(methods):
                    continue
                if error := jsonutil.mapping(message.get("error")):
                    response.errors[methods[index]] = jsonutil.string(error.get("message")) or "error"
                elif "result" in message:
                    response.results[methods[index]] = json.dumps(message["result"]).encode()
        return response
    finally:
        process.terminate(child)


def issue_for_error(message: str) -> ProviderIssue:
    lowered = message.lower()
    if any(text in lowered for text in ("not logged in", "no account", "api key")):
        return ProviderIssue.signed_out(_("This Codex account is not signed in with ChatGPT."))
    if any(text in lowered for text in ("auth", "login", "sign in", "token", "401", "unauthorized")):
        return ProviderIssue.session_expired(_("Codex session expired: relink the account."))
    return ProviderIssue("invalidResponse")


class CodexFetcher:
    def __init__(self, account: Account):
        self.account = account

    def fetch(self) -> ProviderSnapshot:
        executable = process.locate("codex")
        if executable is None:
            if self.account.home is not None:
                raise ProviderIssue.signed_out(_("`codex` CLI not found."))
            return self._fetch_from_backend()
        try:
            response = rpc_call(executable, self.account.home, [ACCOUNT_METHOD, RATE_LIMITS_METHOD])
        except (ProviderIssue, OSError):
            if self.account.home is not None:
                raise ProviderIssue("network") from None
            return self._fetch_from_backend()
        account_data = response.results.get(ACCOUNT_METHOD)
        identity = parse_identity(account_data)
        if account_data is not None and identity is None:
            raise ProviderIssue.signed_out(_("This Codex account is not signed in with ChatGPT."))
        if message := response.errors.get(RATE_LIMITS_METHOD):
            raise issue_for_error(message)
        data = response.results.get(RATE_LIMITS_METHOD)
        if data is None:
            raise ProviderIssue("invalidResponse")
        plan, windows = parse_rpc_usage(data)
        return self._snapshot(
            plan or (identity.plan if identity else None),
            (identity.email if identity else None) or self.account.email,
            windows,
        )

    def _fetch_from_backend(self) -> ProviderSnapshot:
        home = Path(os.environ["CODEX_HOME"]) if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
        try:
            auth = parse_auth((home / "auth.json").read_bytes())
        except OSError:
            auth = None
        if auth is None:
            raise ProviderIssue.signed_out(_("Codex is not signed in with ChatGPT. Run `codex login`."))
        if auth.kind != "chatgpt":
            raise ProviderIssue.no_quota(_("Codex uses an API key: no subscription limits."))
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "User-Agent": "codex-cli",
            "OpenAI-Beta": "codex-1",
            "originator": "Codex Desktop",
        }
        if auth.account_id:
            headers["ChatGPT-Account-Id"] = auth.account_id
        try:
            data = http.get(USAGE_URL, headers)
        except ProviderIssue as issue:
            if issue.kind == "http" and issue.status in (401, 403):
                raise ProviderIssue.session_expired(_("Session expired. Open Codex to renew it.")) from None
            raise
        plan, windows = parse_usage(data)
        return self._snapshot(plan, self.account.email, windows)

    def _snapshot(self, plan: str | None, email: str | None, windows: list[UsageWindow]) -> ProviderSnapshot:
        if not windows:
            raise ProviderIssue.no_quota(_("This account has no subscription limits."))
        return ProviderSnapshot("codex", plan, windows, account=email)
