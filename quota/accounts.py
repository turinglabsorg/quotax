"""Linked accounts (stored without secrets) and the link, sign-in and unlink flows."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

from . import claude, codex, grok, paths, process, sample
from .i18n import _
from .models import DISPLAY_NAMES, PROVIDERS, Account, AccountIdentity, ProviderSnapshot

LOGIN_TIMEOUT = 600


class AccountStore:
    @staticmethod
    def load() -> list[Account]:
        try:
            raw = json.loads(paths.accounts_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = raw.get("accounts") if isinstance(raw, dict) else None
        accounts = [Account.from_json(entry) for entry in entries or [] if isinstance(entry, dict)]
        return [account for account in accounts if account]

    @staticmethod
    @contextlib.contextmanager
    def editing() -> Iterator[list[Account]]:
        """Read-modify-write under a lock, so the extension and a terminal can both change accounts."""
        directory = paths.config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "accounts.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            accounts = AccountStore.load()
            before = [account.to_json() for account in accounts]
            yield accounts
            after = [account.to_json() for account in accounts]
            if after != before:
                file = paths.accounts_file()
                temporary = file.with_name(f".{file.name}.tmp")
                temporary.write_text(json.dumps({"version": 1, "accounts": after}, indent=2) + "\n", encoding="utf-8")
                os.replace(temporary, file)

    @staticmethod
    def add(account: Account) -> None:
        with AccountStore.editing() as accounts:
            accounts.append(account)
            # Grouped by provider, in insertion order within each provider.
            accounts.sort(key=lambda item: PROVIDERS.index(item.provider))

    @staticmethod
    def remove(account_id: str) -> Account | None:
        with AccountStore.editing() as accounts:
            removed = next((account for account in accounts if account.id == account_id), None)
            accounts[:] = [account for account in accounts if account.id != account_id]
            return removed

    @staticmethod
    def update_identity(account_id: str, email: str | None, plan: str | None) -> None:
        with AccountStore.editing() as accounts:
            for account in accounts:
                if account.id == account_id:
                    account.email = email or account.email
                    account.plan = plan or account.plan

    @staticmethod
    def has_shared_login(provider: str) -> bool:
        return any(account.provider == provider and account.source == "cli" for account in AccountStore.load())

    @staticmethod
    def has_managed_account(provider: str, email: str) -> bool:
        return any(
            account.provider == provider
            and account.source == "managed"
            and (account.email or "").casefold() == email.casefold()
            for account in AccountStore.load()
        )


def fetch(account: Account) -> ProviderSnapshot:
    if sample.enabled():
        return sample.snapshot(account)
    match account.provider:
        case "claude":
            return claude.ClaudeFetcher(account).fetch()
        case "codex":
            return codex.CodexFetcher(account).fetch()
        case _:
            return grok.GrokFetcher(account).fetch()


class LinkError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def detect_cli_login(provider: str) -> AccountIdentity | None:
    if sample.enabled():
        return sample.identity(provider)
    try:
        match provider:
            case "claude":
                executable = process.locate("claude")
                if executable is None:
                    return None
                status = process.run(executable, ["auth", "status", "--json"], env={"CLAUDE_CONFIG_DIR": None}, timeout=30)
                return claude.identity_from_status(status.stdout)
            case "codex":
                executable = process.locate("codex")
                if executable is None:
                    return None
                response = codex.rpc_call(executable, None, [codex.ACCOUNT_METHOD])
                return codex.parse_identity(response.results.get(codex.ACCOUNT_METHOD))
            case _:
                credentials = grok.read_credentials(grok.home_for(Account("grok", "cli")))
                return AccountIdentity(credentials.email, None) if credentials else None
    except Exception:  # noqa: BLE001 - a CLI that misbehaves simply has no usable login
        return None


def link_shared_login(provider: str, identity: AccountIdentity | None) -> Account:
    with AccountStore.editing() as accounts:
        existing = next((item for item in accounts if item.provider == provider and item.source == "cli"), None)
        if existing:
            return existing
        account = Account(provider, "cli", identity.email if identity else None, identity.plan if identity else None)
        accounts.append(account)
        accounts.sort(key=lambda item: PROVIDERS.index(item.provider))
        return account


def sign_in(provider: str, account_id: str, on_login_url: Callable[[str], None]) -> AccountIdentity:
    """Runs the official CLI sign-in in an isolated home; the user signs in on the provider's page."""
    executable = process.locate(provider)
    if executable is None:
        raise LinkError(_("`{name}` CLI not found: install it and try again.", name=provider))
    home = paths.account_home(provider, account_id)
    paths.accounts_root().mkdir(parents=True, exist_ok=True, mode=0o700)
    home.mkdir(parents=True, mode=0o700)

    def on_output(text: str):
        if url := login_url(text):
            on_login_url(url)

    try:
        match provider:
            case "claude":
                return _sign_in_claude(executable, home, on_output)
            case "codex":
                return _sign_in_codex(executable, home, on_output)
            case _:
                return _sign_in_grok(executable, home, on_output)
    except BaseException as error:
        shutil.rmtree(home, ignore_errors=True)
        if isinstance(error, (LinkError, KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, process.TimedOut):
            raise LinkError(_("Timed out: sign-in was not completed.")) from None
        raise LinkError(_("{name} sign-in was not completed.", name=DISPLAY_NAMES[provider])) from None


def unlink(account: Account) -> None:
    home = account.home
    if home is None or not _is_inside(home, paths.accounts_root()):
        return
    if account.provider in ("codex", "grok") and (executable := process.locate(account.provider)):
        variable = "CODEX_HOME" if account.provider == "codex" else "GROK_HOME"
        with contextlib.suppress(OSError, process.TimedOut):
            process.run(executable, ["logout"], env={variable: str(home)}, timeout=30)
    shutil.rmtree(home, ignore_errors=True)


def _sign_in_codex(executable: Path, home: Path, on_output) -> AccountIdentity:
    login = process.run(executable, ["login"], env={"CODEX_HOME": str(home)}, timeout=LOGIN_TIMEOUT, on_output=on_output)
    if login.status != 0:
        raise LinkError(_("{name} sign-in was not completed.", name=DISPLAY_NAMES["codex"]))
    response = codex.rpc_call(executable, home, [codex.ACCOUNT_METHOD])
    identity = codex.parse_identity(response.results.get(codex.ACCOUNT_METHOD))
    if identity is None:
        raise LinkError(_("Codex did not return the linked account."))
    return identity


def _sign_in_grok(executable: Path, home: Path, on_output) -> AccountIdentity:
    # Grok's login expects a terminal.
    login = process.run(
        executable, ["login", "--oauth"], env={"GROK_HOME": str(home)}, timeout=LOGIN_TIMEOUT, tty=True, on_output=on_output
    )
    credentials = grok.read_credentials(home)
    if login.status != 0 or credentials is None:
        raise LinkError(_("{name} sign-in was not completed.", name=DISPLAY_NAMES["grok"]))
    return AccountIdentity(credentials.email, None)


def _sign_in_claude(executable: Path, home: Path, on_output) -> AccountIdentity:
    # Claude Code keeps an isolated login in CLAUDE_CONFIG_DIR. The shared login is snapshotted
    # and restored anyway, so a CLI that also touches it cannot switch the user's own account.
    env = {"CLAUDE_CONFIG_DIR": str(home)}
    shared_file = claude.shared_credentials_file()
    shared_before = claude.read_file(shared_file)
    try:
        login = process.run(
            executable, ["auth", "login", "--claudeai"], env=env, timeout=LOGIN_TIMEOUT, keep_stdin_open=True, on_output=on_output
        )
        if login.status != 0:
            raise LinkError(_("{name} sign-in was not completed.", name=DISPLAY_NAMES["claude"]))
        if claude.parse_credentials(claude.read_file(home / claude.CREDENTIALS_FILE)) is None:
            raise LinkError(_("Claude did not save the account credentials."))
        status = process.run(executable, ["auth", "status", "--json"], env=env, timeout=60)
        identity = claude.identity_from_status(status.stdout)
        if identity is None:
            raise LinkError(_("Claude did not return the linked account."))
        return identity
    finally:
        if claude.read_file(shared_file) != shared_before:
            with contextlib.suppress(OSError):
                if shared_before is None:
                    shared_file.unlink()
                else:
                    claude.write_private(shared_file, shared_before)


def login_url(text: str) -> str | None:
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    match = re.search(r"https://[^\s\x1b\x07\"'<>]+", plain)
    return match.group(0) if match else None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve()) and path.resolve() != root.resolve()
    except OSError:
        return False
