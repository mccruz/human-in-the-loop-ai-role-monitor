"""Offline parsing helpers for documented public careers feeds.

The caller owns fetching.  This module only maps known public careers URLs to
their JSON feed endpoints and converts already-fetched payloads into the
provider-neutral :class:`~role_monitor.models.JobPosting` model.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from .models import JobPosting, canonical_url, compact_text


_SAFE_SCHEMES = {"https"}
_JOB_LINK_WORDS = re.compile(r"\b(job|jobs|career|careers|opening|openings|position|positions)\b", re.I)
_RECRUITEE_RESERVED_SUBDOMAINS = {"api", "docs", "support", "www"}


@dataclass(frozen=True, slots=True)
class FeedRequest:
    """A public endpoint selected from a verified careers hostname."""

    adapter: str
    url: str


def _parts(value: str) -> tuple[str, list[str]] | None:
    try:
        parsed = urlsplit(compact_text(value))
    except ValueError:
        return None
    if parsed.scheme.lower() not in _SAFE_SCHEMES or not parsed.hostname:
        return None
    return parsed.hostname.lower().rstrip("."), [part for part in parsed.path.split("/") if part]


def _segment(parts: list[str]) -> str | None:
    if not parts or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]):
        return None
    return parts[0]


def resolve_feed(careers_url: str) -> FeedRequest | None:
    """Map an official ATS careers URL to its documented public JSON endpoint.

    Exact hostname matching is intentional: a URL that merely *contains* an
    ATS domain is not an authority boundary and is rejected.
    """

    result = _parts(careers_url)
    if result is None:
        return None
    host, parts = result
    # Recruitee hosts each public careers site on one subdomain, so there is
    # no required path token.  Do not allow nested lookalikes such as
    # company.recruitee.com.attacker.example.
    recruitee_match = re.fullmatch(r"([a-z0-9-]+)\.recruitee\.com", host)
    if recruitee_match and recruitee_match.group(1) not in _RECRUITEE_RESERVED_SUBDOMAINS:
        return FeedRequest("recruitee", f"https://{host}/api/offers/")
    token = _segment(parts)
    if not token:
        return None
    encoded = quote(token, safe="")
    if host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        return FeedRequest("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{encoded}/jobs?content=true")
    if host == "jobs.lever.co":
        return FeedRequest("lever", f"https://api.lever.co/v0/postings/{encoded}?mode=json")
    if host == "jobs.eu.lever.co":
        return FeedRequest("lever", f"https://api.eu.lever.co/v0/postings/{encoded}?mode=json")
    if host == "jobs.ashbyhq.com":
        return FeedRequest("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{encoded}")
    if host == "careers.smartrecruiters.com":
        return FeedRequest("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{encoded}/postings?limit=100&offset=0")
    if host == "apply.workable.com":
        return FeedRequest("workable", f"https://www.workable.com/api/accounts/{encoded}?details=true")
    return None


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return compact_text(value.get("name") or value.get("label") or value.get("title"))
    if isinstance(value, list):
        return ", ".join(filter(None, (_text(item) for item in value)))
    return compact_text(value)


def _location_text(value: Any) -> str:
    """Normalize the common public-ATS location object shapes."""

    if isinstance(value, list):
        return "; ".join(filter(None, (_location_text(item) for item in value)))
    if not isinstance(value, dict):
        return compact_text(value)
    direct = compact_text(
        value.get("fullLocation")
        or value.get("location_str")
        or value.get("full_address")
        or value.get("name")
        or value.get("label")
    )
    if direct:
        return direct
    if isinstance(value.get("address"), dict):
        nested = _location_text(value["address"])
        if nested:
            return nested
    parts = (
        value.get("city") or value.get("addressLocality"),
        value.get("region") or value.get("state") or value.get("state_name") or value.get("addressRegion"),
        value.get("country_name") or value.get("country") or value.get("addressCountry"),
    )
    return ", ".join(dict.fromkeys(filter(None, (compact_text(part) for part in parts))))


def _location(adapter: str, item: dict[str, Any]) -> str:
    value: Any = _first(item, "location", "locations", "locationName", "location_name")
    if adapter == "lever" and not value and isinstance(item.get("categories"), dict):
        value = item["categories"].get("location")
    return _location_text(value)


def _employment_type(adapter: str, item: dict[str, Any]) -> str:
    value: Any = _first(
        item,
        "employment_type",
        "employment_type_code",
        "employmentType",
        "type",
        "typeOfEmployment",
    )
    if adapter == "lever" and not value and isinstance(item.get("categories"), dict):
        value = item["categories"].get("commitment")
    raw = _text(value)
    normalized = re.sub(r"[^a-z0-9]+", "", raw.casefold())
    canonical = {
        "fulltime": "full-time",
        "fulltimepermanent": "full-time",
        "parttime": "part-time",
        "parttimepermanent": "part-time",
        "contractor": "contract",
        "temporary": "temporary",
        "temp": "temporary",
        "internship": "internship",
        "intern": "internship",
    }
    return canonical.get(normalized, raw.casefold())


def _workplace_type(adapter: str, item: dict[str, Any]) -> str:
    location = item.get("location")
    candidates: list[Any] = [item.get("workplaceType"), item.get("workplace_type")]
    if adapter == "recruitee":
        remote = item.get("remote")
        hybrid = item.get("hybrid")
        on_site = item.get("on_site")
        if hybrid is True or (remote is True and on_site is True):
            return "hybrid"
        if remote is True:
            return "remote"
        if on_site is True:
            return "on-site"
    if isinstance(location, dict):
        if location.get("hybrid") is True:
            return "hybrid"
        if location.get("remote") is True or location.get("telecommuting") is True:
            return "remote"
        candidates.extend((location.get("workplace_type"), location.get("workplaceType")))
    for value in candidates:
        normalized = re.sub(r"[^a-z]+", "", compact_text(value).casefold())
        if normalized in {"remote", "telecommute", "telecommuting"}:
            return "remote"
        if normalized == "hybrid":
            return "hybrid"
        if normalized in {"onsite", "office"}:
            return "on-site"
    explicit = "" if adapter == "recruitee" else _first(item, "remote", "isRemote")
    if isinstance(explicit, bool):
        return "remote" if explicit else "on-site"
    if isinstance(location, dict):
        for key in ("remote", "telecommuting"):
            if isinstance(location.get(key), bool):
                return "remote" if location[key] else "on-site"
    location_text = _location(adapter, item).casefold()
    if re.search(r"\bhybrid\b", location_text):
        return "hybrid"
    if re.search(r"\bremote\b", location_text):
        return "remote"
    return "unknown"


def _description(adapter: str, item: dict[str, Any]) -> str:
    value = _first(item, "content", "descriptionPlain", "description", "descriptionHtml")
    if not value and adapter == "smartrecruiters":
        job_ad = item.get("jobAd")
        sections = job_ad.get("sections") if isinstance(job_ad, dict) else None
        if isinstance(sections, dict):
            value = " ".join(
                compact_text(section.get("text"))
                for section in sections.values()
                if isinstance(section, dict) and compact_text(section.get("text"))
            )
    return _text(value)


def needs_smartrecruiters_detail(item: Mapping[str, Any]) -> bool:
    """Return whether a public list record needs its documented detail ref."""

    return not _description("smartrecruiters", dict(item)) and bool(_url(item.get("ref")))


def _smartrecruiters_public_url(item: dict[str, Any]) -> str:
    """Build the documented public job route without exposing the API ref URL."""

    identifier = ""
    company = item.get("company")
    if isinstance(company, dict):
        identifier = compact_text(company.get("identifier"))
    posting_id = compact_text(_first(item, "id", "uuid"))
    if not identifier:
        ref = _url(item.get("ref"))
        parts = [part for part in urlsplit(ref).path.split("/") if part]
        try:
            company_index = parts.index("companies")
            if parts[company_index + 2] == "postings":
                identifier = compact_text(parts[company_index + 1])
        except (ValueError, IndexError):
            pass
    if not identifier or not posting_id or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
        return ""
    return f"https://jobs.smartrecruiters.com/{quote(identifier, safe='')}/{quote(posting_id, safe='')}"


def _url(value: Any) -> str:
    value = compact_text(value)
    parsed = urlsplit(value)
    return value if parsed.scheme.lower() in _SAFE_SCHEMES and parsed.hostname else ""


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, "", [], {}):
            return item[key]
    return ""


def _posting(adapter: str, item: dict[str, Any], company: str, *, url_keys: tuple[str, ...] = ("absolute_url", "hostedUrl", "url")) -> JobPosting | None:
    title = _text(_first(item, "title", "name", "text"))
    url = _url(_first(item, *url_keys))
    if adapter == "smartrecruiters" and not url:
        url = _smartrecruiters_public_url(item)
    if not title or not url or not compact_text(company):
        return None
    try:
        workplace_type = _workplace_type(adapter, item)
        return JobPosting(
            source=adapter,
            external_id=_text(_first(item, "id", "shortcode", "jobId", "slug", "reference")),
            company=compact_text(company),
            title=title,
            url=url,
            location=_location(adapter, item),
            description=_description(adapter, item),
            employment_type=_employment_type(adapter, item),
            remote=True if workplace_type == "remote" else (False if workplace_type in {"hybrid", "on-site"} else None),
            workplace_type=workplace_type,
        )
    except (TypeError, ValueError):
        return None


def _records(adapter: str, payload: Any) -> Iterable[dict[str, Any]]:
    if adapter == "greenhouse" and isinstance(payload, dict):
        return payload.get("jobs", [])
    if adapter == "lever" and isinstance(payload, list):
        return payload
    if adapter == "ashby" and isinstance(payload, dict):
        return payload.get("jobs", [])
    if adapter == "smartrecruiters" and isinstance(payload, dict):
        return payload.get("content", [])
    if adapter == "workable" and isinstance(payload, dict):
        return payload.get("results", payload.get("jobs", []))
    if adapter == "recruitee" and isinstance(payload, dict):
        return payload.get("offers", [])
    return []


def validate_feed_payload(adapter: str, payload: Any) -> None:
    """Reject provider-wrong success bodies before a source is called complete."""

    adapter = compact_text(adapter).casefold()
    if adapter == "lever":
        valid = isinstance(payload, list)
    else:
        expected_key = {
            "greenhouse": "jobs",
            "ashby": "jobs",
            "smartrecruiters": "content",
            "workable": "jobs" if isinstance(payload, dict) and "jobs" in payload else "results",
            "recruitee": "offers",
        }.get(adapter)
        valid = (
            expected_key is not None
            and isinstance(payload, dict)
            and isinstance(payload.get(expected_key), list)
        )
    if not valid:
        raise ValueError(f"Invalid {adapter or 'unknown'} feed envelope")
    records = list(_records(adapter, payload))
    for item in records:
        if not isinstance(item, dict):
            raise ValueError(f"Invalid {adapter} feed record")
        if adapter == "ashby" and item.get("isListed") is False:
            continue
        keys = (
            ("applyUrl", "postingUrl", "url")
            if adapter == "smartrecruiters"
            else ("absolute_url", "hostedUrl", "jobUrl", "url", "careers_url", "application_url", "apply_url")
        )
        if _posting(adapter, item, "Validation Employer", url_keys=keys) is None:
            raise ValueError(f"Invalid {adapter} feed record")
    if adapter == "smartrecruiters":
        try:
            offset = int(payload["offset"])
            limit = int(payload["limit"])
            total = int(payload["totalFound"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid smartrecruiters pagination metadata") from None
        content = payload["content"]
        if offset < 0 or limit < 1 or total < 0 or len(content) > limit:
            raise ValueError("Invalid smartrecruiters pagination metadata")
        if total > offset and not content:
            raise ValueError("SmartRecruiters returned an empty page before the result set ended")


def validate_smartrecruiters_detail(
    payload: Any,
    *,
    expected_id: str = "",
    expected_company: str = "",
) -> None:
    """Validate the documented PostingDetails object used for full-text scoring."""

    if not isinstance(payload, dict):
        raise ValueError("Invalid SmartRecruiters detail record")
    if _posting(
        "smartrecruiters",
        payload,
        "Validation Employer",
        url_keys=("applyUrl", "postingUrl", "url"),
    ) is None:
        raise ValueError("Invalid SmartRecruiters detail record")
    company = payload.get("company")
    identifier = compact_text(company.get("identifier")) if isinstance(company, dict) else ""
    if expected_id and compact_text(_first(payload, "id", "uuid")) != compact_text(expected_id):
        raise ValueError("SmartRecruiters detail id does not match its list record")
    if expected_company and identifier.casefold() != compact_text(expected_company).casefold():
        raise ValueError("SmartRecruiters detail company does not match its list record")


def next_feed_request(request: FeedRequest, payload: Any) -> FeedRequest | None:
    """Return the next documented SmartRecruiters page, when one exists.

    SmartRecruiters exposes ``offset``, ``limit``, and ``totalFound`` in its
    public list response. Other feeds intentionally return ``None`` because
    their cursor semantics are provider-specific.
    """

    if request.adapter != "smartrecruiters" or not isinstance(payload, dict):
        return None
    try:
        offset = int(payload.get("offset", 0))
        limit = int(payload.get("limit", 100))
        total_found = int(payload["totalFound"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit <= 0 or offset < 0 or total_found <= offset + limit:
        return None
    parsed = urlsplit(request.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["limit"] = str(limit)
    query["offset"] = str(offset + limit)
    return FeedRequest(request.adapter, urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


def parse_feed(adapter: str, payload: Any, company: str) -> list[JobPosting]:
    """Parse a JSON payload already obtained from a supported public feed."""

    adapter = compact_text(adapter).casefold()
    if adapter not in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee"}:
        return []
    results: list[JobPosting] = []
    seen: set[str] = set()
    seen_urls: set[str] = set()
    for item in _records(adapter, payload):
        if not isinstance(item, dict):
            continue
        if adapter == "ashby" and item.get("isListed") is False:
            continue
        # SmartRecruiters' API reference URL is not the public application
        # page; prefer its public fields and use `ref` only as a last resort.
        keys = (
            ("applyUrl", "postingUrl", "url")
            if adapter == "smartrecruiters"
            else ("absolute_url", "hostedUrl", "jobUrl", "url", "careers_url", "application_url", "apply_url")
        )
        posting = _posting(adapter, item, company, url_keys=keys)
        if posting and posting.role_key not in seen and canonical_url(posting.url) not in seen_urls:
            seen.add(posting.role_key)
            seen_urls.add(canonical_url(posting.url))
            results.append(posting)
    return results


class _GenericPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_ld: list[str] = []
        self.links: list[tuple[str, str, str]] = []
        self._json_depth = 0
        self._json_parts: list[str] = []
        self._anchor: tuple[str, str] | None = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "script" and "ld+json" in values.get("type", "").casefold():
            self._json_depth += 1
            self._json_parts = []
        elif tag.casefold() == "a" and values.get("href"):
            self._anchor = (values["href"], values.get("title", ""))
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._json_depth:
            self._json_depth -= 1
            if not self._json_depth:
                self.json_ld.append("".join(self._json_parts))
        elif tag.casefold() == "a" and self._anchor:
            href, title = self._anchor
            self.links.append((href, title, "".join(self._anchor_parts)))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._json_depth:
            self._json_parts.append(data)
        if self._anchor:
            self._anchor_parts.append(data)


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.get("@graph", []), value.get("itemListElement", []), value.get("item", []):
            yield from _walk_json(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _jsonld_posting(item: dict[str, Any], final_url: str, company: str) -> JobPosting | None:
    kind = item.get("@type", "")
    if isinstance(kind, list):
        kind = " ".join(map(str, kind))
    if "jobposting" not in str(kind).casefold():
        return None
    employer = item.get("hiringOrganization", {})
    employer_name = _text(employer) or compact_text(company)
    location = _location_text(item.get("jobLocation"))
    location_type = _text(item.get("jobLocationType")).casefold()
    remote = True if "telecommute" in location_type else None
    identifier = item.get("identifier", "")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name") or ""
    data = {
        "title": item.get("title"),
        "url": urljoin(final_url, _text(item.get("url"))),
        "id": identifier,
        "description": item.get("description"),
        "employmentType": item.get("employmentType"),
        "location": location,
        "remote": remote,
        "workplaceType": "remote" if remote is True else "",
    }
    return _posting("generic", data, employer_name)


def parse_generic_html(body: str, final_url: str, company: str) -> list[JobPosting]:
    """Extract structured job postings and safe role links from an HTML page."""

    if not _url(final_url) or not isinstance(body, str):
        return []
    parser = _GenericPageParser()
    try:
        parser.feed(body)
        parser.close()
    except (ValueError, TypeError):
        return []
    results: list[JobPosting] = []
    seen: set[str] = set()
    seen_urls: set[str] = set()

    def add(posting: JobPosting | None) -> None:
        if posting and posting.role_key not in seen and canonical_url(posting.url) not in seen_urls:
            seen.add(posting.role_key)
            seen_urls.add(canonical_url(posting.url))
            results.append(posting)

    for raw in parser.json_ld:
        try:
            document = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for item in _walk_json(document):
            add(_jsonld_posting(item, final_url, company))
    for href, title, text in parser.links:
        url = urljoin(final_url, href)
        if not _url(url) or not _JOB_LINK_WORDS.search(f"{href} {title} {text}"):
            continue
        add(_posting("generic", {"title": title or text, "url": url}, company))
    return results
