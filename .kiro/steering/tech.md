```markdown
# Tech Steering — Touchstone

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ |
| Backend | FastAPI + uvicorn (one instance per app) |
| Frontend | One shared template in `apps/shared/ui/`, vanilla JS, Tailwind CDN |
| SQL parser | sqlglot, **pinned to exact tested version** |
| DB driver | psycopg 3.x |
| HTTP client | httpx (verify version supports `sni_hostname` extension) |
| Target DB | Postgres 16 (Docker local, Neon for demo) |
| LLM | AWS Bedrock (Claude), temperature 0 |
| MCP | Context7, **pinned version** (not `@latest`) |
| Packaging | pyproject.toml, editable install |

## Forbidden
- React, Vite, webpack, any frontend build step
- Streamlit, node_modules, vector databases
- Any change to `core/` after the week-2 freeze
- `pip install` executed by the tool
- `git checkout` / `git reset` / `git restore` in any executor
- Environment-wide package enumeration
- Reverse-patch rollback
- Automatic database rollback
- Storing raw evidence without full redaction
- Storing raw exception text
- HTTP requests to private/loopback/link-local/reserved IP ranges
- `@latest` for MCP packages
- `SET LOCAL` as a statement in the executor (use connection options)
- Auto-fixing non-widening type changes
- `trust_env=True` in httpx
- Automatic redirect following

## Commands
```

pip install -e .
pytest -q
./apps/schema-drift/run.sh
./apps/contract-observer/run.sh
./apps/dependency-auditor/run.sh    # optional
touchstone compare --target users.email
docker compose -f fixtures/docker-compose.yml up

```

## Code conventions
- Type hints on every public function
- `core/` = exactly 7 files: `__init__.py`, `interfaces.py`, `models.py`,
  `orchestrator.py`, `validator.py`, `persistence.py`, `cache.py`
- Core modules may import from other core modules
- No module outside `core/` may be imported by any module inside `core/`
- Adapters import from `core`; reverse is forbidden
- Every app's `main.py` imports from `core/` and its domain adapters
- Every `Resolution` carries the prompt's SHA-256
- Tests mirror module structure under `tests/`

## Structure rules
- `core/` — 7 files, frozen after week 2
- `adapters/<domain>/` — intended.py, actual.py, comparator.py, executor.py,
  verifier.py (plus normalize.py for Postgres; ssrf.py for openapi)
- `apps/shared/ui/` — one template: index.html, app.js, style.css
- `apps/<app-name>/` — main.py, config.json, README.md, run.sh
- `fixtures/` — seed.py, cases.json, docker-compose.yml
- `.kiro/` — committed, never gitignored

## SQL operation matrix (the safety boundary)

Every database difference maps to exactly one row. The validator enforces
this matrix. Anything not in the matrix fails closed.

| Difference kind | Proposed action | Policy |
|---|---|---|
| `INDEX_MISSING` | `CREATE INDEX` (plain B-tree, non-unique, default column order, name = `ix_<table>_<cols>`) | auto-fixable if gates pass |
| `TYPE_CHANGED` where `(old, new)` is in the widening table | `ALTER COLUMN TYPE` (no USING) | auto-fixable if gates pass |
| `TYPE_CHANGED` where `(old, new)` is NOT in the widening table | — | REVIEW_REQUIRED |
| `COLUMN_MISSING` (nullable or default) | `ADD COLUMN` | REVIEW_REQUIRED |
| `COLUMN_MISSING` (NOT NULL, no default) | — | BLOCKED |
| `TABLE_MISSING` | — | BLOCKED |
| `COLUMN_EXTRA` | `DROP COLUMN` | BLOCKED |
| `TABLE_EXTRA` | `DROP TABLE` | BLOCKED |
| `INDEX_EXTRA` | `DROP INDEX` | BLOCKED |

### Allowed type-widening pairs (exhaustive)
- `smallint` → `integer`
- `integer` → `bigint`
- `real` → `double precision`
- `varchar(n)` → `varchar(m)` where m > n
- `varchar(n)` → `text`

Any other pair → REVIEW_REQUIRED. The resolver refuses to propose an
auto-fix for non-widening pairs. The validator independently verifies the
(old_type, new_type) pair from `Difference.context` — NOT from the
resolver's rationale.

## SQL executor timeout

Set at the **connection level**, never as a statement:
```python
conn = psycopg.connect(dsn, options="-c statement_timeout=30000")
```

The validator rejects SET statements outright.

## App phase implementation rules

### Auth — Apply is OPEN (both apps)
No `_require_write()` guard. All endpoints open.

**Schema Drift**: `/apply` calls `orch.apply_one(req.difference_id)`,
`/rollback` calls `orch.rollback(req.difference_id)`.

**Contract Observer**: `/apply` bypasses `orch.apply_one()` — calls
`ContractExecutor.execute(diff)` directly. All API diffs have `safe_fix=False`
(PR-based fixes, not in-place); `apply_one()` would gate on REVIEW_REQUIRED/BLOCKED.
State updated to RESOLVED or EXECUTION_FAILED; saved via `orch.persistence.save(report)`.

Both serialise results via `_serialise_outcome()`.
`/compare`, `/discover`, `/report/:id`, `/health`, `/config` all open on both apps.

### Demo reset endpoint
```python
@app.post("/demo/reset")
def demo_reset():
    from fixtures.seed import main as seed_main
    seed_main(DB_URL)
    global _latest_report_id
    _latest_report_id = None
    invalidate_remote()
    return {"status": "reset", "message": "..."}
