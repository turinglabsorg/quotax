"""`quotax` command line. The GNOME Shell extension drives it with `--json`, which prints one JSON event per line."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import __version__, process
from .accounts import AccountStore, LinkError, detect_cli_login, fetch, link_shared_login, sign_in, unlink
from .formatting import countdown
from .i18n import _
from .models import DISPLAY_NAMES, PROVIDERS, Account, AccountIdentity, ProviderIssue


def main(argv: list[str] | None = None) -> int:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _stop)

    parser = argparse.ArgumentParser(prog="quotax", description="Remaining Claude, Codex and Grok subscription usage.")
    parser.add_argument("--version", action="version", version=f"quotax {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("print", help="print usage for the linked accounts (default)")
    fetch_parser = commands.add_parser("fetch", help="fetch usage for the linked accounts")
    fetch_parser.add_argument("--id", action="append", help="only this account (repeatable)")
    accounts_parser = commands.add_parser("accounts", help="list the linked accounts")
    detect_parser = commands.add_parser("detect", help="find the logins of the CLIs on this computer")
    link_parser = commands.add_parser("link", help="link the login a CLI already uses")
    link_parser.add_argument("provider", choices=PROVIDERS)
    link_parser.add_argument("--email")
    link_parser.add_argument("--plan")
    signin_parser = commands.add_parser("signin", help="sign in to another account in the browser")
    signin_parser.add_argument("provider", choices=PROVIDERS)
    unlink_parser = commands.add_parser("unlink", help="stop monitoring an account")
    unlink_parser.add_argument("id")
    for subparser in (fetch_parser, accounts_parser, detect_parser, link_parser, signin_parser, unlink_parser):
        subparser.add_argument("--json", action="store_true", help="one JSON event per line")

    args = parser.parse_args(argv)
    match args.command:
        case "fetch":
            return _fetch(args.id, args.json)
        case "accounts":
            return _accounts(args.json)
        case "detect":
            return _detect(args.json)
        case "link":
            return _link(args.provider, args.email, args.plan, args.json)
        case "signin":
            return _signin(args.provider, args.json)
        case "unlink":
            return _unlink(args.id, args.json)
        case _:
            return _print()


def _stop(signum, _frame):
    process.terminate_all()
    raise SystemExit(128 + signum)


def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _fetch_all(accounts: list[Account]):
    """Yields (account, snapshot, issue) as each account finishes."""
    if not accounts:
        return
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        futures = {pool.submit(fetch, account): account for account in accounts}
        for future in as_completed(futures):
            account = futures[future]
            try:
                yield account, future.result(), None
            except ProviderIssue as issue:
                yield account, None, issue
            except Exception:  # noqa: BLE001 - surfaced as an unreachable service, like the macOS app
                yield account, None, ProviderIssue("network")


def _fetch(ids: list[str] | None, as_json: bool) -> int:
    accounts = AccountStore.load()
    if ids:
        wanted = {account_id.upper() for account_id in ids}
        accounts = [account for account in accounts if account.id in wanted]
    for account, snapshot, issue in _fetch_all(accounts):
        if snapshot:
            AccountStore.update_identity(account.id, snapshot.account, snapshot.plan)
        if as_json:
            event = {"type": "result", "id": account.id}
            event.update({"snapshot": snapshot.to_json()} if snapshot else {"issue": issue.to_json()})
            _emit(event)
        else:
            _print_result(account, snapshot, issue)
    if as_json:
        _emit({"type": "done"})
    return 0


def _print() -> int:
    accounts = AccountStore.load()
    if not accounts:
        print(_("No accounts linked in Quotax: showing the CLI logins found on this computer.") + "\n")
        for provider in PROVIDERS:
            if identity := detect_cli_login(provider):
                accounts.append(Account(provider, "cli", identity.email, identity.plan))
    for account, snapshot, issue in _fetch_all(accounts):
        _print_result(account, snapshot, issue)
    return 0


def _print_result(account: Account, snapshot, issue) -> None:
    title = " · ".join(part for part in (DISPLAY_NAMES[account.provider], account.email, account.source_label) if part)
    if issue:
        print(f"{title}: {issue.message}")
        return
    now = time.time()
    print(f"{title} ({snapshot.plan})" if snapshot.plan else title)
    for window in snapshot.windows:
        reset = _(", resets in {countdown}", countdown=countdown(window.resets_at, now)) if window.resets_at else ""
        print("  " + _("{label}: {percent}% left{reset}", label=window.label, percent=window.remaining_at(now), reset=reset))


def _accounts(as_json: bool) -> int:
    for account in AccountStore.load():
        if as_json:
            _emit({"type": "account", "account": account.to_json()})
        else:
            parts = (DISPLAY_NAMES[account.provider], account.email, account.plan, account.source_label)
            print(f"{account.id}  " + " · ".join(part for part in parts if part))
    return 0


def _detect(as_json: bool) -> int:
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        found = dict(zip(PROVIDERS, pool.map(detect_cli_login, PROVIDERS)))
    for provider, identity in found.items():
        if as_json:
            _emit({"type": "login", "provider": provider, "identity": identity.to_json() if identity else None})
        elif identity:
            print(f"{DISPLAY_NAMES[provider]}: " + " · ".join(part for part in (identity.email, identity.plan) if part))
        else:
            print(f"{DISPLAY_NAMES[provider]}: " + _("not signed in"))
    return 0


def _link(provider: str, email: str | None, plan: str | None, as_json: bool) -> int:
    identity = AccountIdentity(email, plan) if email or plan else detect_cli_login(provider)
    account = link_shared_login(provider, identity)
    if as_json:
        _emit({"type": "linked", "account": account.to_json()})
    else:
        print(_("Linked {name}.", name=" · ".join(part for part in (DISPLAY_NAMES[provider], account.email) if part)))
    return 0


def _signin(provider: str, as_json: bool) -> int:
    shown: set[str] = set()

    def on_login_url(url: str):
        if shown:
            return
        shown.add(url)
        if as_json:
            _emit({"type": "url", "url": url})
        else:
            print(_("Finish signing in in your browser: {url}", url=url), flush=True)

    account = Account(provider, "managed")
    try:
        identity = sign_in(provider, account.id, on_login_url)
    except LinkError as error:
        return _signin_failed(error.message, as_json)
    account.email, account.plan = identity.email, identity.plan
    if account.email and AccountStore.has_managed_account(provider, account.email):
        unlink(account)
        return _signin_failed(_("{email} is already linked.", email=account.email), as_json)
    AccountStore.add(account)
    if as_json:
        _emit({"type": "linked", "account": account.to_json()})
    else:
        print(_("Linked {name}.", name=" · ".join(part for part in (DISPLAY_NAMES[provider], account.email) if part)))
    return 0


def _signin_failed(message: str, as_json: bool) -> int:
    if as_json:
        _emit({"type": "error", "message": message})
    else:
        print(message, file=sys.stderr)
    return 1


def _unlink(account_id: str, as_json: bool) -> int:
    account = AccountStore.remove(account_id.upper())
    if account:
        unlink(account)
    if as_json:
        _emit({"type": "unlinked", "id": account_id.upper(), "found": account is not None})
    elif account is None:
        print(_("No linked account with this ID."), file=sys.stderr)
    return 0 if account else 1
