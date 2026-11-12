# Touchstone

> Every system has ghosts — tables, fields, and dependencies that exist in one state but not the other. Touchstone finds them, fixes them, and proves it.

**Kiroween 2026 — Skeleton Crew category**

---

## What it does

Two states exist for every system: what the code declares, and what the system actually runs. The gaps are ghosts.

| Domain | Declared | Actual |
|---|---|---|
| Database | SQLAlchemy models | Live Postgres schema |
| API contracts | `openapi.yaml` | One live response per endpoint |
| Cache | `cache_config.yaml` | Live Redis key list |

Touchstone detects each ghost, proposes a validated fix, and — on human approval — applies it atomically and verifies the result.

**The skeleton is exactly 7 files.** Three applications (`schema-drift`, `contract-observer`, and `poltergeist`) run on top of it without modifying any skeleton file. Two mandatory apps satisfy the Skeleton Crew entry.

---

## The apps

### App 1 — Schema Drift · *The Crypt* · port 8001

Compares SQLAlchemy model declarations against a live Postgres schema. Ghosts are columns, tables, indexes, and type mismatches that exist in one side but not the other.

**Demo drift cases (pre-seeded in Neon DB):**

| Ghost | Kind | Policy | Badge |
|---|---|---|---|
| `users.age` declared `INTEGER`, live `BIGINT` | `TYPE_CHANGED` (narrowing) | REVIEW_REQUIRED | 🕯 SÉANCE NEEDED |
| `ix_orders_user_id` declared, missing from live DB | `INDEX_MISSING` | AUTO-FIX | 👻 HAUNTING |
| `users.email` declared `VARCHAR(255)`, live `VARCHAR(50)` | `TYPE_CHANGED` (widening) | AUTO-FIX | 🕯 SÉANCE NEEDED |

Judges can click **Exorcise** to apply a fix directly to the Neon DB. Click **↺ Reset Demo** to re-seed drift and start over.

---

### App 2 — Contract Observer · *The Séance* · port 8002

Probes a live API and compares each response against the declared `openapi.yaml`. Ghosts are fields that appear in the live response but not the spec (Shadow Fields), and fields declared in the spec that the live API no longer returns (Vanished Fields).

**Demo drift cases (live on Render):**

| Ghost | Kind | Badge | Action |
|---|---|---|---|
| `account_status` returned by `/users`, not in spec | `FIELD_EXTRA` → Shadow Field | ⛓ BOUND | Opens PR on `openapi.yaml` |
| `sku` returned by `/products`, not in spec | `FIELD_EXTRA` → Shadow Field | ⛓ BOUND | Opens PR on `openapi.yaml` |
| `loyalty_tier` declared in spec, not returned by `/users` | `FIELD_MISSING` → Vanished Field | 🕯 SÉANCE NEEDED | Opens PR on `main.py` |