```
Calls `fixtures.seed.main()` which drops and recreates tables with intentional drift.
Also calls `invalidate_remote()` so the next compare fetches a fresh copy of models.py.

### repos.json + remote_source — how drift is checked
Target repos are declared in `repos.json` at the project root. No env var needed.

```json
[
  {
    "id": "customer-service",
    "name": "Customer Service API",
    "github": "NithinBR-AI/touchstone-demo-target-repo",
    "models_url": "https://raw.githubusercontent.com/NithinBR-AI/touchstone-demo-target-repo/main/models.py",
    "openapi_url": "https://raw.githubusercontent.com/NithinBR-AI/touchstone-demo-target-repo/main/openapi.yaml",
    "api_base_url": "https://touchstone-demo-target-repo.onrender.com",
    "default": true
  }
]
```

`adapters/remote_source.py` fetches the file at `models_url` over HTTP (public GitHub raw URL,
no PAT needed) and caches the result in a process-level dict keyed by URL. The app calls:

```python
def _models_path() -> Path:
    return fetch_remote(_active_repo["models_url"])
```

`invalidate_remote()` clears the cache (called on `/demo/reset` and `/repos/select`).
Adding a new target repo = one entry in `repos.json`; zero code changes required.

The UI repo selector (populated from `/config.repos`) lets judges see that Touchstone
is designed to monitor multiple repos, not just one hardcoded service.

### Orchestrator — module-level singleton
```python
# at module level in each main.py
orch = Orchestrator(...)          # created once at import
_latest_report_id: str | None = None   # updated after each /compare
```
One instance per app process. Correct for single-user demo. If extended to
support multiple concurrent users or a persistent server, replace with a
dict keyed by session ID or report ID. Do not refactor core/ to accommodate
this — keep the dict in main.py.

### `/config` response — includes `latest_report_id`
```python
@app.get("/config")
def get_config():
    return {**CONFIG, "latest_report_id": _latest_report_id}
