# Security policy

## Supported version

Security fixes are applied to the latest `1.x` release.

## Reporting a vulnerability

Please use GitHub's private security-advisory workflow rather than opening a public issue. Include the affected command or module, a minimal reproduction using synthetic data, and the likely impact. Do not include real credentials, personal job-search records, or production infrastructure details.

## Security boundaries

- Version 1.1 reads public job listings and never submits applications.
- The default demo is offline and uses synthetic employers and payloads.
- A role cannot be handed off until a human explicitly approves it.
- A handoff is marked complete only after an exact, verified receipt.
- A stranded delivery claim requires downstream reconciliation and an explicit recovery confirmation before retry.
- Repository configuration rejects authentication-shaped fields. Optional Telegram credentials are read only from `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the process environment.
- External error text is bounded and redacted before it is persisted or displayed.
- Live discovery requires explicit network opt-in, accepts only HTTPS URLs without embedded credentials, and rejects literal or resolved non-public destinations, including redirect targets.
- SQLite state and generated reports are created with owner-only file permissions. Shareable reports use explicit field allowlists and omit private reviewer notes.
- Telegram is disabled by default and requires an explicit CLI flag. Its fixed-host sender uses plain text, a bounded summary-only template, mocked tests, capped retries, and no configurable API endpoint.
- Telegram dry-run mode does not read credentials or contact a network. A live-notification failure returns a redacted nonzero result only after scan state and reports are safely persisted.
- Notification delivery is observational and at-least-once. It cannot approve roles or acknowledge handoffs, and an ambiguous timeout may produce a duplicate digest rather than changing pipeline state.

Redaction is defense in depth, not a reason to place secrets in source URLs, configuration, exceptions, fixtures, command arguments, or logs. Never commit or paste a Telegram token or chat ID into an issue. The dependency-free standard-library transport validates DNS results before opening each request, but it cannot pin that result through the TLS connection; configured and redirected hostnames are therefore assumed not to perform DNS rebinding, and DNS resolution itself is not covered by the socket timeout.

The project cannot guarantee the accuracy, availability, or continued compatibility of third-party career sites. Operators remain responsible for vendor terms, request rates, source trust, and the decisions made from discovered listings.
