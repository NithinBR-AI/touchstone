# The Poltergeist — Cache Drift Detector

Detects drift between declared cache keys (`cache_config.yaml` in the target
repo) and what actually exists in the live Redis instance.

## Domain

**cache** — Redis key existence drift.

## Drift cases

| Kind | Severity | State | Description |
|---|---|---|---|
| `KEY_MISSING` | MEDIUM | PENDING | Key declared in config but absent from Redis — fix: POST /cache/warm |
| `UNDECLARED_KEY` | HIGH | PENDING | Key exists in Redis but not declared — fix: DELETE /cache/delete |

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