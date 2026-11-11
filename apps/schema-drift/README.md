# The Crypt — Schema Drift

Touchstone app — detects drift between the SQLAlchemy model declarations in
`demo/models.py` and the live Postgres schema.

## Quick start

```bash
# from repo root
cp .env_sample .env           # add DATABASE_URL=postgresql://...
bash apps/schema-drift/run.sh
```

Then open <http://localhost:8001>.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/config` | App metadata + latest_report_id |
| GET | `/discover` | Declared comparison targets |
| POST | `/compare` | Run drift detection, returns Report |
| GET | `/report/{id}` | Reload a saved report |
| POST | `/apply` | 403 in demo mode |
| POST | `/rollback` | 403 in demo mode |

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `DATABASE_URL` | — | Postgres connection string (required) |
| `SCHEMA_DRIFT_PORT` | `8001` | Server port |

## Demo drift cases

The `demo/models.py` declares two intentional mismatches against the Neon Postgres seed:

| Ghost | Kind | Policy | UI name |
|-------|------|--------|---------|
| `users.age` declared `INTEGER`, live is `BIGINT` | `TYPE_CHANGED` (narrowing) | REVIEW_REQUIRED | 🕯 Type Wraith |
| `ix_users_email` exists in live DB but not declared | `INDEX_EXTRA` | BLOCKED | ⛓ Phantom Index |

Click **Hunt Ghosts** → two cards appear → Apply is disabled in demo mode.

Page refresh restores the last report automatically (via `/config` → `latest_report_id`).

## Auth

Demo-sandbox mode: `/apply` and `/rollback` return HTTP 403. The compare
pipeline runs fully; only write operations are disabled.

## Runtime files

The app creates these at runtime — do not commit them:

| Path | Description |
|------|-------------|
| `apps/schema-drift/.reports/` | Saved report JSON (one file per compare run) |
| `apps/schema-drift/.cache.json` | LLM resolution cache |
