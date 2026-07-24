"""Optional Telegram notifier with environment-only credentials and safe retries."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import re
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .http import RetryPolicy, validate_public_https_url
from .models import compact_text
from .notifications import Notification, NotificationReceipt, Notifier
from .security import safe_error


TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
DEFAULT_TELEGRAM_RETRY = RetryPolicy(attempts=3, timeout_seconds=10.0, backoff_seconds=0.5)

_TOKEN_PATTERN = re.compile(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}")
_CHAT_PATTERN = re.compile(r"(?:-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})")
_PLACEHOLDER_TERMS = ("replace", "example", "placeholder", "your-")


class NotificationError(RuntimeError):
    """A bounded, credential-safe notification failure."""


@dataclass(frozen=True, slots=True, repr=False)
class TelegramCredentials:
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bot_token, str) or not _TOKEN_PATTERN.fullmatch(self.bot_token):
            raise ValueError(f"{TELEGRAM_BOT_TOKEN_ENV} has an invalid format")
        if not isinstance(self.chat_id, str) or not _CHAT_PATTERN.fullmatch(self.chat_id):
            raise ValueError(f"{TELEGRAM_CHAT_ID_ENV} has an invalid format")

    def __repr__(self) -> str:
        return "TelegramCredentials(bot_token='[REDACTED]', chat_id='[REDACTED]')"


def load_telegram_credentials(environment: Mapping[str, str] | None = None) -> TelegramCredentials:
    """Load credentials from a process environment, never a repository config."""

    source = os.environ if environment is None else environment
    token = str(source.get(TELEGRAM_BOT_TOKEN_ENV, "")).strip()
    chat_id = str(source.get(TELEGRAM_CHAT_ID_ENV, "")).strip()
    missing = [
        name
        for name, value in ((TELEGRAM_BOT_TOKEN_ENV, token), (TELEGRAM_CHAT_ID_ENV, chat_id))
        if not value
    ]
    if missing:
        raise ValueError("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the process environment")
    if any(term in token.casefold() or term in chat_id.casefold() for term in _PLACEHOLDER_TERMS):
        raise ValueError("Replace the Telegram environment placeholders before live notification")
    return TelegramCredentials(bot_token=token, chat_id=chat_id)


@dataclass(frozen=True, slots=True)
class TelegramHttpResponse:
    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)


class TelegramSender(Protocol):
    def post(
        self,
        bot_token: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> TelegramHttpResponse:
        """POST one fixed-host Telegram Bot API request."""


class UrllibTelegramSender:
    """Dependency-free sender whose destination cannot be reconfigured."""

    def post(
        self,
        bot_token: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> TelegramHttpResponse:
        validate_public_https_url(TELEGRAM_API_ROOT)
        endpoint = f"{TELEGRAM_API_ROOT}/bot{bot_token}/sendMessage"
        data = json.dumps(dict(payload), ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            endpoint,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "human-in-the-loop-ai-role-monitor/1.1",
            },
            method="POST",
        )
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = _read_bounded(response)
                return TelegramHttpResponse(
                    status=response.status,
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as error:
            return TelegramHttpResponse(
                status=error.code,
                body=_read_bounded(error),
                headers=dict(error.headers.items()) if error.headers else {},
            )


class TelegramNotifier(Notifier):
    """Send a plain-text digest with bounded retry and redacted failures."""

    def __init__(
        self,
        credentials: TelegramCredentials,
        sender: TelegramSender | None = None,
        policy: RetryPolicy = DEFAULT_TELEGRAM_RETRY,
        *,
        sleep: Callable[[float], None] = time.sleep,
        retry_after_cap_seconds: float = 30.0,
    ) -> None:
        if retry_after_cap_seconds < 0:
            raise ValueError("Telegram retry-after cap cannot be negative")
        self._credentials = credentials
        self._sender = sender or UrllibTelegramSender()
        self._policy = policy
        self._sleep = sleep
        self._retry_after_cap_seconds = retry_after_cap_seconds

    def send(self, notification: Notification) -> NotificationReceipt:
        payload = {
            "chat_id": self._credentials.chat_id,
            "text": notification.text,
        }
        secrets = (self._credentials.bot_token, self._credentials.chat_id)
        for attempt in range(1, self._policy.attempts + 1):
            try:
                response = self._sender.post(
                    self._credentials.bot_token,
                    payload,
                    timeout=self._policy.timeout_seconds,
                )
            except (TimeoutError, OSError, URLError) as error:
                if attempt < self._policy.attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                detail = safe_error(error, secrets, limit=240)
                raise NotificationError(
                    f"Telegram notification failed after {attempt} attempts: {detail}"
                ) from None
            except Exception as error:
                detail = safe_error(error, secrets, limit=240)
                raise NotificationError(f"Telegram notification failed: {detail}") from None

            if response.status in self._policy.retry_statuses and attempt < self._policy.attempts:
                self._sleep(self._retry_delay(response, attempt))
                continue
            if not 200 <= response.status < 300:
                detail = _response_detail(response, secrets)
                raise NotificationError(
                    f"Telegram notification failed after {attempt} attempts: HTTP {response.status}{detail}"
                ) from None

            decoded = _decode_response(response, secrets)
            if decoded.get("ok") is not True:
                detail = safe_error(decoded.get("description") or "Telegram returned ok=false", secrets, limit=200)
                raise NotificationError(f"Telegram notification failed: {detail}") from None
            result = decoded.get("result")
            message_id = result.get("message_id") if isinstance(result, Mapping) else None
            if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id < 1:
                raise NotificationError("Telegram notification returned no valid message id") from None
            return NotificationReceipt(
                provider="telegram",
                status="sent",
                attempts=attempt,
                message_id=message_id,
            )
        raise NotificationError("Telegram retry loop exited without a result")

    def _backoff(self, attempt: int) -> float:
        return self._policy.backoff_seconds * (2 ** (attempt - 1))

    def _retry_delay(self, response: TelegramHttpResponse, attempt: int) -> float:
        if response.status == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after is not None:
                return min(retry_after, self._retry_after_cap_seconds)
        return self._backoff(attempt)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _read_bounded(response: Any, limit: int = 1_000_000) -> str:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise OSError("Telegram response exceeded the size limit")
    return raw.decode("utf-8", "replace")


def _decode_response(response: TelegramHttpResponse, secrets: tuple[str, str]) -> Mapping[str, Any]:
    try:
        decoded = json.loads(response.body)
    except (TypeError, json.JSONDecodeError):
        raise NotificationError("Telegram notification returned malformed JSON") from None
    if not isinstance(decoded, Mapping):
        raise NotificationError("Telegram notification returned a non-object response") from None
    # Force one redaction pass before callers can ever surface provider text.
    if "description" in decoded:
        decoded = {**decoded, "description": safe_error(decoded["description"], secrets, limit=200)}
    return decoded


def _response_detail(response: TelegramHttpResponse, secrets: tuple[str, str]) -> str:
    try:
        decoded = json.loads(response.body)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(decoded, Mapping) or not decoded.get("description"):
        return ""
    return ": " + safe_error(decoded["description"], secrets, limit=160)


def _retry_after_seconds(response: TelegramHttpResponse) -> float | None:
    try:
        decoded = json.loads(response.body)
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    parameters = decoded.get("parameters") if isinstance(decoded, Mapping) else None
    candidate = parameters.get("retry_after") if isinstance(parameters, Mapping) else None
    if candidate is None:
        candidate = next(
            (value for key, value in response.headers.items() if key.casefold() == "retry-after"),
            None,
        )
    if isinstance(candidate, bool):
        return None
    try:
        seconds = float(candidate)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
