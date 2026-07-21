# Architecture

The project separates discovery from decisions and delivery. That boundary keeps network failures and ranking changes from silently becoming external actions.

```mermaid
flowchart LR
    A["Public ATS feeds"] --> B["Concurrent discovery"]
    C["Generic career pages"] --> B
    B --> D["Normalized role records"]
    D --> E["Configurable policy scoring"]
    E --> F["SQLite state and deduplication"]
    F --> G["Human review queue"]
    G -->|"approve"| H["Deterministic handoff manifest"]
    G -->|"reject or defer"| F
    H --> I["Task-system adapter"]
    I --> J["Exact receipt verification"]
    J -->|"verified"| F
    J -->|"invalid or partial"| K["No state change"]
```

## Module boundaries

| Module | Responsibility |
| --- | --- |
| `feeds` | Recognize documented ATS domains and normalize already-fetched payloads. |
| `http` | Fetch public pages with bounded retries and injectable transports. |
| `discovery` | Run sources concurrently while isolating per-source failures. |
| `policy` | Apply transparent, configuration-driven scoring rules. |
| `store` | Preserve stable role identity, review decisions, audit events, and delivery state in SQLite. |
| `review` | Expose the explicit human decision boundary. |
| `handoff` | Build atomic manifests and require exact receipts before acknowledging delivery. |
| `reports` | Produce deterministic, redacted CSV and JSON artifacts. |
| `cli` | Coordinate safe commands and the offline demonstration. |

## Reliability decisions

- Stable provider IDs take precedence over mutable job titles for deduplication.
- Tracking parameters and fragments do not create new role identities; provider identity parameters such as Greenhouse `gh_jid` are preserved.
- Only transient transport failures are retried; ordinary client errors fail immediately.
- An exception from one source becomes a structured failure record instead of aborting the run.
- Generic HTML results are treated as incomplete because dynamic career pages may hide listings.
- The first absence from a successful, complete native-source scan pauses review and handoff eligibility. A role is retired only after two consecutive complete misses; failures and generic partial scans never infer closure. Rediscovery clears the miss count without discarding a still-valid human decision.
- Active roles last seen more than seven days ago are unavailable for review or handoff until a fresh scan observes them. Prepared pending manifests containing an unavailable role are invalidated.
- Rescans update public listing fields without resetting a person's decision.
- Approval freezes an immutable public-field snapshot, so a later title or URL change cannot enter a handoff without having been part of the human-reviewed record.
- Only the latest registered manifest can be acknowledged; exact role and URL coverage is required.
- A durable SQLite delivery claim prevents concurrent adapter calls. It never expires automatically because a slow but active call must not be taken over.
- After a crash, the operator first reconciles downstream state. An exact retained receipt can be acknowledged directly; otherwise the claim can be released only with explicit confirmation before an idempotent retry. Adapters must upsert by stable role key because recovery may repeat a call whose receipt was lost.
