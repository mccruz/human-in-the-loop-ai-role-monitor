# Human-in-the-Loop AI Role Monitor

> A safe Python automation portfolio project that turns public ATS listings into a human-reviewed, auditable handoff queue for AI automation, implementation, and workflow-engineering roles.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml) [![License: MIT](https://img.shields.io/badge/License-MIT-0B7F5C.svg)](LICENSE) [![Tests](https://img.shields.io/badge/tests-unittest-2D6A4F.svg)](tests)

![A human-in-the-loop role-monitor pipeline: public ATS feeds flow through scoring and review to a verified handoff.](assets/social-preview.png)

Built to demonstrate the engineering judgment behind reliable AI-enabled automation: public-source discovery, clear decision boundaries, traceable state, and delivery verification. It is intentionally a **monitor and handoff system—not an application bot**.

**Start here:** [architecture](docs/architecture.md) · [provenance](docs/provenance.md) · [security boundaries](SECURITY.md) · [GitHub metadata](docs/github-metadata.md) · [recruiter discoverability checklist](docs/seo-checklist.md)

## What this demonstrates

- Six documented public ATS adapters: Greenhouse, Lever (global and EU), Ashby, SmartRecruiters (pagination and detail hydration), Workable, and Recruitee.
- Concurrent discovery with bounded retries and per-source failure isolation, so one unavailable career site does not discard other findings.
- Explainable, configuration-driven scoring; stable identity and deduplication; and persisted SQLite decision state.
- An explicit human decision: approve, reject, or defer before a role is eligible for handoff.
- Deterministic handoff manifests with exact receipt verification, plus atomic, redacted JSON and CSV reports.
- Optional summary-only Telegram scan digests with environment-only credentials, bounded retries, redacted failures, and an offline dry run.

## 60-second recruiter walkthrough

1. A scan normalizes public openings from multiple ATS providers into a common role record.
2. A transparent policy scores each role against configured terms and thresholds; the resulting queue remains reviewable rather than auto-acted on.
3. SQLite preserves identity, decisions, and audit history across rescans—changing a listing title does not silently reset a prior decision.
4. After a person approves a role, the system produces a deterministic manifest. Delivery is recognized only when the receiver returns an exact receipt for that manifest.

The result is a concrete example of human-in-the-loop automation for implementation-oriented work: automation handles repeatable collection and evidence; people retain the consequential decision.

## Quick start

Requires Python 3.11+ and Git. Clone the repository, enter its directory, and use a virtual environment so the project does not modify a system-managed Python installation:

```bash
git clone https://github.com/mccruz/human-in-the-loop-ai-role-monitor.git
cd human-in-the-loop-ai-role-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-deps -e .
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead. Do not use `--break-system-packages` with a Homebrew- or operating-system-managed Python installation.

Run the complete offline demonstration:

```bash
role-monitor demo --output-dir demo-output --reset
```

It creates a disposable workspace populated only with fictional employers, roles, listings, and receipts. The demo performs one clearly labelled synthetic approval, then proves that manifest and receipt verification work without contacting the network. Inspect `demo-output/reports/`, `demo-output/handoff_manifest.json`, and `demo-output/handoff_receipt.json`.

Preview the optional Telegram digest without credentials or a network request:

```bash
role-monitor demo --output-dir demo-output --reset --telegram-dry-run
```

### Try the manual review flow offline

The demo leaves additional fictional roles pending in its disposable database. This lets you exercise queue, review, and handoff behavior without creating a live configuration or making a network request:

```bash
role-monitor queue \
  --config config.example.json \
  --database demo-output/demo_roles.sqlite3
```

Copy one exact `role_key` value from the `pending` list, assign it below in place of `paste-the-role-key-here`, and then approve only that fictional role:

```bash
ROLE_KEY='paste-the-role-key-here'

role-monitor review \
  --config config.example.json \
  --database demo-output/demo_roles.sqlite3 \
  "$ROLE_KEY" approved \
  --note "Reviewed manually"

role-monitor prepare-handoff \
  --config config.example.json \
  --database demo-output/demo_roles.sqlite3 \
  --destination demo-output/manual-handoff.json
```

`prepare-handoff` includes only fresh roles that a person explicitly approved. It fails safely when no approved, undelivered role is available.

### Configure live discovery

Live discovery needs a local configuration that is deliberately excluded from Git. Create it first:

```bash
cp config.example.json config.local.json
```

Open `config.local.json` in your editor before scanning. Every employer name and URL in the example is fictional, so replace or remove those entries and keep at least one documented public careers-page URL. Do not add credentials or private data to this file.

Then run the scan and inspect the queue before making any decision:

```bash
role-monitor scan --config config.local.json --allow-network
role-monitor queue --config config.local.json
```

The scan output distinguishes successful sources from per-source failures. The queue can legitimately be empty when no discovered role meets the configured review threshold. When it contains a role you have actually reviewed, copy its exact `role_key` and use it explicitly:

```bash
ROLE_KEY='paste-the-role-key-here'
role-monitor review --config config.local.json "$ROLE_KEY" approved --note "Reviewed manually"
role-monitor prepare-handoff --config config.local.json --destination output/handoff.json
```

The live `scan` command never makes a review decision. Use `role-monitor --help` and each subcommand's `--help` for the complete interface.

Live discovery is deliberately opt-in and requires an explicit `--allow-network` flag. Use only public sources, honor provider terms and rate limits, and review discovered roles before any handoff.

## Optional Telegram scan digest

Telegram notification is also explicit and observational. A live scan sends one summary-only, plain-text digest only when `--notify-telegram` is present; it never includes reviewer notes, job descriptions, local paths, credentials, or notification destinations, and it cannot approve or hand off a role.

```bash
role-monitor scan \
  --config config.local.json \
  --allow-network \
  --notify-telegram
```

The implementation reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from the process environment. The repository contains empty placeholders only. See [Telegram setup and failure behavior](docs/telegram.md) before enabling a live send.

If a process stops after delivery begins, first reconcile the downstream task system. Preserve and acknowledge its exact receipt when possible. Only when a stable-role-key upsert is confirmed safe should an operator release the stranded claim for retry:

```bash
role-monitor recover-handoff \
  --config config.local.json \
  --manifest output/handoff.json \
  --confirm-downstream-reconciled
```

## Evidence an operator can inspect

| Stage | Artifact or evidence | Why it matters |
| --- | --- | --- |
| Discovery | Normalized role records and structured per-source failures | Shows partial success instead of hiding a broken feed. |
| Evaluation | Configurable score and review queue | Makes ranking criteria inspectable and reversible. |
| State | SQLite roles, decisions, and audit events | Preserves review decisions while listings change on later scans. |
| Delivery | Deterministic handoff manifest and exact receipt | Prevents partial, stale, or mismatched acknowledgements from being treated as delivered. |
| Reporting | Atomic, redacted JSON and CSV reports | Produces shareable run evidence while keeping reviewer notes in the private SQLite state. |

## Architecture

```mermaid
flowchart LR
    A[Public ATS feeds] --> B[Concurrent discovery]
    B --> C[Normalized roles]
    C --> D[Configurable scoring]
    D --> E[SQLite identity and state]
    E --> F{Human review}
    F -->|approve| G[Deterministic manifest]
    F -->|reject or defer| E
    G --> H[Exact receipt verification]
    H -->|verified| E
```

See the [full architecture](docs/architecture.md) for module responsibilities and reliability decisions.

## Supported public ATS sources

| Provider | Support | Documentation |
| --- | --- | --- |
| Greenhouse | Native public job-board adapter | [Job Board API](https://developers.greenhouse.io/job-board.html) |
| Lever | Native public postings adapter | [Postings API](https://github.com/lever/postings-api) |
| Ashby | Native public job-postings adapter | [Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) |
| SmartRecruiters | Native public postings adapter with pagination | [Public Posting API](https://developers.smartrecruiters.com/docs/endpoints) |
| Workable | Native public published-jobs adapter | [Published jobs endpoint](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page) |
| Recruitee | Native public careers-site adapter | [Careers Site API](https://docs.recruitee.com/reference/intro-to-careers-site-api) |
| Other career pages | Generic HTML / JSON-LD fallback | **Incomplete by design**; dynamic sites can hide listings. |

The project uses documented public endpoints and synthetic fixtures. Provider schemas, availability, and terms can change; see [provenance](docs/provenance.md).

## Safety boundaries

- The project never submits applications, sends credentials to job sources, or automates a consequential decision.
- Human approval is required before a role can be handed off; rejected and deferred decisions are retained separately.
- Configuration rejects authentication-shaped fields, and persisted external errors are bounded and redacted.
- Only an exact receipt for the current manifest can complete delivery; invalid or partial receipts leave state unchanged.
- Concurrent delivery is blocked by a durable claim; crash recovery requires explicit downstream reconciliation rather than an automatic timeout takeover.
- A first complete-feed miss pauses review and handoff eligibility; retirement requires two consecutive complete misses, and roles not seen for seven days must be refreshed before review or handoff.
- Telegram is optional, summary-only, and disabled by default. Credentials come only from the process environment; tests use injected senders and no real keys.

Read the complete [security policy and operating boundaries](SECURITY.md).

## Limitations

- Generic career-page parsing is a best-effort fallback, not a full browser automation solution.
- The system cannot guarantee that a third-party listing is accurate, current, or still accepting candidates.
- The standard-library HTTP transport validates resolved and redirected targets before each request, but it does not pin the validated IP through the TLS connection; configured hosts are assumed not to use DNS rebinding.
- Scoring is an aid to review, not a fit prediction or hiring decision.
- Operators remain responsible for source terms, request rates, and their own follow-up decisions.

## Tests and quality checks

The test suite covers provider-domain validation, malformed responses, retry behavior, concurrent failure isolation, scoring policy, stable identity, preserved review decisions, report redaction/atomicity, exact handoff receipts, Telegram rate limits, environment loading, credential redaction, and no-network dry runs.

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## Roadmap

- [x] Ship an offline `role-monitor demo` workflow with synthetic reports, manifest, and receipt.
- [x] Add optional Telegram notifications with environment-only credentials, summary-only templates, and mocked tests.
- [ ] Expand documented public-feed coverage while preserving the same review and verification boundary.

## Contributing

Contributions should improve documented public-feed compatibility, reliability, accessibility, or evidence quality. See [CONTRIBUTING.md](CONTRIBUTING.md); all fixtures must remain fictional.

## License

MIT. See [LICENSE](LICENSE).
