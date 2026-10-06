import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from quota import accounts, claude, codex, grok, jsonutil, paths
from quota.formatting import countdown
from quota.models import Account, AccountIdentity, ProviderIssue, ProviderSnapshot, UsageWindow, usage_level

os.environ["LANGUAGE"] = "en"


class ClaudeParserTests(unittest.TestCase):
    def test_maps_session_weekly_and_fable_windows(self):
        windows = claude.parse_windows("""
        {
          "five_hour": { "used_percentage": 23.5, "resets_at": 1770000000 },
          "seven_day": { "used_percentage": 41.2, "resets_at": 1770604800 },
          "fable_weekly": { "used_percentage": 12.3, "resets_at": 1770691200 }
        }
        """)
        self.assertEqual([(w.kind, w.model) for w in windows], [("session", None), ("weekly", None), ("weeklyModel", "Fable")])
        self.assertEqual(windows[0].used_percent, 23.5)
        self.assertEqual(windows[0].resets_at, 1_770_000_000)
        self.assertEqual(windows[2].resets_at, 1_770_691_200)

    def test_prefers_scoped_limits_and_parses_microsecond_timestamps(self):
        windows = claude.parse_windows("""
        {
          "five_hour": { "utilization": 36, "resets_at": "2026-07-17T15:00:00.099908+00:00" },
          "seven_day": { "utilization": 73 },
          "fable_weekly": { "utilization": 12 },
          "seven_day_opus": null,
          "seven_day_oauth_apps": { "utilization": 99 },
          "limits": [
            { "kind": "weekly_scoped", "percent": 55, "scope": null },
            {
              "kind": "weekly_scoped",
              "percent": 100,
              "resets_at": "2026-07-17T20:00:00.099908+00:00",
              "scope": { "model": { "display_name": "Fable" } }
            }
          ]
        }
        """)
        self.assertEqual([(w.kind, w.model) for w in windows], [("session", None), ("weekly", None), ("weeklyModel", "Fable")])
        self.assertEqual(windows[2].used_percent, 100)
        self.assertEqual(windows[2].resets_at, jsonutil.iso("2026-07-17T20:00:00Z"))
        self.assertEqual(windows[0].resets_at, jsonutil.iso("2026-07-17T15:00:00Z"))

    def test_reads_credentials_and_plan(self):
        credentials = claude.parse_credentials(
            '{ "claudeAiOauth": { "accessToken": "token", "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x" } }'
        )
        self.assertEqual(credentials, claude.ClaudeCredentials(access_token="token", plan="Max 20x"))
        self.assertIsNone(claude.parse_credentials('{ "claudeAiOauth": {} }'))
        self.assertEqual(claude.plan_label("pro", None), "Pro")

    def test_rejects_invalid_json(self):
        with self.assertRaises(ProviderIssue) as context:
            claude.parse_windows("<html>")
        self.assertEqual(context.exception.kind, "invalidResponse")