```
`CONFIG` comes from `config.json` (domain, accent, title). `latest_report_id`
is `null` until the first `/compare` call. The UI uses it to restore state
on page refresh via `GET /report/{id}`.

### Scan line animation — fires once only
```css
.scan-line { animation: scanline 0.8s ease-out 1 forwards; }
```
`animation-iteration-count: 1` and `fill-mode: forwards`. Added to `<body>`
via JS on `DOMContentLoaded`, removed after the animation ends via
`animationend` listener. Never loops.

Frontend rules

· ONE shared UI template in apps/shared/ui/
· Each app's config.json specifies: domain, accent, title
· Template reads /config on load
· Domain rendering via RENDERERS[domain](difference) registry in app.js
  — template owns layout/badges/dialogs/state; renderers own only the
  evidence body inside a card
· All accent colors from var(--accent); no hard-coded hex in JS
· Seven visual states: pending, review, blocked, resolved, still-present,
  failed, superseded
· Three animations total (scan line, ghost flicker, cascade)
· Apply button only in state=PENDING
· Static files mounted with Path(__file__).parent.parent / "shared" / "ui" — never CWD

Validator rules

· SQL:
  · sqlglot (pinned). Reject multi-statement. Reject SET.
  · Match AST against operation matrix.
  · Canonical target per PG semantics: quoted preserves case, unquoted
    lowercases, public stripped, non-public schema rejected.
  · Reject USING, expression indexes, partial indexes.
  · Bind SQL to originating difference: operation type, schema, table,
    column/index name, index columns must match the Difference target
    exactly. Mismatch → blocked.
  · Type widening: extract (old, new) from Difference.context and AST
    independently. Confirm pair is in widening table.
  · Fail closed on unknown forms.
  · Real-Postgres execution tests required.
· File:
  · Symlink check on original path BEFORE resolve.
  · Path confinement to project root.
  · Verify before_sha256.
  · Parse both original and patched.
  · Format-aware key extraction.
  · Reject any key other than target.
  · Race-free atomic write: directory-fd-relative operations with
    O_NOFOLLOW, inode re-check, rename within the fd (see design.md).
· HTTP:
  · SSRF check before every request.
  · follow_redirects=False — redirects treated as non-2xx, path skipped.
  · trust_env=False — no proxy env vars.

SSRF protection (Contract Observer)

Before every request:

1. Parse URL. Reject non-http/https schemes.
2. Resolve hostname via socket.getaddrinfo.
3. For every resolved IP:
   · Convert IPv4-mapped IPv6 (::ffff:x.x.x.x) to IPv4.
   · Check against blocked ranges (below).
   · Reject non-global addresses (unless --allow-local and IP is 127.0.0.1
     or ::1).
4. trust_env=False in httpx — no proxy env vars.
5. follow_redirects=False in httpx — redirects are not followed.

Blocked CIDR ranges

· IPv4: 0.0.0.0/8, 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8,
  169.254.0.0/16, 172.16.0.0/12, 192.0.0.0/24, 192.0.2.0/24,
  192.168.0.0/16, 198.18.0.0/15, 198.51.100.0/24, 203.0.113.0/24,
  224.0.0.0/4, 240.0.0.0/4
· IPv6: ::1/128, ::/128, fc00::/7, fe80::/10, ff00::/8

--allow-local permits 127.0.0.1 and ::1 only, never other private
ranges. Off by default.

Redaction rules

Redaction happens before persistence, before logging, before the resolver
sees data.

Token-based matching (not substring)

Split normalized keys on camelCase boundaries and separators. Match if any
token equals a sensitive word. This avoids over-redacting monkey,
accessibility, author.

```python
SENSITIVE_TOKENS = {
    "authorization", "auth", "cookie", "token", "secret",
    "password", "passwd", "pwd", "credential", "credentials",
    "key", "apikey", "session", "sessionid", "access",
    "refresh", "bearer", "jwt", "signature", "hmac",
    "clientsecret", "privatekey", "dsn",
}
```

Surfaces covered

· Headers: Authorization, Cookie, Set-Cookie, X-API-Key,
  Proxy-Authorization, X-Auth-Token, X-Amz-Security-Token,
  X-Csrf-Token → ***REDACTED***
· Query strings: any key matching token rules → ***REDACTED***
· URL userinfo: user:pass@ → ***:***@
· Form bodies (application/x-www-form-urlencoded): matching keys
  → ***REDACTED***
· JSON: matching keys at depth ≤ 5 → ***REDACTED***.
  Depth > 5: discard entire subtree (replace with "***TRUNCATED***").
· Error evidence: NEVER store raw exception text. Store:
  ```python
  SafeError(code: str, error_class: str, summary: str)
  ```
  summary is generated from a lookup table keyed by code, max 200 chars.
  Not derived from the exception. Raw exception MAY be logged at debug level
  in local CLI only, never in deployed instance.
· Response body: truncated to 8KB AFTER redaction.

Suffix convention

Only evidence fields containing redacted content carry _redacted. Metadata
(status_code, duration_ms, kind, target) does not.

```

---
