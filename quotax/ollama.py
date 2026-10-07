"""Ollama Cloud: usage of the plan behind cloud models on ollama.com.

Ollama authenticates a device with an Ed25519 key (`~/.ollama/id_ed25519`) that `ollama signin`
links to an account. Requests to ollama.com carry `Authorization: <public key>:<signature>`, where
the signature covers `<METHOD>,<path>?ts=<unix seconds>` and the same `ts` goes in the query
(ollama/ollama: api/client.go, auth/auth.go). Quotax signs exactly like that, so it never needs a
password or an API key: an extra account is a new key that the user links in the browser.
"""

from __future__ import annotations

import base64
import contextlib
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import ed25519, http, jsonutil
from .formatting import capitalized
from .i18n import _
from .models import Account, AccountIdentity, ProviderIssue, ProviderSnapshot, UsageWindow

BASE_URL = "https://ollama.com"
CONNECT_URL = "https://ollama.com/connect"
KEY_FILE = Path(".ollama") / "id_ed25519"


@dataclass(frozen=True)
class OllamaKey:
    seed: bytes
    public: bytes

    @property
    def authorized_key(self) -> str:
        """The `ssh-ed25519 AAAA…` line Ollama shows and links to accounts."""
        return "ssh-ed25519 " + base64.b64encode(ed25519.public_blob(self.public)).decode()

    @property
    def encoded(self) -> str:
        """The key as ollama.com addresses it in URLs."""
        return base64.urlsafe_b64encode(self.authorized_key.encode()).decode().rstrip("=")

    def authorization(self, method: str, path: str, timestamp: str) -> str:
        signature = ed25519.sign(self.seed, f"{method},{path}?ts={timestamp}".encode())
        blob = base64.b64encode(ed25519.public_blob(self.public)).decode()
        return f"{blob}:{base64.b64encode(signature).decode()}"


def key_path(account: Account) -> Path:
    return (account.home or Path.home()) / KEY_FILE


def read_key(path: Path) -> OllamaKey | None:
    try:
        parsed = ed25519.read_private_key(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return OllamaKey(*parsed) if parsed else None


def create_key(path: Path) -> OllamaKey:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    seed = os.urandom(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(ed25519.write_private_key(seed))
    return OllamaKey(seed, ed25519.public_key(seed))


def connect_url(key: OllamaKey, device_name: str) -> str:
    # Same link `ollama signin` opens: ollama.com asks the user to sign in and links this key.
    return f"{CONNECT_URL}?name={urllib.parse.quote(device_name, safe='')}&key={key.encoded}"


def signed_request(key: OllamaKey, method: str, path: str) -> bytes:
    timestamp = str(int(time.time()))
    return http.request(
        method,
        f"{BASE_URL}{path}?ts={timestamp}",
        {
            "Authorization": key.authorization(method, path, timestamp),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )


def _fields(value) -> dict:
    """ollama.com answers with Go field names (`Email`, `Plan`); like Go's decoder, ignore case."""
    mapping = jsonutil.mapping(value) or {}
    return {str(key).lower(): item for key, item in mapping.items()}


def parse_usage(data: bytes | str) -> list[UsageWindow]:
    """`GET /api/balance` (ollama/ollama docs/api/balance.mdx).

    Current plans report the included monthly allowance in USD (`balance_usd` left of `allowance_usd`,
    resetting at `period.until`); legacy plans report `session` and `weekly` windows with
    `remaining_percent` (0–100) and `resets_at`.
    """
    root = _fields(jsonutil.obj(data))
    if jsonutil.mapping(root.get("included")) is None:
        raise ProviderIssue("invalidResponse")
    included = _fields(root["included"])
    windows = []
    for kind in ("session", "weekly"):
        window = _fields(included.get(kind))
        remaining = jsonutil.number(window.get("remaining_percent"))
        if remaining is not None and 0 <= remaining <= 100:
            windows.append(UsageWindow(kind, 100 - remaining, jsonutil.timestamp(window.get("resets_at"))))
    allowance = jsonutil.number(included.get("allowance_usd"))
    balance = jsonutil.number(included.get("balance_usd"))
    if allowance is not None and allowance > 0 and balance is not None:
        period = _fields(included.get("period"))
        windows.append(UsageWindow("monthly", (allowance - balance) * 100 / allowance, jsonutil.timestamp(period.get("until"))))
    return windows


def parse_identity(data: bytes | str | None) -> AccountIdentity | None:
    """None for a key that is not linked: ollama.com then answers with an empty user."""
    root = jsonutil.try_obj(data)
    if root is None:
        return None
    fields = _fields(root)
    email = jsonutil.string(fields.get("email"))
    name = jsonutil.string(fields.get("name"))
    if not email and not name:
        return None
    plan = jsonutil.string(fields.get("plan"))
    return AccountIdentity(email or name, capitalized(plan) if plan else None)


def whoami(key: OllamaKey) -> AccountIdentity | None:
    """The account this key is linked to, or None while it is not linked."""
    try:
        return parse_identity(signed_request(key, "POST", "/api/me"))
    except ProviderIssue as issue:
        if issue.kind == "http" and issue.status in (401, 403):
            return None
        raise


def disconnect(key: OllamaKey) -> None:
    """Unlinks the key from its account, like `ollama signout`."""
    with contextlib.suppress(ProviderIssue):
        signed_request(key, "DELETE", f"/api/user/keys/{key.encoded}")


class OllamaFetcher:
    def __init__(self, account: Account):
        self.account = account

    def fetch(self) -> ProviderSnapshot:
        key = read_key(key_path(self.account))
        if key is None:
            raise self._signed_out()
        try:
            windows = parse_usage(signed_request(key, "GET", "/api/balance"))
        except ProviderIssue as issue:
            if issue.kind == "http" and issue.status in (401, 403):
                raise self._signed_out() from None
            raise
        if not windows:
            raise ProviderIssue.no_quota(_("This account has no subscription limits."))
        # The plan name is a nicety: the meters stay even when this second call fails.
        try:
            identity = whoami(key)
        except ProviderIssue:
            identity = None
        return ProviderSnapshot(
            "ollama",
            (identity.plan if identity else None) or self.account.plan,
            windows,
            account=(identity.email if identity else None) or self.account.email,
        )

    def _signed_out(self) -> ProviderIssue:
        if self.account.home:
            return ProviderIssue.session_expired(_("Ollama Cloud session expired: relink the account."))
        return ProviderIssue.signed_out(_("Not signed in to Ollama Cloud. Run `ollama signin`."))
