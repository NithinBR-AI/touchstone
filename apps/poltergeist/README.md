# The Poltergeist — Cache Drift Detector

> **Where this app's code lives** — Touchstone App 3, built on the shared `core/` skeleton.
>
> | Part | Location |
> |---|---|
> | App (FastAPI wiring, endpoints, Reset Demo) | `apps/poltergeist/` — this folder |
> | Domain adapters | [`adapters/redis/`](../../adapters/redis/) — intended, actual, comparator, validator, executor, verifier |
> | Skeleton (shared by every app; holds no domain code) | [`core/`](../../core/) |
> | Resolver (deterministic, no LLM) | [`agents/resolver.py`](../../agents/resolver.py) — `MockResolver` |
> | Shared UI template | [`apps/shared/ui/`](../shared/ui/) |
> | Deployment | [`Dockerfile`](Dockerfile) in this folder — copies `core/` + `adapters/redis/` only; build from the repo root |

Detects drift between declared cache keys (`cache_config.yaml` in the target
repo) and what actually exists in the live Redis instance.

## Domain

**cache** — Redis key existence drift.

## Drift cases

| Kind | Severity | State | Description |
|---|---|---|---|
| `KEY_MISSING` | MEDIUM | PENDING | Key declared in config but absent from Redis — fix: POST /cache/warm |
| `UNDECLARED_KEY` | HIGH | BLOCKED | Key exists in Redis but not declared — fix: DELETE /cache/delete, once a human clicks Exorcise |

## Demo drift (always present)

- `products:list` → `KEY_MISSING` — declared but never populated by the app
- `debug:session_dump` → `UNDECLARED_KEY` — exists in Redis, never declared

## How to run

No environment variables needed. The Poltergeist never connects to Redis itself: it reads and fixes keys through the demo target API (`cache_status_url` and `cache_warm_url` in `repos.json`), and that service holds the Upstash credentials.

```bash
./apps/poltergeist/run.sh
# → http://localhost:8003
```

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/config` | GET | App config + latest report ID |
| `/repos` | GET | All configured repos |
| `/repos/select` | POST | Switch active repo `{"repo_id": "..."}` |
| `/discover` | GET | Active repo cache config |
| `/compare` | POST | Run cache drift detection |
| `/apply` | POST | Apply fix: KEY_MISSING → POST /cache/warm; UNDECLARED_KEY → DELETE /cache/delete |
| `/rollback` | POST | Not supported for cache domain |
| `/report/{id}` | GET | Fetch a previous report |
| `/demo/reset` | POST | Restore drift state (re-seeds cache keys) |

## Reset demo

After a judge applies a fix, click **↺ Reset Demo** to restore drift state.
The reset re-seeds both drift cases: `products:list` (KEY_MISSING) and
`debug:session_dump` (UNDECLARED_KEY).