The live API is deployed at **[touchstone-demo-target-repo.onrender.com](https://touchstone-demo-target-repo.onrender.com)** (Render free tier — warms up automatically on first Hunt Ghosts). The declared spec lives in **[NithinBR-AI/touchstone-demo-target-repo](https://github.com/NithinBR-AI/touchstone-demo-target-repo)**.

Judges can click **Exorcise** on any drift card to open a GitHub PR. All drifts of the same kind are batched into **one PR per file** — Shadow Fields → one PR on `openapi.yaml`, Vanished Fields → one PR on `main.py`. Clicking Exorcise on an already-open touchstone PR returns the existing PR (idempotent). **Reset Demo** closes all open touchstone PRs so the next run produces a fresh PR.

---

### App 3 — The Poltergeist · *Redis Cache Drift Detector* · port 8003

Compares declared Redis cache keys in `cache_config.yaml` against keys that actually exist in the live cache. Ghosts are keys that should exist but don't (KEY_MISSING), and keys that exist in the live cache but were never declared (UNDECLARED_KEY).

**Demo drift cases (live on Render):**

| Ghost | Kind | Policy | Badge |
|---|---|---|---|
| `products:list` declared, absent from live cache | `KEY_MISSING` | AUTO-FIX | 👻 HAUNTING |
| `debug:session_dump` present in live cache, not declared | `UNDECLARED_KEY` | BLOCKED | ⛓ BOUND |

Judges can click **Exorcise** on `KEY_MISSING` ghosts to warm the missing key via `/cache/warm?key=<key>`. Click **↺ Reset Demo** to restore the drift state. `UNDECLARED_KEY` ghosts require manual investigation — no auto-fix.

---

## Architecture — the skeleton

The skeleton is `core/` — 7 files, frozen after week 2. It implements the 10-step compare-verify workflow and knows nothing about any specific domain.

```
core/
├── interfaces.py    ← 6 protocols: InputSource, SourceAdapter, Comparator,
│                       Resolver, Executor, Verifier
├── models.py        ← GhostState (7 states), Difference, Resolution,
│                       SQLChange, FilePatch, ExecutionResult,
│                       VerificationResult, ApplyOutcome, RollbackEvent, Report
├── validator.py     ← domain validator (operation matrix + safety rules)
├── cache.py         ← JSON-backed resolution cache; written only after apply+verify succeeds;
│                       keyed by Difference.signature with cache_version, validator_contract,
│                       prompt_hash; 7-day TTL (expires_at); null proposals never cached
├── persistence.py   ← atomic report store (temp + fsync + rename)
└── orchestrator.py  ← state machine: run() / apply_one() / rollback()
```

Each domain adapter implements the 6 protocols. The skeleton calls the protocols; it never imports from adapters.

```
adapters/postgres/    ← Schema Drift (complete)
adapters/openapi/     ← Contract Observer (complete)
adapters/redis/       ← The Poltergeist (complete)

apps/schema-drift/    ← mandatory app 1 — The Crypt
apps/contract-observer/ ← mandatory app 2 — The Séance
apps/poltergeist/     ← third app — The Poltergeist
apps/shared/ui/       ← one Halloween-themed template + RENDERERS registry
```

Adding a domain requires implementing the adapter protocols. It never requires touching `core/`.

---

## State machine

Seven states × eight events = 56 cells. Every cell is explicitly defined and tested.

| State | Meaning |
|---|---|
| `PENDING` | Difference found; resolution not yet proposed |
| `REVIEW_REQUIRED` | Non-auto-fixable; waiting for human decision |
| `BLOCKED` | Structurally rejected by the validator |
| `RESOLVED` | Applied and verified |
| `STILL_PRESENT` | Applied but verifier found the ghost persists |
| `EXECUTION_FAILED` | Executor rejected or timed out |
| `SUPERSEDED` | A later run resolved a previously open ghost |

---

## Flow

```
1. Read targets         (repo config → declared source URL + live environment URL)
2. Read both sides      (SourceAdapter normalizes each into a State)
3. Compare              (domain Comparator classifies each gap as a Difference)
4. Resolve              (LLM Resolver proposes one fix per ghost)
5. Validate             (Validator enforces domain safety rules; rejects if violated)
6. Authorize            (human reviews → approves or dismisses)
7. Execute              (Executor applies the fix atomically)
8. Verify               (Verifier re-reads live state → RESOLVED / STILL_PRESENT / EXECUTION_FAILED)
```

**LLM proposes. Validator decides. Human authorizes. That order never changes.**

---

## Ghost classification

### Database (Schema Drift)

| Kind | Auto-fix | Policy |
|---|---|---|
| `INDEX_MISSING` | `CREATE INDEX` | AUTO-FIX |
| `TYPE_CHANGED` (widening only) | `ALTER COLUMN TYPE` | AUTO-FIX |
| `TYPE_CHANGED` (narrowing) | — | REVIEW_REQUIRED |
| `COLUMN_MISSING` | `ADD COLUMN` | REVIEW_REQUIRED |
| `TABLE_MISSING`, `COLUMN_EXTRA`, `TABLE_EXTRA`, `INDEX_EXTRA` | — | BLOCKED |

Widening pairs: `smallint→integer`, `integer→bigint`, `real→double precision`, `varchar(n)→varchar(m) where m>n`, `varchar(n)→text`. Everything else is narrowing and fails closed.

### API contracts (Contract Observer)

| Kind | UI label | Policy |
|---|---|---|
| `FIELD_EXTRA` | Shadow Field | BLOCKED — PR-based fix to `openapi.yaml` |
| `FIELD_MISSING` | Vanished Field | REVIEW_REQUIRED — PR-based fix |
| `PATH_MISSING` | Lost Endpoint | BLOCKED |
| `PATH_EXTRA` | Phantom Endpoint | BLOCKED |

API contract changes are never auto-applied to a live system. The executor opens a GitHub PR.

### Cache keys (The Poltergeist)

| Kind | UI label | Policy |
|---|---|---|
| `KEY_MISSING` | Missing Key | AUTO-FIX — warms the key via `/cache/warm?key=<key>` |
| `UNDECLARED_KEY` | Shadow Key | BLOCKED — requires manual investigation |

---

## Safety boundaries

### All domains
- **Validator is the safety boundary** — not the LLM, not keyword matching. The domain validator enforces the operation matrix structurally.
- **Redaction** — token-based, applied before persistence and before the resolver sees the data. Errors stored as code/class/summary — never raw exception text.
- **Atomic writes** — executor uses temp file + fsync + rename within the same directory fd. File patches are hash-locked (`before_sha256` verified before apply, `after_sha256` verified after).

### Schema Drift (database)
- **Operation matrix** — only 9 operation kinds recognized; every other SQL node is blocked.
- **Target binding** — validator extracts table/column/index from the SQL AST and compares against `Difference.target`; a mismatch blocks regardless of resolver confidence.
- **Connection-level timeout** — 30s via `options="-c statement_timeout=30000"` at connect time; never via `SET`.
- **USING clause / expression index / partial index** — structurally rejected.

### Contract Observer (API)
- **SSRF protection** — all outbound HTTP blocks loopback, private, link-local, reserved, and IPv4-mapped-IPv6 destinations. Validated before every request.
- **`follow_redirects=False`** — redirects are not auto-followed. Same-host redirects are manually followed (max 5 hops) with SSRF revalidation on each hop.
- **`trust_env=False`** — no proxy env vars.
- **No live mutations** — the executor opens a GitHub PR; it never patches the live API.

---

## How Kiro was used

### Spec-driven development

The entire project was driven from a single spec written before any code existed:

- `.kiro/specs/touchstone/requirements.md` — 40 numbered requirements across four categories: core skeleton, database domain, API domain, deps domain
- `.kiro/specs/touchstone/design.md` — state machine diagram, 56-cell transition table (7 states × 8 events), operation matrix, cache key design, SSRF rules
- `.kiro/specs/touchstone/tasks.md` — phased implementation plan with a freeze gate at task 3.2; each completed task updated with a `VERIFIED:` note showing test counts

The difference from vibe coding was structural: the spec forced resolution of ambiguous cases before implementation began. For example, "what happens when a narrowing type change is proposed?" was answered in requirements (`REVIEW_REQUIRED`, `proposed_change=None`) before the resolver or validator were written.

### Steering docs

Three steering docs guided every Kiro response throughout the build:

- **`product.md`** — 13 non-negotiable principles written on day 1. The most consequential: "LLM proposes; validator decides; human authorizes" and "Core frozen at end of week 2." These prevented scope creep in both directions.
- **`tech.md`** — stack, operation matrix, SSRF rules, redaction contract, type widening table. The widening pairs spelled out here meant the validator implementation required no judgment calls.
- **`kiro-features.md`** — documented which Kiro features to use and how, including the PBT strategy, the custom agent contract, and what the submission write-up must cover.

The biggest single impact: writing `product.md` before writing any code forced the product boundary to be explicit. When implementation revealed a tension (e.g., "should rollback generate an inverse SQL statement?"), the steering doc had already answered it ("NO — preserve the observed state; no inverse generated").

### Custom agent — Validator Reviewer

`.kiro/agents/validator-reviewer.md` defines an independent safety auditor with a strict input contract. The agent receives no resolver rationale, no confidence score, and no context that could bias its evaluation. It re-derives its verdict from the operation matrix independently.

**Advisory only**: the deterministic validator in `core/validator.py` is authoritative. The agent's verdict is recorded alongside the validator result. Disagreements escalate to human review — the agent cannot approve what the matrix blocks, and can flag concerns the matrix cannot express (e.g., a widening that is technically valid but suspiciously large for a typical identifier column).

### Agent hooks

**`new-adapter`** (trigger: `FileCreate` on `adapters/**/comparator.py`)  
Scaffolds the full application plumbing when a new domain comparator is created: `apps/<domain>/main.py`, `config.json`, `README.md`, `run.sh`, a renderer stub in the shared UI `RENDERERS` registry, and a comparator test file. The hook prompt explicitly prohibits modifying `core/`.

**`comparator-test`** (trigger: `FileSave` on `adapters/**/comparator.py`)  
Runs the matching comparator test suite on every save. Resolves the test file from the domain folder name — `adapters/postgres/comparator.py` triggers `tests/test_comparator_pg.py`.

**`demo-case-cache`** (trigger: `FileSave` on `fixtures/cases.json`)
Invokes the resolver against each new demo case. Cache entries are written organically by `apply_one()` after verification succeeds — not by this hook directly. The hook seeds demo drift so the apply+verify path can populate the cache naturally.

### MCP — Context7

The resolver fetches library documentation from Context7 MCP before proposing a fix — grounding proposals in documented behavior rather than training data recall. If Context7 is unreachable, the resolver proceeds without docs and forces `confidence: LOW`. MCP improves proposal quality; the validator enforces safety independently.

### Property-based testing (PBT)

`tests/test_properties.py` uses Hypothesis to verify invariants that unit tests cannot enumerate:

- **Type normalizer** — round-trip: every supported Postgres type normalizes to a canonical string that is stable across repeated calls
- **SQL validator** — positive generator (every auto-fixable matrix row → must be accepted) + negative generator (mutate any part of valid SQL → must be rejected)
- **State machine** — enumerate all 56 cells; invariant: no sequence can reach `PENDING` from `BLOCKED` or `REVIEW_REQUIRED`
- **Redaction** — token boundary coverage across case variants (`authorization`, `Authorization`, `AUTHORIZATION`, etc.)

PBT caught one real defect: the initial state machine allowed a `REVIEW_REQUIRED → PENDING` transition via a specific two-step event sequence that unit tests (which only tested one event at a time) missed.

---

## Demo

Target repos are declared in **`repos.json`** at the repo root. No env var, no cloning. Touchstone fetches declared sources from configured GitHub raw URLs at runtime. The UI shows a **repo selector** in the header — judges can see that Touchstone is built to monitor multiple repos.

```bash
# Restore Schema Drift demo state (or use the Reset Demo button in the UI)
python fixtures/seed.py $DATABASE_URL

# Run offline tests (no DB needed)
python -m pytest tests/ -q
# 382 passed, 11 skipped

# Run Postgres integration tests
DATABASE_URL=<neon-url> python -m pytest tests/test_postgres_e2e.py tests/test_sql_execution.py -v
# 9 passed, 1 skipped
```

---

## Project structure

```
touchstone/
├── core/                                 # frozen — 7 files
│   ├── interfaces.py
│   ├── models.py
│   ├── validator.py
│   ├── cache.py
│   ├── persistence.py
│   └── orchestrator.py
├── repos.json                            # target repos registry
├── adapters/
│   ├── remote_source.py                  # fetches files from GitHub raw URLs
│   ├── postgres/                         # schema-drift domain (complete)
│   │   ├── normalize.py
│   │   ├── intended.py
│   │   ├── actual.py
│   │   ├── comparator.py
│   │   ├── executor.py
│   │   └── verifier.py
│   ├── openapi/                          # contract-observer domain (complete)
│   │   ├── ssrf.py
│   │   ├── intended.py
│   │   ├── actual.py
│   │   ├── comparator.py
│   │   ├── executor.py
│   │   └── verifier.py
│   └── redis/                            # poltergeist domain (complete)
│       ├── actual.py
│       ├── intended.py
│       ├── comparator.py
│       ├── executor.py
│       └── verifier.py
├── agents/
│   ├── resolver.py                       # MockResolver (real Bedrock resolver: task 7.1)
│   ├── redaction.py                      # token-based redaction + SafeError
│   └── prompts.py
├── apps/
│   ├── shared/ui/                        # one shared Halloween-themed template
│   │   ├── index.html
│   │   ├── app.js
│   │   └── style.css
│   ├── schema-drift/                     # mandatory app 1 — The Crypt (complete)
│   │   ├── main.py
│   │   ├── config.json
│   │   ├── README.md
│   │   └── run.sh
│   ├── contract-observer/               # mandatory app 2 — The Séance (complete)
│   │   ├── main.py
│   │   ├── config.json
│   │   ├── README.md
│   │   └── run.sh
│   └── poltergeist/                     # third app — The Poltergeist (complete)
│       ├── main.py
│       ├── config.json
│       ├── README.md
│       └── run.sh
├── fixtures/
│   ├── seed.py                           # seeds Neon DB with drift cases
│   ├── cases.json                        # drift cases for demo + cache seeding
│   └── docker-compose.yml               # local Postgres 16 alternative
├── cli.py                                # CLI: touchstone compare / touchstone report
├── pyproject.toml
├── tests/
│   ├── test_skeleton.py                  # 7 files, no external imports (15 tests)
│   ├── test_sqlglot_ast_assumptions.py   # pinned AST shapes (23 tests)
│   ├── test_normalize.py                 # type normaliser (91 tests)
│   ├── test_validator_sql.py             # matrix + binding + widening (33 tests)
│   ├── test_validator_file.py            # symlink / hash / key / traversal (12 tests)
│   ├── test_state_transitions.py         # 56-cell state machine (66 tests)
│   ├── test_rollback.py                  # file rollback + DB refusal (10 tests)
│   ├── test_cache_reproducibility.py     # cache hit / miss / provenance (8 tests)
│   ├── test_orchestrator_mock.py         # end-to-end with fake infrastructure (5 tests)
│   ├── test_resolver.py                  # MockResolver + canned resolutions (9 tests)
│   ├── test_ssrf.py                      # SSRF guard (13 tests)
│   ├── test_comparator_api.py            # OpenAPI comparator (8 tests)
│   ├── test_redaction.py                 # token-based redaction (49 tests)
│   ├── test_comparator_cache.py          # Redis cache comparator (9 tests)
│   ├── test_comparator_pg.py             # Postgres comparator (requires DATABASE_URL)
│   ├── test_executor_pg.py               # Postgres executor (requires DATABASE_URL)
│   ├── test_verifier_pg.py               # Postgres verifier (requires DATABASE_URL)
│   ├── test_postgres_e2e.py              # full end-to-end (requires DATABASE_URL)
│   ├── test_sql_execution.py             # real Postgres SQL execution
│   ├── test_apps_structure.py            # Skeleton Crew proof: 2+ apps, 4 files each
│   └── test_properties.py               # Hypothesis PBT
├── .kiro/
│   ├── specs/touchstone/                 # requirements.md, design.md, tasks.md
│   ├── steering/                         # product.md, tech.md, kiro-features.md
│   ├── hooks/                            # new-adapter, comparator-test, demo-case-cache
│   ├── agents/                           # validator-reviewer.md
│   └── settings/mcp.json                # Context7 MCP (pinned version)
└── .github/workflows/                    # drift.yml CI
```

---

## Setup

```bash
git clone <repo>
cd touchstone

python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

pip install -e ".[dev]"
```

Copy `.env_sample` to `.env` and fill in your Neon connection string:

```
DATABASE_URL=postgresql://neondb_owner:<password>@<host>/neondb?sslmode=require
```

### Start the apps

```bash
# Schema Drift — The Crypt (port 8001)
bash apps/schema-drift/run.sh

# Contract Observer — The Séance (port 8002)
bash apps/contract-observer/run.sh

# The Poltergeist — Redis Cache Drift (port 8003)
bash apps/poltergeist/run.sh
```

### CLI

```bash
touchstone compare --target ix_orders_user_id
touchstone compare --target users.email
touchstone report
```

---

## Skeleton proof

The skeleton is `core/` — 7 files, frozen after week 2. Two mandatory applications run on top of it without modifying any skeleton file.

**What the skeleton is:** `core/interfaces.py` defines six protocols (`InputSource`, `SourceAdapter`, `Comparator`, `Resolver`, `Executor`, `Verifier`). The remaining six files implement the 10-step compare-verify workflow and know nothing about Postgres, OpenAPI, or `pyproject.toml`.

**What the apps are:** Each app is a ~30-line `main.py` that imports from `core/` and its domain adapter, a `config.json` (domain, accent, title), a `README.md`, and a `run.sh`. No domain logic lives in `core/`.

**Verification commands:**

```bash
# 1. Confirm core/ has not changed since the freeze commit
git log --oneline core/

# 2. Confirm core/ has no uncommitted changes
git diff core/

# 3. Assert 7 files and no imports from outside core/
python -m pytest tests/test_skeleton.py -v
```

`git log` proves commit discipline. The import rule is enforced structurally by `test_skeleton.py`, which walks every module in `core/` and asserts no import resolves to a module outside `core/`.

---

## License

MIT
