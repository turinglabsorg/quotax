from __future__ import annotations

import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import http, jsonutil, process
from .i18n import _
from .models import Account, ProviderIssue, ProviderSnapshot, UsageWindow

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing"
PREFERRED_ISSUER = "https://auth.x.ai"


@dataclass
class GrokCredentials:
    access_token: str
    user_id: str | None = None
    email: str | None = None
    expires_at: float | None = None

    def is_fresh(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at - now > 5 * 60


@dataclass
class GrokBilling:
    kind: str  # "usage" | "needsMonthlyView" | "noQuota"
    plan: str | None = None
    window: UsageWindow | None = None


def parse_credentials(data: bytes | str | None, now: float | None = None) -> GrokCredentials | None:
    root = jsonutil.try_obj(data)
    if root is None:
        return None
    now = time.time() if now is None else now
    preferred, alternates, saw_preferred_issuer = [], [], False
    for key in sorted(root):
        is_preferred = key == PREFERRED_ISSUER or key.startswith(f"{PREFERRED_ISSUER}::")
        saw_preferred_issuer = saw_preferred_issuer or is_preferred
        entry = jsonutil.mapping(root[key])
        token = jsonutil.string(entry.get("key")) if entry else None
        if not token:
            continue
        expires_at = jsonutil.string(entry.get("expires_at"))
        credentials = GrokCredentials(
            access_token=token,
            user_id=jsonutil.string(entry.get("user_id")),
            email=jsonutil.string(entry.get("email")),
            expires_at=jsonutil.iso(expires_at) if expires_at else None,
        )
        (preferred if is_preferred else alternates).append(credentials)
    if fresh := next((credentials for credentials in preferred if credentials.is_fresh(now)), None):
        return fresh
    if preferred:
        return preferred[0]
    return None if saw_preferred_issuer else next(iter(alternates), None)


def parse_credits(data: bytes | str) -> GrokBilling:
    root = jsonutil.obj(data)
    config = _billing_config(root)
    if config is None:
        return GrokBilling("noQuota")
    plan = jsonutil.string(config.get("subscriptionTier"))
    period = jsonutil.mapping(config.get("currentPeriod"))
    resets_at = jsonutil.timestamp((period or {}).get("end")) or jsonutil.timestamp(config.get("billingPeriodEnd"))

    if (percent := _weekly_percent(config)) is not None:
        kind = "monthly" if jsonutil.string((period or {}).get("type")) == "USAGE_PERIOD_TYPE_MONTHLY" else "weekly"
        return GrokBilling("usage", plan, UsageWindow(kind, percent, resets_at))
    if monthly := _monthly_window(config, resets_at):
        return GrokBilling("usage", plan, monthly)
    return GrokBilling("needsMonthlyView", plan)


def parse_monthly(data: bytes | str) -> UsageWindow | None:
    root = jsonutil.obj(data)
    config = jsonutil.mapping(root.get("config")) or root
    period = jsonutil.mapping(config.get("currentPeriod")) or {}
    resets_at = jsonutil.timestamp(period.get("end")) or jsonutil.timestamp(config.get("billingPeriodEnd"))
    return _monthly_window(config, resets_at)


def _billing_config(root: dict) -> dict | None:
    if config := jsonutil.mapping(root.get("config")):
        return config
    flat_fields = (
        "creditUsagePercent", "currentPeriod", "billingPeriodStart", "billingPeriodEnd",
        "subscriptionTier", "monthlyLimit", "used", "onDemandCap", "onDemandUsed", "prepaidBalance",
    )
    return root if any(key in root for key in flat_fields) else None


def _weekly_percent(config: dict) -> float | None:
    """The billing API omits zero-valued fields, so a missing creditUsagePercent can mean 0% used.

    It only does when the weekly period is confirmed and no other field shows explicit zeros or spend.
    """
    if "creditUsagePercent" in config:
        return jsonutil.number(config["creditUsagePercent"])
    if _omitted_percent_is_unreported(config) or _monthly_window(config, None) is not None:
        return None
    return 0 if _has_confirmed_weekly_period(config) else None


def _money(value) -> float | None:
    return jsonutil.number((jsonutil.mapping(value) or {}).get("val"))


def _omitted_percent_is_unreported(config: dict) -> bool:
    if _money(config.get("onDemandCap")) == 0:
        return any((_money(config.get(key)) or 0) > 0 for key in ("onDemandUsed", "used"))
    return any(_money(config.get(key)) == 0 for key in ("onDemandCap", "onDemandUsed", "prepaidBalance", "monthlyLimit", "used"))


def _has_confirmed_weekly_period(config: dict) -> bool:
    period = jsonutil.mapping(config.get("currentPeriod"))
    if not period or jsonutil.string(period.get("type")) != "USAGE_PERIOD_TYPE_WEEKLY":
        return False
    start, end = jsonutil.timestamp(period.get("start")), jsonutil.timestamp(period.get("end"))
    if start is None or end is None:
        return False
    return start == jsonutil.timestamp(config.get("billingPeriodStart")) and end == jsonutil.timestamp(config.get("billingPeriodEnd"))


def _monthly_window(config: dict, resets_at: float | None) -> UsageWindow | None:
    limit = _money(config.get("monthlyLimit"))
    used = _money(config.get("used"))
    if limit is None or limit <= 0 or used is None:
        return None
    return UsageWindow("monthly", used / limit * 100, resets_at)


def home_for(account: Account) -> Path:
    if account.home:
        return account.home
    if os.environ.get("GROK_HOME"):
        return Path(os.environ["GROK_HOME"])
    return Path.home() / ".grok"


def read_credentials(home: Path) -> GrokCredentials | None:
    try:
        return parse_credentials((home / "auth.json").read_bytes())
    except OSError:
        return None


class GrokFetcher:
    def __init__(self, account: Account):
        self.account = account

    def fetch(self) -> ProviderSnapshot:
        home = home_for(self.account)
        credentials = read_credentials(home)
        if credentials is None:
            raise ProviderIssue.signed_out(_("This Grok account is not signed in."))
        if not credentials.is_fresh(time.time()):
            self._refresh_session(home)
            credentials = read_credentials(home)
            if credentials is None or not credentials.is_fresh(time.time()):
                raise ProviderIssue.session_expired(_("Grok session expired: relink the account."))

        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "X-XAI-Token-Auth": "xai-grok-cli",
            "Accept": "application/json",
        }
        if credentials.user_id:
            headers["x-userid"] = credentials.user_id
        email = credentials.email or self.account.email

        try:
            billing = parse_credits(http.get(f"{BILLING_URL}?{urllib.parse.urlencode({'format': 'credits'})}", headers))
            if billing.kind == "usage":
                return ProviderSnapshot("grok", billing.plan, [billing.window], account=email)
            if billing.kind == "needsMonthlyView":
                if window := parse_monthly(http.get(BILLING_URL, headers)):
                    return ProviderSnapshot("grok", billing.plan, [window], account=email)
                raise ProviderIssue.no_quota(_("Grok reports no usage percentage for this account."))
            raise ProviderIssue.no_quota(_("This account has no subscription limits."))
        except ProviderIssue as issue:
            if issue.kind == "http" and issue.status in (401, 403):
                raise ProviderIssue.session_expired(_("Grok session expired: relink the account.")) from None
            raise

    # Running any authenticated Grok command lets the CLI rotate its own access token.
    @staticmethod
    def _refresh_session(home: Path) -> None:
        executable = process.locate("grok")
        if executable is None:
            return
        try:
            process.run(executable, ["models"], env={"GROK_HOME": str(home)}, timeout=45)
        except (OSError, process.TimedOut):
            pass
