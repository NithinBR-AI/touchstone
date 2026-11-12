```markdown
# Requirements — Touchstone

## Introduction

Touchstone compares an intended system state (from repo files) against an
actual system state (from live environments), classifies drift
deterministically, proposes one fix per difference via one LLM agent,
validates each proposal against an operation matrix, and applies approved
fixes with evidence-backed re-verification.

Three applications shipped:
- Schema Drift (database) — MANDATORY
- Contract Observer (API) — MANDATORY
- The Poltergeist (cache) — SHIPPED (third app, Redis key-existence drift)

## Submission scope

MANDATORY:
- Req 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 15, 17, 18, 19, 20, 21, 23, 24, 25,
  26, 27, 28, 29, 30, 31, 32, 38, 40, 41, 42, 43
- Skeleton Crew: at least two apps in two folders (Req 30)

OPTIONAL — skip if behind; list under "What's next":
- Req 3 (discovery — demo uses cases.json)
- Req 12, 13, 14, 22, 34 (Dependency domain — CUT, replaced by Poltergeist)
- Req 16 (MCP grounding)
- Req 35 (Kiro hooks)
- Req 36 (CI)
- Req 37 items 2–3 (deployment polish)

Three-app submission shipped. Two mandatory apps satisfy Skeleton Crew.

## Requirements

### Requirement 1: Six-Interface Core
1. `core/interfaces.py` defines six protocols: `InputSource`, `SourceAdapter`,
   `Comparator`, `Resolver`, `Executor`, `Verifier`.
2. `core/` contains exactly seven files: `__init__.py` (not `init.py`),
   `interfaces.py`, `models.py`, `orchestrator.py`, `validator.py`,
   `persistence.py`, `cache.py`.
3. Core modules may import from other core modules.
4. No module outside `core/` imported by any module inside `core/`.
5. `git log --oneline core/` shows exactly one freeze commit after the week-2
   initial.
6. `git diff core/` empty after all apps complete.
7. `tests/test_skeleton.py` asserts file count and import rule.

### Requirement 2: Core Data Models
1. `GhostState` enum: `PENDING`, `REVIEW_REQUIRED`, `BLOCKED`, `RESOLVED`,
   `STILL_PRESENT`, `EXECUTION_FAILED`, `SUPERSEDED`.
2. `Difference` has `id: str` (UUID) and `signature: str` (stable hash).
3. `Resolution.proposed_change: TypedChange | None` (None for review-only).
4. `TypedChange = SQLChange | FilePatch`.
5. `FilePatch` has `before_sha256`, `after_sha256`, `forward_patch`,
   `reverse_patch`, `file_path`.
6. `ExecutionResult` has `rollback_bytes`, `post_apply_bytes`,
   `post_apply_sha256`, `file_path_ref` (file domain only).
7. `VerificationResult`: `resolved: bool`, `rechecked_at`, `evidence: dict`,
   `remaining: Difference | None`.
8. `ApplyOutcome`: `state`, `execution`, `verification | None`.
9. `RollbackEvent`: `attempted_at`, `success`, `reason`.
10. `Report.rollback_events: dict[str, list[RollbackEvent]]`.
11. `Report.requires_refresh: bool`.
12. `Difference.context["safe_fix"]: bool`.

### Requirement 3: Discovery (optional in demo)
Scan for models.py, openapi.yaml, pyproject.toml. Emit ComparisonTarget with
evidence. Demo uses cases.json.

### Requirement 4: Address Resolution
Database URL order: `--db-url`, `DATABASE_URL`, `.env`, `alembic.ini`,
`docker-compose.yml`. API URL order: `--api-url`, `API_URL`,
`openapi.yaml servers[0].url`, `docker-compose.yml`. Redact credentials.

### Requirement 5: Intended Database Schema
Import via `importlib.util.spec_from_file_location`, read
`Base.metadata.tables`. Normalize per Req 6.

### Requirement 6: Type Normalization
`normalize_sqlalchemy_type()` / `normalize_pg_type()` → canonical strings.
`tests/test_normalize.py` with ≥30 triples including quoted identifiers,
precision/scale, nullability. Unsupported types raise defined error.

### Requirement 7: Actual Database Schema
Query `information_schema.columns`, `pg_indexes`, PK constraint views.
Connection timeout 10s, query timeout 30s via
`psycopg.connect(dsn, options="-c statement_timeout=30000")`.

### Requirement 8: Database Comparison
Emit Difference per Req 7 of tech.md operation matrix. Severity per rules.
`TYPE_CHANGED` carries `context["original_actual_type"]`.
`context["safe_fix"]` set per matrix.

### Requirement 9: Intended API Contract
Parse spec, resolve local `$ref`, extract status codes and 200-schema
properties. Skip `x-` keys.

### Requirement 10: Actual API Contract with SSRF Protection
1. Send one request per operation with configured path-parameter placeholder.
2. **Before every request:**
   - Reject non-http(s) schemes.
   - Resolve hostname via `socket.getaddrinfo`.
   - Convert IPv4-mapped IPv6 to IPv4.
   - Check every resolved IP against blocked CIDR ranges (see tech.md).
   - Reject non-global addresses (unless `--allow-local` and IP is
     `127.0.0.1`/`::1`).
3. **Disable automatic redirect following** (`follow_redirects=False`). Redirects
   are treated as non-2xx; the path is skipped.
4. **Disable environment-derived proxies** (`trust_env=False`).
5. `--allow-local` flag permits `127.0.0.1` and `::1` only. Off by default.
6. Record `status_code`, top-level keys, fully redacted raw request, fully
   redacted raw response body (8KB post-redaction). Timeout 10s → `UNREACHABLE`.
7. Never send mutating requests without `--allow-mutating`.
8. Observation is a sample, not a proof. Report displays the evidence.

### Requirement 11: API Comparison and Redaction
1. Emit differences: endpoint missing, field missing, field undocumented,
   status code changed, field rename suggestion.
2. `safe_fix` classification:
   - `FIELD_UNDOCUMENTED` → `safe_fix=True`, may produce spec patch
   - Others → `safe_fix=False`, REVIEW_REQUIRED, `proposed_change=None`
3. **Redaction (see tech.md for full rules):**
   - Token-based matching (not substring): avoids monkey/accessibility/author
   - Headers, query strings, URL userinfo, form bodies, JSON depth ≤ 5,
     error evidence
   - **JSON at depth > 5:** discard entire subtree
   - **Error evidence:** NEVER store raw exception. Store
     `{code, error_class, summary}` where summary is generated from code
   - Redaction before persistence, logging, and resolver
   - `_redacted` suffix only on redacted evidence fields, not metadata
4. `FIELD_UNDOCUMENTED` resolution describes outcome as "documented an
   observed response field" with claim/not_claimed evidence.

### Requirement 12: Intended Dependencies (optional)
Parse `pyproject.toml` via `tomllib` (stdlib) or `requirements.txt` via
`packaging.requirements`.

### Requirement 13: Actual Dependencies (optional)
`importlib.metadata.version()` for declared packages only. No environment
enumeration. No pip subprocess.

### Requirement 14: Dependency Comparison (optional)
- `DECLARED_UNPINNED` (LOW): `PENDING` resolution proposing pin to
  `==<installed>`. **Human clicks Apply** — not auto-applied.
- `DECLARED_VERSION_MISMATCH` (HIGH): `REVIEW_REQUIRED`,
  `proposed_change=None`.
- `DECLARED_MISSING_INSTALL` (HIGH): `BLOCKED`.

### Requirement 15: Resolver Agent
- At most one live call per difference per run + one schema-validation retry.
- Cache hits bypass live call.
- Temperature 0. Prompt SHA-256 recorded.
- JSON schema validated. Retry once on failure, then `blocked`.
- Only Difference and context sent.
- Cache keyed by signature with `cache_version`, `validator_contract`,
  `prompt_hash`.
- `safe_fix=False` or `DECLARED_VERSION_MISMATCH` → review-only Resolution.
- Demo mode serves cached resolutions marked `CACHED`.
- **The resolver SHALL be operational in week 1** — mock returning canned
  resolutions acceptable for week-1 schema-drift path.

### Requirement 16: MCP Grounding (optional)
Context7 config in `.kiro/settings/mcp.json` with **pinned version** (not
`@latest`). Fetch PostgreSQL/OpenAPI 3.1/PEP 621 docs per domain. If docs
don't cover syntax → `LOW`. If unreachable → proceed, force `LOW`. MCP
improves quality; validator enforces safety independently.

### Requirement 17: SQL Validator — Matrix + Target Binding + Widening
1. **Operation matrix** as constant in `core/validator.py` (see tech.md).
2. Parse via `sqlglot` (pinned version).
3. Reject multi-statement.
4. **Reject `SET` statements outright** (timeout via connection options, not
   statements).
5. Match AST to matrix. Reject if policy BLOCKED. REVIEW_REQUIRED diff kinds
   produce `proposed_change=None`.
6. Canonical target: quoted preserves case, unquoted lowercases, `public`
   stripped, non-public schema rejected. `"Users"` ≠ `users`.
7. Reject `USING` clause, expression indexes, partial indexes.
8. **Bind SQL to originating difference:** schema, table, column/index name,
   index columns MUST match `Difference.target` exactly. Mismatch → `blocked`.
9. **Type widening:** extract `(old, new)` independently from
   `Difference.context` and AST. Confirm pair in widening table. Resolver's
   claim SHALL NOT be trusted.
10. `tests/test_sqlglot_ast_assumptions.py` verifies AST shapes against
    pinned version.
11. `tests/test_validator_sql.py`: all matrix rows, injection cases,
    canonical target cases, target-binding mismatches, widening/non-widening,
    `drop_flag` NOT blocked.
12. `tests/test_sql_execution.py`: every auto-fixable case runs against real
    ephemeral Postgres.

### Requirement 18: File Patch Validator
1. Symlink check on original path BEFORE resolve.
2. Path confinement to project root.
3. Verify `before_sha256`.
4. Parse both original and patched (YAML root dict/list, TOML via tomllib,
   requirements via packaging.requirements).
5. Format-aware key extraction.
6. Reject any key other than target.
7. **Race-free file writes** — see Req 21 and design.md.

`test_validator_file.py`: traversal, symlink, stale hash, invalid YAML,
YAML root as list, wrong-key, unrelated requirements line.

### Requirement 19: State Machine
Orchestrator is sole state owner. Transitions:
- PENDING → EXECUTION_FAILED (executor fails)
- PENDING → RESOLVED (executor succeeds, verify passes)
- PENDING → STILL_PRESENT (executor succeeds, verify fails)
- PENDING → SUPERSEDED (sibling file patch applied)
- REVIEW_REQUIRED / BLOCKED / EXECUTION_FAILED → apply refused
- RESOLVED/STILL_PRESENT (file) → rollback event recorded, state unchanged
- RESOLVED/STILL_PRESENT (database) → rollback refused

No path produces PENDING when `validator_status != "valid"`.
`persistence.save()` on every state-changing path.
`test_state_transitions.py` enumerates 7×8 = 56 cells + invariants +
persisted-state-match check.

### Requirement 20: Database Execution
1. Statement timeout at **connection level**:
   `psycopg.connect(dsn, options="-c statement_timeout=30000")`.
2. Single parsed statement, transaction, commit on success, rollback on error.
3. **NO automatic rollback statement.** Preserve
   `context["original_actual_type"]` (where applicable) and applied statement.
4. UI text: "Automatic rollback is unsupported. The originally observed state
   and the executed SQL are preserved for manual recovery." (NOT "inverse
   preserved" — no inverse is generated.)
5. Rollback request → `RollbackEvent(success=False, reason="rollback not
   supported for database domain")`.

### Requirement 21: File Execution — Race-Free Atomic Write
1. Verify `before_sha256`.
2. Compute patched bytes, verify `after_sha256`.
3. **Directory-fd-relative atomic write:**
   - Open parent directory with `os.O_RDONLY | os.O_DIRECTORY`
   - Open existing target via `os.open(filename, os.O_RDONLY | os.O_NOFOLLOW,
     dir_fd=dir_fd)` to read current bytes (fail if symlink)
   - Capture `st_ino` and `st_dev` via `os.stat(filename, dir_fd=dir_fd,
     follow_symlinks=False)`
   - Write temp in same directory via fd with `O_CREAT | O_EXCL | O_WRONLY |
     O_NOFOLLOW`
   - `os.fsync` the temp
   - Re-open target via fd with `O_NOFOLLOW`, re-read, compare bytes
   - Re-stat via fd, compare inode+device
   - `os.rename(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)` —
     atomic within the directory, cannot escape via symlink
4. Record `rollback_bytes`, `post_apply_bytes`, `post_apply_sha256`,
   `file_path_ref`.
5. Rollback: refuse if current != `post_apply_bytes`. Atomic write of
   `rollback_bytes` using the same fd-relative procedure. Append RollbackEvent.
   State unchanged.

`test_rollback.py`: file success, external edit refusal, double rollback
refusal, database refusal, inode-replacement race (mock via concurrent write).

### Requirement 22: Dependency Execution (optional)
Declaration repair only for `DECLARED_UNPINNED` (proposes pin, human applies).
Patches manifest. Runs FilePatchValidator. Never runs pip. Follows Req 21.

### Requirement 23: Verification with Evidence
Re-run comparison for single target after apply. Return VerificationResult
with resolved, rechecked_at, evidence, remaining. Update ghost count.

### Requirement 24: ApplyOutcome Return Type
`apply_one()` returns ApplyOutcome. Execution failure → `verification=None`.
Success → verification populated. `/apply` serializes `state`, `execution`,
`verification` (nullable).

### Requirement 25: Rollback as Event (file domain only)
Rollback events apply to file-domain only. Database has no automatic
rollback. File rollback appends RollbackEvent; state unchanged; original
execution and verification preserved; persist immediately.

### Requirement 26: Human Approval Gate (server-side, not browser)
1. Apply button only on PENDING cards.
2. On apply, show exact change, blast radius, reversibility, rollback.
3. No "Apply All". No override for REVIEW_REQUIRED.
4. **Server-side authorization:**
   - Either: bearer token on `/apply`, `/rollback`, `/report/:id`
     (configurable via env var; never logged or returned in responses)
   - Or: isolated read-only demo (comparison works; apply returns 403;
     reset mechanism documented and tested)
   - Mode documented in app README and submission write-up
   - CORS same-origin is not a substitute
5. Record authenticated principal (token ID or "demo-sandbox") in report
   audit log.

### Requirement 27: Superseded Handling
After file patch success: `requires_refresh=True`, mark other PENDING diffs
on same file as SUPERSEDED. UI dims them. `/apply` refuses SUPERSEDED.

### Requirement 28: FastAPI Backend (per app)
- Mount `apps/shared/ui/` via `Path(__file__).parent.parent / "shared" / "ui"`
- Endpoints: `GET /discover`, `POST /compare`, `POST /apply`, `POST /rollback`,
  `GET /report/:id`, `GET /health`, `GET /config`
- Authorization per Req 26

### Requirement 29: Shared UI Template
1. `apps/shared/ui/` with exactly three files: index.html, app.js, style.css.
2. Only UI code in repo. No app contains its own UI.
3. Reads `/config` on load.
4. Renders same card layout, badges, dialog, health bar for all apps.
5. **Domain rendering via registry:**
   ```js
   const RENDERERS = {
     database: renderDatabaseEvidence,
     api:      renderApiEvidence,
     cache:    renderCacheEvidence,
   };
```

Template owns layout/badges/dialogs/state. Renderers own only the
evidence body inside a card.

6. Apps differ only via config.domain, config.accent, config.title.
7. Changing the template affects all apps simultaneously.

Requirement 30: Applications in Separate Folders

1. apps/schema-drift/ and apps/contract-observer/ — both MANDATORY.
2. apps/poltergeist/ — SHIPPED third app (cache domain).
3. Each app folder contains: main.py (~30 lines importing core + domain
   adapter), config.json, README.md, run.sh.
4. main.py imports from core/ and its domain adapter. No domain logic.
5. config.json specifies domain, accent, title.
6. run.sh starts only that app.
7. README describes domain, users, how to run.
8. Apps visually distinct (domain, accent, title, verb) but share template.
9. test_apps_structure.py accepts 2 or 3 apps; verifies each existing app
   has 4 required files and shared UI has 3 files.

Requirement 31: Haunted Console Visuals

Full Halloween theme (implemented — supersedes plain-console original):
- Void black bg (#0a0a0f), pumpkin orange accent (#f97316), blood red blocked (#ef4444),
  spirit green resolved (#4ade80), amber review (#fbbf24).
- Ghost vocabulary: drift kinds shown as Halloween names (Type Wraith, Phantom Index, etc.).
- State badges with emoji + Halloween text (👻 HAUNTING, ⛓ BOUND, ✝ LAID TO REST, etc.).
- Atmospheric page: 3-layer radial fog + corner vignettes + mist strip header separator.
- Ghost name displayed large (28px bold) with state-colored glow text-shadow.
- Card top-bar: 2px glowing accent line per state color.
- Per-state card background: dark tinted gradient, single CSS rule via --state-bg-tint var.
- Roaming ghost sprites: 👻🕷️🦇💀 drift across screen on compare (proportional to unresolved count).
- Idle state: floating ghost + tombstone divider + "The crypt is quiet… for now."
- Scan-line animation fires once on load (animation-iteration-count: 1; forwards fill).
- Ghost flicker on PENDING cards. 50ms card entrance stagger. Five+ animations total.

Requirement 32: Health Bar and Cascade

Header: {resolved} of {total} resolved · {review} review · {blocked} blocked
· {superseded} superseded. Bar fill = resolved/total. Transitions on apply
success or execution failure. Red <40, amber 40–79, green 80+.

Requirement 33: Cache Version and Provenance

Cache entries carry cache_version, validator_contract, prompt_hash.
Lookup skips on mismatch. Skipped entries fall through to live.
test_cache_reproducibility.py runs twice, asserts semantic outcomes match
by signature, UUIDs differ, second run marked cached=True.

Requirement 34: Dependency Verifier Evidence (optional)

Re-read manifest. Evidence: manifest_before/after, installed_version,
declaration_updated=True, runtime_changed=False, compatibility_verified=False.
UI: "Declaration repaired and verified. Runtime state unchanged. Compatibility
not verified."

Requirement 35: Kiro Hooks (optional)

Three hooks. Verify trigger names, matcher syntax, payload fields against
installed Kiro on day 1. Fire each hook manually before relying.
new-adapter scaffolds app folder + test + renderer stub — does NOT
implement adapters, does NOT modify core/.

Requirement 36: CI (optional)

GitHub Actions on pull_request when models.py, migrations/**, or openapi.yaml
change. Postgres service container, run compare, PR comment. Never applies.

Requirement 37: Deployment

docker compose up brings up Postgres + demo API + apps in ≤30s.
Third app behind a compose profile, NOT started by default. One URL per
mandatory app. Sandbox DB read-only or reseeded nightly. Apps accessible
until Jan 29, 2027.

Requirement 38: Category Proof Artifacts

One freeze commit touching core/ after week 2 (plus week-1 initial). README
"Skeleton proof" section with three verification commands. test_skeleton.py
asserts 7 files and no external imports. README states git log proves commit
discipline, NOT architectural completeness. GitHub LICENSE (MIT) visible in
About section.

Requirement 39: Repeatability and Demo Mode

--demo loads cache keyed by signature. Every demo ghost hits cache. No
live Bedrock or MCP during recording. test_demo_repeatability.py runs twice,
asserts identical semantic outcomes by signature. Cache hits marked
cached=True with CACHED badge.

Requirement 40: Skeleton Crew Structural Compliance

1. At least two app folders under apps/, each containing runnable app.
2. core/ as clearly identifiable shared skeleton.
3. git log --oneline core/ shows only freeze commit.
4. git diff core/ empty after all apps.
5. README "Skeleton proof" section: what the skeleton is, what the apps are,
   the three verification commands.
6. Apps share one UI template and differ only via config.

Requirement 41: Property-Based Testing (Non-Tautological)

1. tests/test_properties.py using hypothesis.
2. Type normalizer:
   · Reference table: 30+ hand-verified (sa_type, pg_type, expected_canonical)
     triples all produce expected
   · Invariant: any supported type produces non-empty canonical string
   · Unsupported type raises defined error
   · Round-trip: normalize_sa(sa) == normalize_pg(pg) for every reference pair
3. SQL validator (positive + negative):
   · Generated valid SQL for every auto-fixable matrix row with matching
     target → accepted
   · Mutated SQL with wrong target, extra statement, USING, expression index,
     partial index, destructive op, SET statement, or mismatched operation
     type → rejected
   · Generator produces SQL that executes against real Postgres — verify
     by running against test fixture
4. State machine:
   · Enumerate 7 states × 8 events = 56 cells; assert each matches documented
     transition table
   · No sequence transitions BLOCKED→PENDING/RESOLVED,
     REVIEW_REQUIRED→PENDING, EXECUTION_FAILED→PENDING
   · validator_status != "valid" implies state is never PENDING after any
     sequence
   · Rollback of resolved file-domain does NOT change state to PENDING
   · Persisted state matches in-memory state after every transition
5. Redaction (key variants):
   · authorization/Authorization, client_secret/clientSecret/CLIENTSECRET,
     private_key/privateKey/privatekey all match
   · monkey, accessibility, author do NOT match
   · Generated key mutations from base words (authorization, secret, token,
     password, key) with camelCase/underscore/hyphen variants
6. Submission write-up states: hypotheses generated per property, and at
   least one failing case PBT found that unit tests missed.

Requirement 42: SSRF Test Coverage

tests/test_ssrf.py:

· Blocked: 127.0.0.1, localhost, 169.254.169.254, 10.0.0.1, 192.168.1.1,
  [::1], [fe80::1], [::ffff:127.0.0.1], internal.corp resolving to private
· Redirect to blocked range refused
· DNS-rebinding attack: first getaddrinfo returns public, second returns
  private → pinned IP used for connection, no re-resolution
· Proxy env vars ignored (trust_env=False)
· --allow-local permits 127.0.0.1/::1 but not other private ranges
· IPv6 pinned URL bracketed correctly
· Non-standard port preserved across redirect
· Relative Location headers resolved against current URL

Requirement 43: Redaction Test Coverage

tests/test_redaction.py:

· Headers: Authorization, Cookie, X-API-Key, X-Amz-Security-Token
· Query strings: token, api_key, secret, client_secret
· URL userinfo
· Form bodies
· JSON depth 5 (redacted) vs depth 6 (subtree discarded)
· Error evidence: stores {code, class, summary} NOT raw exception
· Token boundaries: monkey/accessibility/author NOT redacted;
  api_key/apiKey/clientSecret redacted
· _redacted suffix only on redacted fields
· Redaction happens before persistence (verify via fixture)

```

---
