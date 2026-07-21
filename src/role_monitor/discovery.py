"""Concurrent ATS discovery with per-source retries and failure isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
import json
from typing import Iterable
from urllib.parse import urlsplit

from .feeds import (
    needs_smartrecruiters_detail,
    next_feed_request,
    parse_feed,
    parse_generic_html,
    resolve_feed,
    validate_feed_payload,
    validate_smartrecruiters_detail,
)
from .http import HttpTransport, RetryPolicy, fetch_with_retry
from .models import Employer, JobPosting, ScanFailure, canonical_url
from .security import safe_error


DEFAULT_USER_AGENT = "human-in-the-loop-ai-role-monitor/1.0 (+public portfolio demo)"


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    employer: Employer
    adapter: str
    checked_url: str
    jobs: tuple[JobPosting, ...]
    attempts: int
    complete: bool
    failure: ScanFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


def discover_one(
    employer: Employer,
    transport: HttpTransport,
    retry_policy: RetryPolicy,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> DiscoveryResult:
    request = resolve_feed(employer.careers_url)
    adapter = request.adapter if request else "generic"
    requested_url = request.url if request else employer.careers_url
    attempts = 0
    try:
        if request:
            jobs: list[JobPosting] = []
            seen_keys: set[str] = set()
            seen_urls: set[str] = set()
            page_request = request
            visited_pages: set[str] = set()
            response_url = requested_url
            for _page_number in range(100):
                if page_request.url in visited_pages:
                    raise RuntimeError("ATS pagination cycle detected")
                visited_pages.add(page_request.url)
                fetched = fetch_with_retry(
                    transport,
                    page_request.url,
                    retry_policy,
                    user_agent=user_agent,
                )
                attempts += fetched.attempts
                response = fetched.response
                response_url = response.url
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"HTTP {response.status}")
                payload = json.loads(response.body)
                validate_feed_payload(adapter, payload)
                parse_payload = payload
                if adapter == "smartrecruiters":
                    hydrated: list[dict[str, object]] = []
                    for item in payload["content"]:
                        if needs_smartrecruiters_detail(item):
                            detail_url = _smartrecruiters_detail_url(item, page_request.url)
                            detail_fetch = fetch_with_retry(
                                transport,
                                detail_url,
                                retry_policy,
                                user_agent=user_agent,
                            )
                            attempts += detail_fetch.attempts
                            if not 200 <= detail_fetch.response.status < 300:
                                raise RuntimeError(f"HTTP {detail_fetch.response.status} for SmartRecruiters detail")
                            detail = json.loads(detail_fetch.response.body)
                            list_path = [part for part in urlsplit(page_request.url).path.split("/") if part]
                            validate_smartrecruiters_detail(
                                detail,
                                expected_id=str(item.get("id") or item.get("uuid") or ""),
                                expected_company=list_path[2],
                            )
                            hydrated.append(detail)
                        else:
                            hydrated.append(item)
                    parse_payload = {**payload, "content": hydrated}
                for parsed_posting in parse_feed(adapter, parse_payload, employer.name):
                    posting = replace(parsed_posting, employer_key=employer.key)
                    posting_url = canonical_url(posting.url)
                    if posting.role_key not in seen_keys and posting_url not in seen_urls:
                        seen_keys.add(posting.role_key)
                        seen_urls.add(posting_url)
                        jobs.append(posting)
                following = next_feed_request(page_request, payload)
                if following is None:
                    break
                page_request = following
            else:
                raise RuntimeError("ATS pagination exceeded 100 pages")
            complete = True
        else:
            fetched = fetch_with_retry(
                transport,
                requested_url,
                retry_policy,
                user_agent=user_agent,
            )
            attempts = fetched.attempts
            response = fetched.response
            response_url = response.url
            if not 200 <= response.status < 300:
                raise RuntimeError(f"HTTP {response.status}")
            jobs = [
                replace(posting, employer_key=employer.key)
                for posting in parse_generic_html(response.body, response.url, employer.name)
            ]
            complete = False
        return DiscoveryResult(
            employer=employer,
            adapter=adapter,
            checked_url=response_url,
            jobs=tuple(jobs),
            attempts=attempts,
            complete=complete,
        )
    except Exception as error:
        if attempts == 0:
            attempts = retry_policy.attempts if isinstance(error, (TimeoutError, OSError)) else 1
        failure = ScanFailure(
            employer_key=employer.key,
            employer_name=employer.name,
            adapter=adapter,
            error=safe_error(error),
            attempts=attempts,
        )
        return DiscoveryResult(
            employer=employer,
            adapter=adapter,
            checked_url=requested_url,
            jobs=(),
            attempts=attempts,
            complete=False,
            failure=failure,
        )


def discover_all(
    employers: Iterable[Employer],
    transport: HttpTransport,
    retry_policy: RetryPolicy,
    *,
    max_workers: int = 6,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[DiscoveryResult]:
    """Scan every employer even if an individual future fails unexpectedly."""

    employer_list = list(employers)
    if max_workers < 1:
        raise ValueError("max_workers must be at least one")
    indexed = {employer.key: index for index, employer in enumerate(employer_list)}
    results: list[DiscoveryResult] = []
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="role-feed") as executor:
        futures = {
            executor.submit(discover_one, employer, transport, retry_policy, user_agent=user_agent): employer
            for employer in employer_list
        }
        for future in as_completed(futures):
            employer = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # Defensive: one worker must never abort the overall run.
                results.append(DiscoveryResult(
                    employer=employer,
                    adapter="unknown",
                    checked_url=employer.careers_url,
                    jobs=(),
                    attempts=1,
                    complete=False,
                    failure=ScanFailure(
                        employer_key=employer.key,
                        employer_name=employer.name,
                        adapter="unknown",
                        error=safe_error(error),
                        attempts=1,
                    ),
                ))
    return sorted(results, key=lambda item: indexed[item.employer.key])


def _smartrecruiters_detail_url(item: dict[str, object], list_url: str) -> str:
    """Accept only the documented detail ref inside the same company namespace."""

    ref = str(item.get("ref") or "")
    ref_parts = urlsplit(ref)
    list_parts = urlsplit(list_url)
    ref_path = [part for part in ref_parts.path.split("/") if part]
    list_path = [part for part in list_parts.path.split("/") if part]
    valid = (
        ref_parts.scheme == "https"
        and ref_parts.hostname == "api.smartrecruiters.com"
        and list_parts.hostname == "api.smartrecruiters.com"
        and len(ref_path) == 5
        and len(list_path) == 4
        and ref_path[0] in {"v1", "api-v1"}
        and ref_path[1:4] == list_path[1:4]
        and ref_path[-1] == str(item.get("id") or item.get("uuid") or "")
    )
    if not valid:
        raise ValueError("Invalid SmartRecruiters detail ref")
    return ref