class CodexParserTests(unittest.TestCase):
    def test_classifies_windows_by_duration(self):
        plan, windows = codex.parse_usage("""
        {
          "plan_type": "plus",
          "rate_limit": {
            "primary_window": { "used_percent": 37, "limit_window_seconds": 604800, "reset_at": 1800000000 },
            "secondary_window": { "used_percent": 12, "limit_window_seconds": 18000, "reset_at": 1800100000 }
          }
        }
        """)
        self.assertEqual(plan, "Plus")
        self.assertEqual([w.kind for w in windows], ["session", "weekly"])
        self.assertEqual(windows[0].used_percent, 12)
        self.assertEqual(windows[1].resets_at, 1_800_000_000)

    def test_keeps_unknown_durations_as_custom_windows(self):
        _plan, windows = codex.parse_usage("""
        {
          "plan_type": "pro",
          "rate_limit": {
            "primary_window": { "used_percent": 12, "limit_window_seconds": 3600 },
            "secondary_window": null
          }
        }
        """)
        self.assertEqual([(w.kind, w.minutes) for w in windows], [("custom", 60)])
        self.assertEqual(windows[0].label, "1 h window")

    def test_maps_app_server_rate_limits(self):
        plan, windows = codex.parse_rpc_usage("""
        {
          "rateLimits": {
            "limitId": "codex",
            "primary": { "usedPercent": 0, "windowDurationMins": 43200, "resetsAt": 1791954967 },
            "secondary": null,
            "credits": { "hasCredits": false, "unlimited": false, "balance": null },
            "planType": "free"
          }
        }
        """)
        self.assertEqual(plan, "Free")
        self.assertEqual(windows, [UsageWindow("monthly", 0, 1_791_954_967)])

        _plan, paid = codex.parse_rpc_usage("""
        { "rateLimits": {
            "primary": { "usedPercent": 41, "windowDurationMins": 300, "resetsAt": 1800000000 },
            "secondary": { "usedPercent": 12, "windowDurationMins": 10080, "resetsAt": 1800100000 },
            "planType": "pro" } }
        """)
        self.assertEqual([w.kind for w in paid], ["session", "weekly"])
        with self.assertRaises(ProviderIssue):
            codex.parse_rpc_usage('{ "rateLimits": null }')

    def test_requires_plan_type(self):
        with self.assertRaises(ProviderIssue):
            codex.parse_usage('{ "detail": "x" }')

    def test_detects_auth_mode(self):
        self.assertEqual(
            codex.parse_auth('{ "tokens": { "access_token": "a", "account_id": "acc" } }'), codex.CodexAuth("chatgpt", "a", "acc")
        )
        self.assertEqual(codex.parse_auth('{ "OPENAI_API_KEY": "sk-test", "tokens": null }'), codex.CodexAuth("apiKey"))
        self.assertIsNone(codex.parse_auth('{ "OPENAI_API_KEY": null }'))

    def test_makes_plan_identifiers_readable(self):
        identity = codex.parse_identity('{ "account": { "email": "me@example.com", "planType": "self_serve_business_prolite" } }')
        self.assertEqual(identity.plan, "Business")
        self.assertEqual(codex.plan_name("edu_plus"), "Edu plus")


class GrokParserTests(unittest.TestCase):
    def test_maps_weekly_credits(self):
        billing = grok.parse_credits("""
        {
          "config": {
            "creditUsagePercent": 42,
            "currentPeriod": {
              "type": "USAGE_PERIOD_TYPE_WEEKLY",
              "start": "2026-06-30T18:36:14.268512+00:00",
              "end": "2026-07-07T18:36:14.268512+00:00"
            },
            "subscriptionTier": "SuperGrok"
          }
        }
        """)
        expected = UsageWindow("weekly", 42, jsonutil.iso("2026-07-07T18:36:14Z"))
        self.assertEqual(billing, grok.GrokBilling("usage", "SuperGrok", expected))

    def test_falls_back_to_monthly_budget(self):
        billing = grok.parse_credits(
            '{ "config": { "subscriptionTier": "SuperGrok Heavy", "monthlyLimit": { "val": "200" }, "used": { "val": 50 } } }'
        )
        self.assertEqual(billing, grok.GrokBilling("usage", "SuperGrok Heavy", UsageWindow("monthly", 25, None)))

    def test_treats_omitted_percent_in_confirmed_weekly_period_as_zero(self):
        billing = grok.parse_credits("""
        {
          "config": {
            "currentPeriod": {
              "type": "USAGE_PERIOD_TYPE_WEEKLY",
              "start": "2026-10-01T07:17:10.164276+00:00",
              "end": "2026-10-08T07:17:10.164276+00:00"
            },
            "onDemandCap": { "val": 0 },
            "onDemandUsed": { "val": 0 },
            "isUnifiedBillingUser": true,
            "prepaidBalance": { "val": 0 },
            "topUpMethod": "TOP_UP_METHOD_SAVED_PAYMENT_METHOD",
            "billingPeriodStart": "2026-10-01T07:17:10.164276+00:00",
            "billingPeriodEnd": "2026-10-08T07:17:10.164276+00:00"
          }
        }
        """)
        self.assertEqual(billing, grok.GrokBilling("usage", None, UsageWindow("weekly", 0, jsonutil.iso("2026-10-08T07:17:10Z"))))

    def test_keeps_omitted_percent_unknown_without_confirmation(self):
        mismatched_period = grok.parse_credits("""
        { "config": {
            "currentPeriod": { "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00Z" },
            "billingPeriodStart": "2026-10-01T00:00:00Z", "billingPeriodEnd": "2026-11-01T00:00:00Z" } }
        """)
        self.assertEqual(mismatched_period, grok.GrokBilling("needsMonthlyView"))

        spend_without_percent = grok.parse_credits("""
        { "config": {
            "currentPeriod": { "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00Z" },
            "billingPeriodStart": "2026-10-01T00:00:00Z", "billingPeriodEnd": "2026-10-08T00:00:00Z",
            "onDemandCap": { "val": 0 }, "onDemandUsed": { "val": 3 } } }
        """)
        self.assertEqual(spend_without_percent, grok.GrokBilling("needsMonthlyView"))

        explicit_zeros = grok.parse_credits("""
        { "config": {
            "currentPeriod": { "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-10-01T00:00:00Z", "end": "2026-10-08T00:00:00Z" },
            "billingPeriodStart": "2026-10-01T00:00:00Z", "billingPeriodEnd": "2026-10-08T00:00:00Z",
            "onDemandCap": { "val": 50 }, "prepaidBalance": { "val": 0 } } }
        """)
        self.assertEqual(explicit_zeros, grok.GrokBilling("needsMonthlyView"))

    def test_asks_for_monthly_view_when_percent_missing(self):
        self.assertEqual(
            grok.parse_credits('{ "config": { "subscriptionTier": "Enterprise" } }'), grok.GrokBilling("needsMonthlyView", "Enterprise")
        )
        self.assertEqual(grok.parse_credits('{ "unrelated": true }'), grok.GrokBilling("noQuota"))

    def test_prefers_fresh_default_issuer(self):
        now = jsonutil.iso("2026-09-28T10:00:00Z")
        credentials = grok.parse_credentials(
            """
        {
          "https://auth.x.ai::stale": { "key": "old", "expires_at": "2026-09-01T00:00:00.000Z" },
          "https://auth.x.ai::client": { "key": "fresh", "user_id": "user-1", "expires_at": "2099-01-01T00:00:00.000Z" },
          "https://other.example": { "key": "alternate" }
        }
        """,
            now=now,
        )
        self.assertEqual(credentials.access_token, "fresh")
        self.assertEqual(credentials.user_id, "user-1")

    def test_reports_expired_sessions_and_ignores_alternates_when_default_exists(self):
        now = jsonutil.iso("2026-09-28T10:00:00Z")
        expired = grok.parse_credentials('{ "https://auth.x.ai": { "key": "old", "expires_at": "2026-09-28T10:02:00Z" } }', now=now)
        self.assertEqual(expired.access_token, "old")
        self.assertFalse(expired.is_fresh(now))
        self.assertIsNone(grok.parse_credentials('{ "https://auth.x.ai": {}, "https://other": { "key": "x" } }', now=now))
        self.assertEqual(grok.parse_credentials('{ "https://other": { "key": "x" } }', now=now).access_token, "x")


