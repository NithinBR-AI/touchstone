# The Poltergeist — Cache Drift Detector

Detects drift between declared cache keys (`cache_config.yaml` in the target
repo) and what actually exists in the live Redis instance.

## Domain

**cache** — Redis key existence drift.

## Drift cases

| Kind | Severity | State | Description |
|---|---|---|---|
| `KEY_MISSING` | MEDIUM | PENDING | Key declared in config but absent from Redis |
| `UNDECLARED_KEY` | HIGH | BLOCKED | Key exists in Redis but not declared — possible data leak |

## Demo drift (always present)

- `products:list` → `KEY_MISSING` — declared but never populated by the app
- `debug:session_dump` → `UNDECLARED_KEY` — exists in Redis, never declared

## How to run

```bash
# Set env vars
export UPSTASH_REDIS_REST_URL=https://your-endpoint.upstash.io
export UPSTASH_REDIS_REST_TOKEN=your_token

./apps/poltergeist/run.sh
# → http://localhost:8003
```

## Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/config` | GET | App config + latest report ID |
| `/discover` | GET | Active repo cache config |
| `/compare` | POST | Run cache drift detection |
| `/apply` | POST | Apply fix for KEY_MISSING (calls /cache/warm) |
| `/rollback` | POST | Not supported for cache domain |
| `/report/:id` | GET | Fetch a previous report |
| `/demo/reset` | POST | Restore drift state via /cache/warm |

## Reset demo

After a judge applies a fix, click **↺ Reset Demo** to restore drift state.
This calls `/cache/warm` on the target app which deletes `products:list`
and restores `debug:session_dump`.