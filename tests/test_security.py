from __future__ import annotations

import unittest

from role_monitor.security import REDACTION, redact_text, safe_error


class RedactionTests(unittest.TestCase):
    def test_redacts_headers_assignments_urls_and_explicit_canaries(self):
        canary = "portfolio-canary-4f85b9"
        value = (
            "Authorization: Bearer bearer-value "
            "api_key=key-value "
            "https://example.test/path?token=query-value&safe=yes "
            "https://hooks.slack.com/services/T1/B2/secret "
            f"explicit={canary}"
        )
        result = redact_text(value, [canary])
        for secret in ("bearer-value", "key-value", "query-value", "T1/B2/secret", canary):
            self.assertNotIn(secret, result)
        self.assertGreaterEqual(result.count(REDACTION), 5)

    def test_safe_error_is_single_line_and_bounded(self):
        result = safe_error(RuntimeError("password=hunter2\n" + "x" * 500), limit=80)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("\n", result)
        self.assertLessEqual(len(result), 80)

    def test_redacts_common_authorization_and_client_secret_aliases(self):
        value = (
            "Authorization: Token token-value; "
            "Authorization=ApiKey key-value; "
            "X-API-Key: header-value; "
            "client_secret=client-value; "
            "https://example.test/callback?client_secret=query-value"
        )
        result = redact_text(value)
        for secret in ("token-value", "key-value", "header-value", "client-value", "query-value"):
            self.assertNotIn(secret, result)
        self.assertGreaterEqual(result.count(REDACTION), 5)

    def test_redacts_bare_oauth_session_tokens_and_cookie_headers(self):
        value = (
            "token=bare-value; refresh_token=refresh-value; "
            "oauth_token: oauth-value; id_token=id-value; "
            "session_id=session-value; Cookie: sid=cookie-value; preference=safe"
        )
        result = redact_text(value)
        for secret in (
            "bare-value",
            "refresh-value",
            "oauth-value",
            "id-value",
            "session-value",
            "cookie-value",
        ):
            self.assertNotIn(secret, result)
        self.assertGreaterEqual(result.count(REDACTION), 6)


if __name__ == "__main__":
    unittest.main()