class WindowTests(unittest.TestCase):
    def test_treats_passed_reset_as_fully_available(self):
        now = time.time()
        self.assertEqual(UsageWindow("session", 91.6, now - 10).remaining_at(now), 100)
        self.assertEqual(UsageWindow("session", 91.6, None).remaining_at(now), 8)

    def test_rounds_half_away_from_zero(self):
        self.assertEqual(UsageWindow("session", 12.5).remaining_at(time.time()), 87)

    def test_menu_bar_ignores_model_scoped_limits(self):
        now = time.time()
        snapshot = ProviderSnapshot(
            "claude",
            None,
            [UsageWindow("session", 30), UsageWindow("weekly", 60), UsageWindow("weeklyModel", 95, model="Fable")],
        )
        self.assertEqual(snapshot.tightest_window(now).kind, "weekly")
        model_only = ProviderSnapshot("claude", None, [UsageWindow("weeklyModel", 95, model="Fable")])
        self.assertEqual(model_only.tightest_window(now).kind, "weeklyModel")

    def test_levels_follow_remaining_percent(self):
        self.assertEqual(usage_level(50), "normal")
        self.assertEqual(usage_level(20), "warning")
        self.assertEqual(usage_level(5), "critical")

    def test_formats_countdowns(self):
        now = time.time()
        self.assertEqual(countdown(now + 2 * 3_600 + 10 * 60 + 5, now), "2h 10m")
        self.assertEqual(countdown(now + 3 * 86_400 + 4 * 3_600 + 30, now), "3d 4h")
        self.assertEqual(countdown(now + 20, now), "1m")
        self.assertEqual(countdown(now - 5, now), "now")


