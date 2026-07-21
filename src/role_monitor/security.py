"""Small, dependency-free helpers for keeping operational output credential-safe."""

from __future__ import annotations

import re
from typing import Iterable

from .models import compact_text


REDACTION = "[REDACTED]"
_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic|token|api[_-]?key|digest)\s+)?[^\s,;]+"),
    re.compile(r"(?i)((?:set-)?cookie\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)\b(api[_-]?key|x[_-]?api[_-]?key|token|access[_-]?token|auth[_-]?token|refresh[_-]?token|oauth[_-]?token|id[_-]?token|session[_-]?(?:token|id)|client[_-]?secret|private[_-]?key|secret|password|signature)\b(\s*[:=]\s*)[^\s,;&]+"),
    re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|client[_-]?secret|secret|password|signature)=)[^&#\s]+"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_/-]+", re.IGNORECASE),
    re.compile(r"https://api\.telegram\.org/bot[^/\s]+", re.IGNORECASE),
)


def redact_text(value: object, secret_values: Iterable[str] = ()) -> str:
    """Redact common credential shapes and explicitly supplied canary values."""

    result = str(value or "")
    for secret in sorted({str(item) for item in secret_values if len(str(item)) >= 4}, key=len, reverse=True):
        result = result.replace(secret, REDACTION)
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTION}", result)
        elif pattern.groups == 1:
            result = pattern.sub(lambda match: f"{match.group(1)}{REDACTION}", result)
        else:
            result = pattern.sub(REDACTION, result)
    return result


def safe_error(error: BaseException | object, secret_values: Iterable[str] = (), limit: int = 300) -> str:
    """Return one bounded line that is safe to persist or display."""

    if isinstance(error, BaseException):
        raw = f"{error.__class__.__name__}: {error}"
    else:
        raw = str(error)
    return compact_text(redact_text(raw, secret_values))[:limit]
