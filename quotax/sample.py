"""Sample data for screenshots and UI work (`QUOTAX_SAMPLE=1`). Never touches the network or the CLIs."""

import os
import time

from .models import Account, AccountIdentity, ProviderSnapshot, UsageWindow

EMAIL = "name@example.com"
PLANS = {"claude": "Team", "codex": "Plus", "grok": "SuperGrok", "ollama": "Pro"}


def enabled() -> bool:
    return bool(os.environ.get("QUOTAX_SAMPLE"))


def identity(provider: str) -> AccountIdentity:
    return AccountIdentity(EMAIL, None if provider == "grok" else PLANS[provider])


def snapshot(account: Account) -> ProviderSnapshot:
    now = time.time()

    def later(days=0, hours=0, minutes=0):
        return now + days * 86_400 + hours * 3_600 + minutes * 60 + 30

    if account.provider == "claude" and account.source == "managed":
        windows = [
            UsageWindow("session", 9, later(hours=4, minutes=32)),
            UsageWindow("weekly", 41, later(days=5, hours=2)),
        ]
        return ProviderSnapshot("claude", account.plan or "Max 5x", windows, account=account.email or EMAIL, fetched_at=now - 125)

    windows = {
        "claude": [
            UsageWindow("session", 28, later(hours=2, minutes=10)),
            UsageWindow("weekly", 64, later(days=3, hours=4)),
            UsageWindow("weeklyModel", 83, later(days=3, hours=4), model="Fable"),
        ],
        "codex": [
            UsageWindow("session", 12, later(hours=4, minutes=1)),
            UsageWindow("weekly", 37, later(days=5, hours=13)),
        ],
        "grok": [UsageWindow("weekly", 96, later(days=3, hours=9))],
        "ollama": [UsageWindow("monthly", 43)],
    }[account.provider]
    return ProviderSnapshot(account.provider, PLANS[account.provider], windows, account=account.email or EMAIL, fetched_at=now - 125)
