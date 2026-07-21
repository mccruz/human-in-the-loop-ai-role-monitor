# Contributing

Contributions that improve reliability, accessibility, documentation, or documented public-feed compatibility are welcome.

## Development checks

```bash
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
role-monitor demo --output-dir demo-output --reset --telegram-dry-run
```

## Data and security rules

- Use fictional employers, roles, URLs, identifiers, and receipts in fixtures.
- Never commit credentials, personal application records, private schemas, production paths, notification destinations, or copied vendor payloads.
- Telegram changes must keep the endpoint fixed, credentials environment-only, error text redacted, and all tests sender-injected. Do not add a real token or chat ID, even temporarily.
- Base native integrations on documented public endpoints. Do not add a reverse-engineered private endpoint as if it were supported API behavior.
- Add domain-boundary, malformed-response, retry, and failure-isolation tests for feed changes.
- Preserve human decisions and verified handoff state during migrations and rescans.
