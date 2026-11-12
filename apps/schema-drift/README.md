# The Crypt — Schema Drift

Touchstone app — detects drift between the SQLAlchemy model declarations and
the live Postgres schema. The declared schema lives in a separate target repo
([touchstone-demo-target](https://github.com/NithinBR-AI/touchstone-demo-target-repo));
Touchstone points at it via the `MODELS_PATH` env var.

## Quick start

```bash
# from repo root
cp .env_sample .env           # add DATABASE_URL (and optionally MANTLE_API_KEY)
bash apps/schema-drift/run.sh
```

Then open <http://localhost:8001>.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| GET | `/config` | App metadata + repos list + active_repo |
| GET | `/repos` | Full repos.json list + active repo id |
| POST | `/repos/select` | Switch active repo `{"repo_id": "..."}` |
| GET | `/discover` | Declared comparison targets (source URL + repo) |
| POST | `/compare` | Run drift detection, returns Report |
| GET | `/report/{id}` | Reload a saved report |
| POST | `/apply` | Apply a proposed fix to the live DB |
| POST | `/rollback` | Record a rollback event (DB domain: not supported) |
| POST | `/demo/reset` | Re-seed Neon DB with drift for judge replays |

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `DATABASE_URL` | — | Postgres connection string (required) |
| `MANTLE_API_KEY` | — | Mantle/Bedrock API key; falls back to MockResolver if unset |
| `SCHEMA_DRIFT_PORT` | `8001` | Server port |

Target repos are configured in `repos.json` at the repo root — no env var needed.
Add entries there to monitor more repos; the UI repo selector updates automatically.

## Demo drift cases

The [touchstone-demo-target](https://github.com/NithinBR-AI/touchstone-demo-target-repo) repo declares
two intentional mismatches against the Neon Postgres seed:

| Ghost | Kind | Policy | UI name |
|-------|------|--------|---------|
| `users.age` declared `INTEGER`, live is `BIGINT` | `TYPE_CHANGED` (narrowing) | REVIEW_REQUIRED | 🕯 Type Wraith |
| `ix_orders_user_id` exists in declared, missing from live | `INDEX_MISSING` | AUTO-FIX | 👻 Missing Specter |
| `users.email` declared `VARCHAR(255)`, live is `VARCHAR(50)` | `TYPE_CHANGED` (widening) | AUTO-FIX | 🕯 Type Wraith |

Click **Hunt Ghosts** → three cards appear → click **Exorcise** to apply auto-fixable drifts.

After applying, click **↺ Reset Demo** to re-seed the DB and start over.

Page refresh restores the last report automatically (via `/config` → `latest_report_id`).

## Runtime files

The app creates these at runtime — do not commit them:

| Path | Description |
|------|-------------|
| `apps/schema-drift/.reports/` | Saved report JSON (one file per compare run) |
| `apps/schema-drift/.cache.json` | LLM resolution cache |
