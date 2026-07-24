from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from role_monitor.http import RetryPolicy
from role_monitor.notifications import Notification
from role_monitor.telegram import (
    NotificationError,
    TelegramCredentials,
    TelegramHttpResponse,
    TelegramNotifier,
    UrllibTelegramSender,
    load_telegram_credentials,
)


BOT_TOKEN = "12345:" + ("A" * 20)
CHAT_ID = "-10012345"


def success(message_id: int = 42) -> TelegramHttpResponse:
    return TelegramHttpResponse(200, json.dumps({"ok": True, "result": {"message_id": message_id}}))


class FakeSender:
    def __init__(self, responses: list[TelegramHttpResponse | BaseException]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def post(self, bot_token, payload, *, timeout):  # type: ignore[no-untyped-def]
        self.calls.append((bot_token, dict(payload), timeout))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class TelegramTests(unittest.TestCase):
    def credentials(self) -> TelegramCredentials:
        return TelegramCredentials(BOT_TOKEN, CHAT_ID)

    def notification(self) -> Notification:
        return Notification("scan.completed", "Synthetic scan complete")

    def test_environment_loader_requires_both_values_and_rejects_placeholders(self):
        for environment in ({}, {"TELEGRAM_BOT_TOKEN": BOT_TOKEN}):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(ValueError, "Set both"):
                    load_telegram_credentials(environment)
        with self.assertRaisesRegex(ValueError, "placeholders"):
            load_telegram_credentials({
                "TELEGRAM_BOT_TOKEN": "replace-with-your-bot-token",
                "TELEGRAM_CHAT_ID": "replace-with-your-chat-id",
            })

    def test_credentials_have_a_secret_safe_representation(self):
        rendered = repr(self.credentials())
        self.assertNotIn(BOT_TOKEN, rendered)
        self.assertNotIn(CHAT_ID, rendered)
        self.assertEqual(
            self.credentials(),
            load_telegram_credentials({
                "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
                "TELEGRAM_CHAT_ID": CHAT_ID,
            }),
        )

    def test_success_uses_plain_text_and_returns_secret_free_receipt(self):
        sender = FakeSender([success(77)])
        receipt = TelegramNotifier(self.credentials(), sender=sender).send(self.notification())
        self.assertEqual({
            "provider": "telegram",
            "status": "sent",
            "attempts": 1,
            "message_id": 77,
        }, receipt.as_dict())
        self.assertEqual(BOT_TOKEN, sender.calls[0][0])
        self.assertEqual({"chat_id": CHAT_ID, "text": "Synthetic scan complete"}, sender.calls[0][1])
        self.assertNotIn("parse_mode", sender.calls[0][1])

    def test_rate_limit_honors_bounded_retry_after(self):
        sender = FakeSender([
            TelegramHttpResponse(429, json.dumps({
                "ok": False,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 99},
            })),
            success(),
        ])
        sleeps: list[float] = []
        receipt = TelegramNotifier(
            self.credentials(),
            sender=sender,
            policy=RetryPolicy(attempts=2, timeout_seconds=4, backoff_seconds=0.25),
            sleep=sleeps.append,
            retry_after_cap_seconds=30,
        ).send(self.notification())
        self.assertEqual(2, receipt.attempts)
        self.assertEqual([30], sleeps)

    def test_timeout_retries_with_exponential_backoff(self):
        sender = FakeSender([TimeoutError("temporary timeout"), OSError("temporary outage"), success()])
        sleeps: list[float] = []
        receipt = TelegramNotifier(
            self.credentials(),
            sender=sender,
            policy=RetryPolicy(attempts=3, timeout_seconds=2, backoff_seconds=0.5),
            sleep=sleeps.append,
        ).send(self.notification())
        self.assertEqual(3, receipt.attempts)
        self.assertEqual([0.5, 1.0], sleeps)
        self.assertTrue(all(call[2] == 2 for call in sender.calls))

    def test_transient_server_status_retries(self):
        sender = FakeSender([
            TelegramHttpResponse(503, json.dumps({"ok": False, "description": "unavailable"})),
            success(),
        ])
        sleeps: list[float] = []
        receipt = TelegramNotifier(
            self.credentials(),
            sender=sender,
            policy=RetryPolicy(attempts=2, timeout_seconds=2, backoff_seconds=0.25),
            sleep=sleeps.append,
        ).send(self.notification())
        self.assertEqual(2, receipt.attempts)
        self.assertEqual([0.25], sleeps)

    def test_terminal_failure_does_not_retry_and_redacts_secrets(self):
        sender = FakeSender([TelegramHttpResponse(400, json.dumps({
            "ok": False,
            "description": f"bad token {BOT_TOKEN} for chat {CHAT_ID}",
        }))])
        with self.assertRaises(NotificationError) as caught:
            TelegramNotifier(self.credentials(), sender=sender).send(self.notification())
        rendered = str(caught.exception)
        self.assertEqual(1, len(sender.calls))
        self.assertNotIn(BOT_TOKEN, rendered)
        self.assertNotIn(CHAT_ID, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_exhausted_transport_error_is_bounded_and_redacted(self):
        sender = FakeSender([OSError(f"endpoint bot{BOT_TOKEN} chat={CHAT_ID}")])
        with self.assertRaises(NotificationError) as caught:
            TelegramNotifier(
                self.credentials(),
                sender=sender,
                policy=RetryPolicy(attempts=1, timeout_seconds=1, backoff_seconds=0),
            ).send(self.notification())
        rendered = str(caught.exception)
        self.assertNotIn(BOT_TOKEN, rendered)
        self.assertNotIn(CHAT_ID, rendered)
        self.assertLessEqual(len(rendered), 320)

    def test_unexpected_sender_failure_is_redacted_without_retry(self):
        sender = FakeSender([RuntimeError(f"bad callback {BOT_TOKEN} {CHAT_ID}")])
        with self.assertRaises(NotificationError) as caught:
            TelegramNotifier(self.credentials(), sender=sender).send(self.notification())
        rendered = str(caught.exception)
        self.assertEqual(1, len(sender.calls))
        self.assertNotIn(BOT_TOKEN, rendered)
        self.assertNotIn(CHAT_ID, rendered)

    def test_malformed_success_envelopes_are_rejected(self):
        for response in (
            TelegramHttpResponse(200, "not-json"),
            TelegramHttpResponse(200, "[]"),
            TelegramHttpResponse(200, json.dumps({"ok": False, "description": "rejected"})),
            TelegramHttpResponse(200, json.dumps({"ok": True, "result": {}})),
        ):
            with self.subTest(body=response.body):
                with self.assertRaises(NotificationError):
                    TelegramNotifier(self.credentials(), sender=FakeSender([response])).send(self.notification())

    def test_standard_sender_uses_only_the_fixed_telegram_endpoint(self):
        captured: dict[str, object] = {}

        class Response:
            status = 200
            headers = {}

            def read(self, limit):  # type: ignore[no-untyped-def]
                del limit
                return json.dumps({"ok": True}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False

        class Opener:
            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        with (
            patch("role_monitor.telegram.validate_public_https_url") as validate,
            patch("role_monitor.telegram.build_opener", return_value=Opener()),
        ):
            response = UrllibTelegramSender().post(
                BOT_TOKEN,
                {"chat_id": CHAT_ID, "text": "plain text"},
                timeout=5,
            )
        request = captured["request"]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual({"chat_id": CHAT_ID, "text": "plain text"}, payload)
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(5, captured["timeout"])
        self.assertEqual(200, response.status)
        validate.assert_called_once_with("https://api.telegram.org")


if __name__ == "__main__":
    unittest.main()
