# Human-in-the-Loop AI Role Monitor

A Python system that turns public job feeds into an explained review queue,
retains a person's decisions, and verifies each approved handoff. Matching uses
configurable rules, **not an LLM**; “AI roles” describes the job-search focus.

## Example result

**Synthetic demo example:** “AI Automation Engineer” at fictional Northstar
Automation receives a score of **45** and enters the review queue as
**pending**. A score does not authorize a handoff.

| Step | What the person or system does |
| --- | --- |
| Review | A person approves, rejects, or defers a role |
| Handoff | Only an approved role enters the handoff package |
| Confirmation | Delivery completes only after a matching receipt |

The demo exercises simulated review decisions and a fictional receiver. It
submits no applications and measures no hiring outcome.

## My contribution

I implemented public-feed adapters, explainable scoring, SQLite review state,
and receipt-verified handoff. This Python project emphasizes persistent state
and delivery correctness; the separate n8n Job Monitor emphasizes workflow
orchestration and integration design.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-0B7F5C.svg)](LICENSE)

![Public job feeds moving through scoring, human review, and a verified handoff](assets/social-preview.png)

This is a monitoring and review system, not an application bot. It never
submits an application or makes a hiring decision.

<a id="review-this-project-in-3-minutes"></a>

## Explore the project

Start with the example above, then follow the diagram and the
[engineering evidence](#engineering-evidence). Setup is optional for review.

## How it works

1. Collect public openings from configured job-system feeds.
2. Convert each listing into one consistent role record.
3. Score the role with configurable terms and place it in a review queue.
4. Preserve the role's identity and previous decisions in SQLite when it
   appears again.
5. Require a person to approve, reject, or defer the role.
6. Prepare an approved handoff and mark it complete only after the receiver
   confirms the exact handoff package.

Automation handles repeatable collection and recordkeeping. A person keeps
control of the consequential decision.

## Engineering evidence

| Capability | Implementation | Check |
| --- | --- | --- |
| Collect feeds with isolated failures | [Discovery](src/role_monitor/discovery.py) | [HTTP/discovery tests](tests/test_http_discovery.py) |
| Explain a score | [Scoring policy](src/role_monitor/policy.py) | [Policy tests](tests/test_policy.py) |
| Preserve review and verify delivery | [Store](src/role_monitor/store.py), [handoff](src/role_monitor/handoff.py) | [Review-state tests](tests/test_store_review.py), [handoff tests](tests/test_handoff.py) |

## Optional offline demo

The demo uses fictional employers, roles, decisions, and receipts. It does not
contact job sites, submit applications, or require credentials.

```bash
git clone https://github.com/mccruz/human-in-the-loop-ai-role-monitor.git
cd human-in-the-loop-ai-role-monitor
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-deps -e .
role-monitor demo --output-dir demo-output --reset
```

Review these generated files:

- `demo-output/reports/` — redacted run summaries;
- `demo-output/handoff_manifest.json` — the approved fictional handoff; and
- `demo-output/handoff_receipt.json` — confirmation of that exact handoff.

Preview the optional Telegram summary without credentials or a network request:

```bash
role-monitor demo --output-dir demo-output --reset --telegram-dry-run
```

Live source configuration is deliberately opt-in. It requires a local file that
is excluded from Git and an explicit `--allow-network` flag. See
`role-monitor --help`, the [architecture guide](docs/architecture.md), and the
[Telegram guide](docs/telegram.md) before using live feeds or notifications.

## Evidence produced by the workflow

| Stage | Reviewable evidence |
| --- | --- |
| Collection | Normalized roles and separate source failures |
| Scoring | Visible score, reasons, and review threshold |
| State | Saved roles, decisions, and audit history |
| Handoff | Exact approved package and matching receipt |
| Reporting | Redacted JSON and CSV summaries |

## Safety and limits

- The workflow never submits applications or approves a role automatically.
- Human decisions are stored separately from source data and retained across
  rescans.
- Authentication-shaped configuration fields are rejected, and external errors
  are limited and redacted.
- Only an exact receipt for the current handoff can complete delivery.
- A delivery interrupted after sending requires a person to reconcile the
  downstream system before retrying.
- Telegram is optional, summary-only, and disabled by default.

Public listings can be incomplete, inaccurate, or removed without notice. The
generic career-page fallback cannot reliably parse every dynamic website, and
configured sources remain subject to provider terms and rate limits. Scoring is
a review aid, not a prediction of job fit or hiring outcome.

## Verification

The test suite covers source validation, malformed responses, retries, scoring,
stable identity, retained decisions, report redaction, delivery confirmation,
Telegram limits, and offline operation.

```bash
python -m unittest discover -s tests -v
python -m compileall -q src tests
```

## Project guide

- [Architecture](docs/architecture.md)
- [Security policy](SECURITY.md)
- [Telegram setup and behavior](docs/telegram.md)
- [Public-source and authorship boundary](docs/provenance.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
