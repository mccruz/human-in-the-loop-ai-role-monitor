"""HTTP transport and bounded retry policy with injectable test doubles."""

from __future__ import annotations

from dataclasses import dataclass
import json
import ipaddress
import socket
import threading
import time
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import canonical_url, require_public_hostname


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    url: str
    body: str
    content_type: str = "application/json"


class HttpTransport(Protocol):
    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> HttpResponse:
        """Fetch one URL or raise a transport exception."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    timeout_seconds: float = 15.0
    backoff_seconds: float = 0.25
    retry_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("Retry attempts must be at least one")
        if self.timeout_seconds <= 0 or self.backoff_seconds < 0:
            raise ValueError("Retry timeout must be positive and backoff cannot be negative")


@dataclass(frozen=True, slots=True)
class FetchResult:
    response: HttpResponse
    attempts: int


class UrllibTransport:
    """Minimal public-feed client that deliberately omits credential support."""

    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> HttpResponse:
        safe_url = validate_public_https_url(url)
        request = Request(safe_url, headers=dict(headers), method="GET")
        try:
            opener = build_opener(_PublicRedirectHandler())
            with opener.open(request, timeout=timeout) as response:
                validate_public_https_url(response.geturl())
                body = response.read(5_000_000).decode(response.headers.get_content_charset() or "utf-8", "replace")
                return HttpResponse(
                    status=response.status,
                    url=response.geturl(),
                    body=body,
                    content_type=response.headers.get_content_type(),
                )
        except HTTPError as error:
            return HttpResponse(status=error.code, url=error.geturl() or url, body="", content_type="text/plain")
        except (URLError, TimeoutError, OSError):
            raise


def validate_public_https_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate a live fetch target as HTTPS and globally routable."""

    normalized = canonical_url(url)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    require_public_hostname(hostname)
    if not resolve_dns:
        return normalized
    try:
        literal = ipaddress.ip_address(hostname)
        addresses = [literal]
    except ValueError:
        try:
            answers = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise OSError(f"Public source hostname could not be resolved: {hostname}") from error
        addresses = []
        for answer in answers:
            try:
                addresses.append(ipaddress.ip_address(answer[4][0]))
            except ValueError:
                raise OSError("Public source resolved to an invalid address") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise OSError("Public source resolved to a non-public network address")
    return normalized


class _PublicRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_public_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_with_retry(
    transport: HttpTransport,
    url: str,
    policy: RetryPolicy,
    *,
    user_agent: str,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchResult:
    """Retry transient statuses and transport exceptions with exponential backoff."""

    headers = {
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
        "User-Agent": user_agent,
    }
    last_error: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            response = transport.get(url, timeout=policy.timeout_seconds, headers=headers)
            if response.status not in policy.retry_statuses or attempt == policy.attempts:
                return FetchResult(response=response, attempts=attempt)
        except (TimeoutError, OSError, URLError) as error:
            last_error = error
            if attempt == policy.attempts:
                raise
        if policy.backoff_seconds:
            sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
    if last_error:
        raise last_error
    raise RuntimeError("Retry loop exited without a response")


class MappingTransport:
    """Thread-safe synthetic transport for demos and tests."""

    def __init__(self, responses: Mapping[str, HttpResponse | BaseException | Sequence[HttpResponse | BaseException]]):
        self._responses: dict[str, list[HttpResponse | BaseException]] = {}
        for url, value in responses.items():
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                sequence = list(value)
            else:
                sequence = [value]  # type: ignore[list-item]
            if not sequence:
                raise ValueError(f"Synthetic response sequence is empty for {url}")
            self._responses[url] = sequence
        self.calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def get(self, url: str, *, timeout: float, headers: Mapping[str, str]) -> HttpResponse:
        del timeout, headers
        with self._lock:
            if url not in self._responses:
                raise URLError(f"No synthetic response configured for {url}")
            self.calls[url] = self.calls.get(url, 0) + 1
            sequence = self._responses[url]
            value = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if isinstance(value, BaseException):
            raise value
        return value


def json_response(url: str, payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, url=url, body=json.dumps(payload), content_type="application/json")