class AccountTests(unittest.TestCase):
    def test_parses_claude_auth_status(self):
        identity = claude.identity_from_status(
            '{ "loggedIn": true, "authMethod": "claude.ai", "email": "me@example.com", "orgName": "Example", "subscriptionType": "team" }'
        )
        self.assertEqual(identity, AccountIdentity("me@example.com", "Team"))
        self.assertIsNone(claude.identity_from_status('{ "loggedIn": false }'))

    def test_merges_refreshed_claude_token_keeping_other_fields(self):
        stored = (
            '{ "claudeAiOauth": { "accessToken": "old", "refreshToken": "r1", "expiresAt": 1, '
            '"subscriptionType": "max", "rateLimitTier": "default_claude_max_5x" }, "other": 1 }'
        )
        merged = claude.applying_refresh('{ "access_token": "new", "refresh_token": "r2", "expires_in": 3600 }', stored, now=1_000)
        credentials = claude.parse_credentials(merged)
        self.assertEqual(credentials.access_token, "new")
        self.assertEqual(credentials.refresh_token, "r2")
        self.assertEqual(credentials.expires_at, 4_600)
        self.assertEqual(credentials.plan, "Max 5x")
        self.assertEqual(json.loads(merged)["other"], 1)
        self.assertIsNone(claude.applying_refresh('{ "error": "invalid_grant" }', stored))

    def test_parses_codex_account(self):
        self.assertEqual(
            codex.parse_identity('{ "account": { "type": "chatgpt", "email": "me@example.com", "planType": "plus" }, "requiresOpenaiAuth": true }'),
            AccountIdentity("me@example.com", "Plus"),
        )
        self.assertIsNone(codex.parse_identity('{ "account": null, "requiresOpenaiAuth": true }'))
        self.assertIsNone(codex.parse_identity('{ "account": { "email": "me@example.com", "planType": "unknown" } }').plan)

    def test_extracts_login_url_from_terminal_output(self):
        output = (
            "\x1b[1mStarting local login server.\x1b[0m If your browser did not open, navigate to:\n\n"
            "  https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz\n"
        )
        self.assertEqual(accounts.login_url(output), "https://auth.openai.com/oauth/authorize?client_id=abc&state=xyz")
        self.assertIsNone(accounts.login_url("no link here"))

    def test_accounts_round_trip_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory, "XDG_DATA_HOME": directory}
        ):
            account = Account("grok", "managed", "me@example.com")
            accounts.AccountStore.add(Account("codex", "cli"))
            accounts.AccountStore.add(account)
            accounts.AccountStore.add(Account("claude", "cli"))
            loaded = accounts.AccountStore.load()
            self.assertEqual([item.provider for item in loaded], ["claude", "codex", "grok"])
            self.assertEqual(loaded[2], account)
            self.assertNotIn("token", paths.accounts_file().read_text())
            self.assertTrue(str(account.home).endswith(f"quota/accounts/grok/{account.id}"))
            self.assertIsNone(Account("claude", "cli").home)

    def test_rejects_account_ids_that_are_not_uuids(self):
        self.assertIsNone(Account.from_json({"id": "../../etc", "provider": "grok", "source": "managed"}))

    def test_unlink_only_removes_isolated_homes(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
            account = Account("claude", "managed")
            account.home.mkdir(parents=True)
            (account.home / ".credentials.json").write_text("{}")
            accounts.unlink(account)
            self.assertFalse(account.home.exists())
            self.assertTrue(paths.accounts_root().exists())


class CatalogTests(unittest.TestCase):
    """Every user-facing string needs an Italian translation with the same placeholders."""

    ROOT = Path(__file__).resolve().parent.parent

    def test_italian_catalog_covers_every_string(self):
        catalog = json.loads((self.ROOT / "quota/locale/it.json").read_text(encoding="utf-8"))
        sources = [*self.ROOT.glob("quota/*.py"), *self.ROOT.glob("extension/*.js")]
        pattern = re.compile(r"""\b_\(\s*(["'])((?:\\.|(?!\1).)*)\1""")
        keys = set()
        for source in sources:
            for match in pattern.finditer(source.read_text(encoding="utf-8")):
                keys.add(match.group(2).encode().decode("unicode_escape").encode("latin-1").decode("utf-8"))
        self.assertTrue(keys)
        self.assertEqual(sorted(keys - catalog.keys()), [])
        for key, value in catalog.items():
            self.assertEqual(sorted(re.findall(r"\{\w+\}", key)), sorted(re.findall(r"\{\w+\}", value)), key)


if __name__ == "__main__":
    unittest.main()
