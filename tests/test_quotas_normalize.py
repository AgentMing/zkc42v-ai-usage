"""Unit tests for quota normalize + isolated fetch error paths."""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from quotas.credentials import Credential  # noqa: E402
from quotas.fetch import (  # noqa: E402
    fetch_all_quotas,
    fetch_market_snapshot,
    normalize_codex_usage,
    normalize_grok_billing,
    normalize_kimi_usages,
    normalize_market_snapshot,
    normalize_ollama_usage,
    normalize_windsurf_usage,
    parse_opencode_go_dashboard,
)
from quotas.models import SERVICE_NAMES  # noqa: E402

FIXTURES = ROOT / "fixtures" / "quotas"


class NormalizeTests(unittest.TestCase):
    def test_market_snapshot_normalization(self):
        payload = {
            "data": {
                "diff": [
                    {"f2": 3000.0, "f3": 0.65, "f12": "000001"},
                    {"f2": 10000.0, "f3": -0.21, "f12": "399001"},
                    {"f2": 5000.0, "f3": 0.12, "f12": "SPX"},
                    {"f2": 16000.0, "f3": -0.08, "f12": "NDX"},
                ]
            }
        }
        snapshot = normalize_market_snapshot(payload)
        self.assertEqual([item["key"] for item in snapshot["items"]], ["sh", "sz", "spx", "ndx"])
        self.assertEqual(snapshot["items"][0]["change_percent"], 0.65)
        self.assertEqual(snapshot["items"][1]["change_percent"], -0.21)

    def test_market_snapshot_fetch_uses_public_endpoint(self):
        from quotas.fetch import MARKET_URL

        seen = {}

        def fake_http(url, **kwargs):
            seen["url"] = url
            return 200, {}, json.dumps({"data": {"diff": [{"f2": 1, "f3": 0.5, "f12": "SPX"}]}}).encode()

        snapshot = fetch_market_snapshot(http=fake_http)
        self.assertEqual(seen["url"], MARKET_URL)
        self.assertEqual(snapshot["items"][0]["key"], "spx")

    def test_codex_retries_connection_failure(self):
        from quotas.credentials import Credential
        from quotas.fetch import fetch_codex

        payload = (FIXTURES / "codex_usage.json").read_bytes()
        calls = 0

        def flaky_http(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise urllib.error.URLError("temporary network failure")
            return 200, {}, payload

        cred = Credential(service="codex", present=True, kind="oauth", access_token="test")
        with patch("quotas.fetch.time.sleep"):
            rec = fetch_codex(cred, http=flaky_http)
        self.assertEqual(rec.status, "ok")
        self.assertEqual(calls, 3)

    def test_grok_retries_temporary_http_status(self):
        from quotas.credentials import Credential
        from quotas.fetch import fetch_grok

        payload = (FIXTURES / "grok_billing.json").read_bytes()
        calls = 0

        def flaky_http(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                return 503, {}, b"temporary"
            return 200, {}, payload

        cred = Credential(service="grok", present=True, kind="oauth", access_token="test")
        with patch("quotas.fetch.time.sleep"):
            rec = fetch_grok(cred, http=flaky_http)
        self.assertEqual(rec.status, "ok")
        self.assertEqual(calls, 3)

    def test_codex_fixture_fields(self):
        payload = json.loads((FIXTURES / "codex_usage.json").read_text())
        rec = normalize_codex_usage(payload)
        self.assertEqual(rec.name, "codex")
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.used_percent, 42.0)
        self.assertEqual(rec.remaining_percent, 58.0)
        self.assertIsNotNone(rec.reset_at)
        self.assertTrue(rec.windows)

    def test_grok_fixture_fields(self):
        payload = json.loads((FIXTURES / "grok_billing.json").read_text())
        rec = normalize_grok_billing(payload)
        self.assertEqual(rec.name, "grok")
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.used_percent, 80.0)
        self.assertEqual(rec.remaining_percent, 20.0)
        self.assertIsNotNone(rec.reset_at)

    def test_kimi_fixture_fields(self):
        payload = json.loads((FIXTURES / "kimi_usages.json").read_text())
        rec = normalize_kimi_usages(payload)
        self.assertEqual(rec.name, "kimi")
        self.assertEqual(rec.status, "ok")
        self.assertIsNotNone(rec.used_percent)
        self.assertIsNotNone(rec.remaining_percent)
        # used 30 / limit 100 -> 30% used, 70% remaining
        self.assertAlmostEqual(rec.used_percent, 30.0)
        self.assertAlmostEqual(rec.remaining_percent, 70.0)
        self.assertIsNotNone(rec.reset_at)
        labels = {str(w.get("label")) for w in rec.windows}
        self.assertIn("week", labels)
        self.assertIn("5h", labels)

    def test_codex_labels_week_from_window_seconds(self):
        payload = json.loads((FIXTURES / "codex_usage.json").read_text())
        rec = normalize_codex_usage(payload)
        self.assertTrue(rec.windows)
        self.assertEqual(rec.windows[0]["label"], "week")
        self.assertAlmostEqual(rec.windows[0]["remaining_percent"], 58.0)

    def test_opencode_go_dashboard_parse(self):
        html = (FIXTURES / "opencode_go_dashboard.html").read_text()
        windows = parse_opencode_go_dashboard(html)
        self.assertIn("rolling", windows)
        self.assertEqual(windows["rolling"]["usagePercent"], 12.5)
        self.assertEqual(windows["weekly"]["usagePercent"], 40.0)
        self.assertEqual(windows["monthly"]["usagePercent"], 55.0)

    def test_ollama_usage_fixture_fields(self):
        payload = json.loads((FIXTURES / "ollama_usage.json").read_text())
        rec = normalize_ollama_usage(payload)
        self.assertEqual(rec.name, "ollama-pro")
        self.assertEqual(rec.status, "ok")
        self.assertAlmostEqual(rec.used_percent, 18.0)
        self.assertAlmostEqual(rec.remaining_percent, 82.0)
        self.assertIn("pro", rec.detail)
        self.assertEqual([w["label"] for w in rec.windows], ["5h", "week"])
        self.assertAlmostEqual(rec.windows[1]["remaining_percent"], 91.0)
        self.assertIsNone(rec.windows[0]["reset_at"])

    def test_windsurf_usage_fixture_fields(self):
        payload = json.loads((FIXTURES / "windsurf_usage.json").read_text())
        rec = normalize_windsurf_usage(payload)
        self.assertEqual(rec.name, "windsurf")
        self.assertEqual(rec.status, "ok")
        self.assertAlmostEqual(rec.used_percent, 24.0)
        self.assertAlmostEqual(rec.remaining_percent, 76.0)
        self.assertIn("Pro", rec.detail)
        self.assertIn("extra=$964.22", rec.detail)
        self.assertEqual([w["label"] for w in rec.windows], ["day", "week"])
        self.assertEqual(rec.windows[0]["display_label"], "day")
        self.assertAlmostEqual(rec.windows[1]["remaining_percent"], 62.0)

    def test_windsurf_cached_usage_counters(self):
        rec = normalize_windsurf_usage(
            {
                "planName": "Max",
                "usage": {
                    "messages": 100,
                    "usedMessages": 25,
                    "flowActions": 200,
                    "usedFlowActions": 80,
                },
            }
        )
        self.assertEqual(rec.status, "ok")
        self.assertEqual(rec.detail, "Max")
        self.assertAlmostEqual(rec.windows[0]["used_percent"], 25.0)
        self.assertAlmostEqual(rec.windows[1]["used_percent"], 40.0)

    def test_devin_web_quota_shape(self):
        rec = normalize_windsurf_usage(
            {
                "plan_name": "pro",
                "daily_percentage": 0.12,
                "weekly_percentage": 42,
                "daily_reset_at": "2026-06-11T00:00:00-08:00",
                "weekly_reset_at": "2026-06-14T00:00:00-08:00",
                "overage_balance_cents": 7087,
            }
        )
        self.assertEqual(rec.status, "ok")
        self.assertAlmostEqual(rec.windows[0]["used_percent"], 12.0)
        self.assertAlmostEqual(rec.windows[1]["remaining_percent"], 58.0)
        self.assertIn("extra=$70.87", rec.detail)

    def test_devin_web_fetch_sends_bearer_and_org(self):
        from quotas.fetch import DEVIN_WEB_QUOTA_BASE_URL, fetch_devin_web_quota

        seen = {}

        def fake_http(url, **kwargs):
            seen["url"] = url
            seen["method"] = kwargs.get("method", "GET")
            seen["headers"] = kwargs.get("headers")
            return 200, {}, json.dumps(
                {
                    "plan_name": "pro",
                    "daily_percentage": 0.12,
                    "weekly_percentage": 42,
                }
            ).encode()

        rec = fetch_devin_web_quota(
            Credential(
                service="windsurf",
                present=True,
                kind="bearer",
                access_token="auth1-test-token",
                extra={
                    "quota_source": "devin-web",
                    "organization": "org/example-org",
                    "internal_organization_id": "org_GQ6LhcfkW1TSinM6",
                },
            ),
            http=fake_http,
        )
        self.assertEqual(
            seen["url"],
            f"{DEVIN_WEB_QUOTA_BASE_URL}/org_GQ6LhcfkW1TSinM6/billing/quota/usage",
        )
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer auth1-test-token")
        self.assertEqual(seen["headers"]["x-cog-org-id"], "org_GQ6LhcfkW1TSinM6")
        self.assertEqual(rec.status, "ok")

    def test_ollama_fetch_sends_bearer_key(self):
        from quotas.fetch import OLLAMA_USAGE_URL, fetch_ollama_pro

        seen = {}

        def fake_http(url, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return 200, {}, (FIXTURES / "ollama_usage.json").read_bytes()

        rec = fetch_ollama_pro(
            Credential(
                service="ollama-pro",
                present=True,
                kind="api_key",
                api_key="ollama-test-key",
            ),
            http=fake_http,
        )
        self.assertEqual(seen["url"], OLLAMA_USAGE_URL)
        self.assertEqual(seen["headers"]["Authorization"], "Bearer ollama-test-key")
        self.assertEqual(rec.status, "ok")

    def test_windsurf_fetch_sends_json_api_key(self):
        from quotas.fetch import WINDSURF_USAGE_URL, fetch_windsurf

        seen = {}

        def fake_http(url, **kwargs):
            seen["url"] = url
            seen["method"] = kwargs.get("method")
            seen["headers"] = kwargs.get("headers")
            seen["body"] = json.loads(kwargs["data"].decode())
            return 200, {}, (FIXTURES / "windsurf_usage.json").read_bytes()

        rec = fetch_windsurf(
            Credential(
                service="windsurf",
                present=True,
                kind="api_key",
                api_key="sk-ws-test",
            ),
            http=fake_http,
        )
        self.assertEqual(seen["url"], WINDSURF_USAGE_URL)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["headers"]["Connect-Protocol-Version"], "1")
        self.assertEqual(seen["body"]["metadata"]["apiKey"], "sk-ws-test")
        self.assertEqual(rec.status, "ok")

    def test_missing_credential_does_not_abort_others(self):
        """fetch_all_quotas isolates failures; missing creds → unavailable."""
        creds = {
            "codex": Credential(service="codex", present=False, kind="missing"),
            "grok": Credential(service="grok", present=False, kind="missing"),
            "kimi": Credential(service="kimi", present=False, kind="missing"),
            "opencode-go": Credential(service="opencode-go", present=False, kind="missing"),
            "ollama-pro": Credential(service="ollama-pro", present=False, kind="missing"),
            "windsurf": Credential(service="windsurf", present=False, kind="missing"),
        }

        def boom_http(*a, **k):
            raise AssertionError("http should not be called when creds missing")

        records = fetch_all_quotas(creds, http=boom_http)
        self.assertEqual([r.name for r in records], list(SERVICE_NAMES))
        for r in records:
            self.assertEqual(r.status, "unavailable")
            self.assertTrue(r.detail)

    def test_partial_http_failure_isolated(self):
        """One provider HTTP failure does not crash siblings."""
        payloads = {
            "https://chatgpt.com/backend-api/codex/usage": (
                200,
                {},
                (FIXTURES / "codex_usage.json").read_bytes(),
            ),
            "https://cli-chat-proxy.grok.com/v1/billing?format=credits": (
                500,
                {},
                b"upstream error",
            ),
        }

        def fake_http(url, **kwargs):
            for key, val in payloads.items():
                if url.startswith(key.split("?")[0]) or url == key:
                    return val
            if "kimi.com" in url:
                return 401, {}, b"no key"
            if "opencode.ai" in url:
                return 200, {}, b'{"data":[]}'
            if "ollama.com/api/usage" in url:
                return 200, {}, (FIXTURES / "ollama_usage.json").read_bytes()
            if "windsurf.com" in url:
                return 200, {}, (FIXTURES / "windsurf_usage.json").read_bytes()
            return 404, {}, b"nope"

        creds = {
            "codex": Credential(
                service="codex",
                present=True,
                kind="oauth",
                access_token="t",
                account_id="a",
            ),
            "grok": Credential(service="grok", present=True, kind="oauth", access_token="t"),
            "kimi": Credential(service="kimi", present=True, kind="api_key", api_key="sk-kimi-x"),
            "opencode-go": Credential(
                service="opencode-go",
                present=True,
                kind="api_key",
                api_key="sk-x",
            ),
            "ollama-pro": Credential(
                service="ollama-pro",
                present=True,
                kind="api_key",
                api_key="ollama-test",
            ),
            "windsurf": Credential(
                service="windsurf",
                present=True,
                kind="api_key",
                api_key="windsurf-test",
            ),
        }
        records = fetch_all_quotas(creds, http=fake_http)
        by = {r.name: r for r in records}
        self.assertEqual(by["codex"].status, "ok")
        self.assertEqual(by["codex"].used_percent, 42.0)
        self.assertEqual(by["grok"].status, "error")
        self.assertIn("500", by["grok"].detail)
        self.assertEqual(by["kimi"].status, "error")
        # opencode-go key works but no dashboard → unavailable (not crash)
        self.assertEqual(by["opencode-go"].status, "key ok")
        self.assertEqual(by["ollama-pro"].status, "ok")
        self.assertEqual(by["ollama-pro"].remaining_percent, 82.0)
        self.assertEqual(by["windsurf"].status, "ok")
        self.assertEqual(by["windsurf"].remaining_percent, 76.0)


if __name__ == "__main__":
    unittest.main()
