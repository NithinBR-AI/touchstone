# Contract Observer — Touchstone App 2

Detects **API contract drift**: fields the live API returns that are not documented in the OpenAPI spec, or fields declared in the spec that the API no longer returns.

## What it does

1. Fetches `openapi.yaml` from the target repo (GitHub raw URL)
2. Pings `GET /health` on the live API (warms up Render free-tier cold-start)
3. Probes each GET endpoint and infers the actual response schema
4. Diffs intended vs actual — emits `FIELD_EXTRA` and `FIELD_MISSING` drift
5. For each drift: opens a GitHub PR on the target repo to update the spec

## Demo drift cases

The demo target (`NithinBR-AI/touchstone-demo-target-repo`) returns:
- `account_status` on `GET /users` — not in spec (**FIELD_EXTRA**)
- `sku` on `GET /products` — not in spec (**FIELD_EXTRA**)

## Running locally

```bash
# From repo root — GITHUB_TOKEN needed to open PRs
GITHUB_TOKEN=ghp_... bash apps/contract-observer/run.sh
```

Runs on `http://localhost:8002` by default. Set `CONTRACT_OBSERVER_PORT` to override.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Liveness + config summary |
| GET | /config | App config + active repo + latest report ID |
| GET | /repos | All configured repos |
| POST | /repos/select | Switch active repo `{"repo_id": "..."}` |
| GET | /discover | Active repo's openapi_url + api_base_url |
| POST | /compare | Probe live API and detect contract drift |
| POST | /apply | Batch-open GitHub PRs for all diffs of the same kind |
| POST | /rollback | Mark a drift item rolled back `{"difference_id": "..."}` |
| GET | /report/{id} | Fetch a stored report |
| POST | /demo/reset | Re-seed drift and invalidate remote cache for judge replays |

## Config

`config.json`:
```json
{
  "domain": "api",
  "accent": "#67e8f9",
  "title": "The Séance"
}
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| GITHUB_TOKEN | Yes (for /apply) | GitHub PAT with `repo` scope on target repo |

## Architecture

```
repos.json
  → adapters/openapi/intended.py   (parse openapi.yaml from URL)
  → adapters/openapi/actual.py     (probe live API, infer schema)
  → adapters/openapi/comparator.py (diff intended vs actual)
  → core/orchestrator.py           (state machine, cache, persistence)
  → adapters/openapi/executor.py   (open GitHub PR)
  → adapters/openapi/verifier.py   (re-fetch spec, confirm field added)
```
