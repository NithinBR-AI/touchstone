# Touchstone

**Schema drift is invisible until your DB migration fails in production. API contract drift is invisible until a client breaks. Cache drift is invisible until you wonder why your keys aren't there.**

Touchstone makes all three visible — and fixable — before they cause incidents. It compares what your code declares against what actually runs, classifies every gap deterministically, calls an LLM to propose one validated fix per ghost, and — on human approval — applies it atomically and re-verifies the result.

**Kiroween 2026 — Skeleton Crew category** · Built with NVIDIA and DeepSeek models via [AWS Bedrock Mantle](https://aws.amazon.com/bedrock/)

---

## What it does

Two states exist for every system: what the code declares, and what the system actually runs. The gaps are ghosts.

| Domain | Declared | Actual | Ghost example |
|---|---|---|---|
| Database | SQLAlchemy models | Live Postgres schema | Index declared in models, missing from live DB |
| API contracts | `openapi.yaml` | Live API response | Field returned by API, absent from spec |
| Cache | `cache_config.yaml` | Live Redis key list | Key declared in config, missing from live cache |

Touchstone detects each ghost, proposes a validated fix using an LLM grounded in the diff context, and — on human approval — applies it atomically and verifies the result. Every step is audited. Every fix is proven before it closes.

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

The live API is deployed at **[touchstone-demo-target-repo.onrender.com](https://touchstone-demo-target-repo.onrender.com)** (Render free tier — may take ~30s to wake on first request). The declared spec lives in **[NithinBR-AI/touchstone-demo-target-repo](https://github.com/NithinBR-AI/touchstone-demo-target-repo)**.

Judges can click **Exorcise** on any drift card to open a GitHub PR. All drifts of the same kind are batched into **one PR per file** — Shadow Fields → one PR on `openapi.yaml`, Vanished Fields → one PR on `main.py`. Clicking Exorcise on an already-open touchstone PR returns the existing PR (idempotent). **Reset Demo** closes all open touchstone PRs so the next run produces a fresh PR.

---

### App 3 — The Poltergeist · *Redis Cache Drift Detector* · port 8003

Compares declared Redis cache keys in `cache_config.yaml` against keys that actually exist in the live cache. Ghosts are keys that should exist but don't (KEY_MISSING), and keys that exist in the live cache but were never declared (UNDECLARED_KEY).

**Demo drift cases (live on Render):**

| Ghost | Kind | Policy | Badge |
|---|---|---|---|
| `products:list` declared, absent from live cache | `KEY_MISSING` | AUTO-FIX | 👻 HAUNTING |
| `debug:session_dump` present in live cache, not declared | `UNDECLARED_KEY` | AUTO-FIX | 👻 HAUNTING |

Judges can click **Exorcise** on either ghost. `KEY_MISSING` → warms the key via `/cache/warm`. `UNDECLARED_KEY` → deletes the key via `/cache/delete`. Verifier confirms the action took effect before marking RESOLVED. Click **↺ Reset Demo** to restore both drift cases.

---

## Architecture — the skeleton

The skeleton is `core/` — 7 files. It implements the compare-verify workflow and knows nothing about any specific domain.

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

## Models — AWS Bedrock Mantle

All LLM calls route through **[AWS Bedrock Mantle](https://aws.amazon.com/bedrock/)** — a single OpenAI-compatible endpoint (`https://bedrock-mantle.us-east-1.api.aws/v1`) that exposes NVIDIA and DeepSeek models under one API key. The client is 80 lines in `agents/mantle_client.py`, adapted from the same pattern used in [GhostVendor](https://github.com/NithinBR-AI/ghostvendor).

| App | Domain | Model | Why |
|---|---|---|---|
| App 1 — Schema Drift | `database` | **Qwen3-235B** (`qwen.qwen3-235b-a22b-2507`) via `ultra()` | SQL fix proposals require multi-step reasoning: trace the diff context, verify the identifier is safe to use unquoted, produce exactly one DDL statement matching the target name. The 235B MoE handles all three in one pass reliably. |
| App 2 — Contract Observer | `api` | **DeepSeek v3.2** (`deepseek.v3.2`) via `deepseek_pro()` | API patch generation is deferred to PR-creation time (inside `ContractExecutor.execute_batch()`). The resolver skips LLM for api domain at resolve time — the executor calls `deepseek_pro()` to generate the patched file content when opening the GitHub PR. |
| App 3 — The Poltergeist | `cache` | **No LLM** (`MockResolver`) | Cache key warming is fully deterministic — the executor warms a missing key via `/cache/warm?key=<key>`. No reasoning is needed; `MockResolver` satisfies the resolver protocol without making any network calls. |

**Fallback chain:** `ultra()` falls back to Nemotron Super (`nvidia.nemotron-super-3-120b`) on transient Mantle error. `deepseek_pro()` falls back to Qwen3-235B. Both are on the same Mantle endpoint — the fallback is a model name swap only.

**Temperature:** `0.0` on all calls — fix proposals must be deterministic. The same ghost must always produce the same SQL or rationale so the prompt SHA-256 is a stable cache key.

**No key → no problem:** If `MANTLE_API_KEY` is not set, `MantleResolver` automatically falls back to `MockResolver` and logs a warning. The apps start and scan normally; only the fix proposals are canned rather than LLM-generated.

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
| `UNDECLARED_KEY` | Shadow Key | AUTO-FIX — deletes the key via `/cache/delete?key=<key>` |

---

## Safety boundaries

### All domains
- **Validator is the safety boundary** — not the LLM, not keyword matching. The domain validator enforces the operation matrix structurally.
- **Redaction** — token-based, applied before persistence and before the resolver sees the data. Errors stored as code/class/summary — never raw exception text.
- **Atomic writes** — executor uses temp file + fsync + rename within the same directory fd. File patches are hash-locked (`before_sha256` verified before apply, `after_sha256` verified after).

### Schema Drift (database)
- **Operation matrix** — only recognized operation kinds are accepted; every other SQL node is blocked.
- **Target binding** — validator extracts table/column/index from the SQL AST and compares against `Difference.target`; a mismatch blocks regardless of resolver confidence.
- **Connection-level timeout** — 30s via `options="-c statement_timeout=30000"` at connect time; never via `SET`.
- **USING clause / expression index / partial index** — structurally rejected.

### Contract Observer (API)
- **SSRF protection** — all outbound HTTP blocks loopback, private, link-local, reserved, and IPv4-mapped-IPv6 destinations. Validated before every request.
- **`follow_redirects=False`** — redirects are not auto-followed. Same-host redirects are manually followed (max 5 hops) with SSRF revalidation on each hop.
- **`trust_env=False`** — no proxy env vars.
- **No live mutations** — the executor opens a GitHub PR; it never patches the live API.

---

## Key Design Decisions

**Core is the authority.** `core/` owns the state machine, validator, cache, and persistence. All domain logic lives in adapters — `core/` never imports from adapters. This is enforced structurally: `test_skeleton.py` asserts the 7-file count and no-external-import rule on every test run. The constraint means adapters can be replaced or added without touching the shared workflow.

**Validator is the safety boundary — not the LLM.** The LLM proposes; `core/validator.py` enforces. The validator re-derives its verdict from the operation matrix using `sqlglot` AST analysis — it doesn't trust the LLM's SQL string. A hallucinated `DROP TABLE` after a valid `CREATE INDEX` is caught by the matrix, not by prompt engineering.

**Target binding.** For database fixes, the validator extracts the table/column/index name from the SQL AST and compares it to `Difference.target`. A mismatch blocks regardless of model confidence. The LLM cannot create an index on a different table by omitting the target from its rationale.

**Cache written only after apply + verify.** The resolution cache (`core/cache.py`) is never written at resolve time. It's only written after `apply_one()` succeeds and `VerificationResult.resolved=True`. Null `proposed_change` entries are never cached. This means cached resolutions are proven — not speculative.

**API fixes are PRs, never live mutations.** The Contract Observer executor opens a GitHub PR — it never patches the live API or writes to disk. This is enforced architecturally: `ContractExecutor.execute()` calls the GitHub API, not a local file writer. The verifier re-reads the OpenAPI spec and checks whether the drift field is now present; if the PR has not yet been merged, the ghost stays open.

**Batch by kind, one PR per file.** When multiple API drifts of the same kind exist (e.g., three `FIELD_EXTRA` drifts), the executor groups them into one PR on `openapi.yaml` — not three separate PRs that would conflict on the same file. `FIELD_EXTRA` and `FIELD_MISSING` produce separate PRs (they patch different files). Idempotent: if an open `touchstone/*` PR with the same title prefix already exists, its URL is returned immediately.

**Domain-aware prompts, temperature 0.** Each domain gets a different system prompt (`_DB_SYSTEM_PROMPT` for SQL rules, `_API_SYSTEM_PROMPT` for contract drift rationale, `_CACHE_SYSTEM_PROMPT` for cache drift rationale). Temperature is `0.0` on all calls — the same ghost must always produce the same output so the prompt SHA-256 is a stable cache key.

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

- **`product.md`** — 13 non-negotiable principles. The most consequential: "LLM proposes; validator decides; human authorizes" and "core/ never imports from adapters." These prevented scope creep in both directions.
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

`.kiro/settings/mcp.json` pins Context7 at `@upstash/context7-mcp@4.6.0`. Kiro uses it during development to look up library documentation (sqlglot, psycopg, httpx) before generating code — grounding responses in actual API behavior rather than training recall. Context7 is a Kiro dev-time tool; it is not called at runtime by any app or the resolver.

### Property-based testing (PBT)

`tests/test_properties.py` (planned — Req 41) will use Hypothesis to verify invariants across the type normalizer, SQL validator, state machine, and redaction layers.

---

## Demo

Target repos are declared in **`repos.json`** at the repo root. No env var, no cloning. Touchstone fetches declared sources from configured GitHub raw URLs at runtime. The UI shows a **repo selector** in the header — judges can see that Touchstone is built to monitor multiple repos.

```bash
# Restore Schema Drift demo state (or use the Reset Demo button in the UI)
python fixtures/seed.py $DATABASE_URL

# Run offline tests (no DB needed)
python -m pytest tests/ -q
# 426 passed, 5 skipped (live DB tests auto-skip when DATABASE_URL is unset)

# Run Postgres integration tests
DATABASE_URL=<neon-url> python -m pytest tests/test_sql_execution.py tests/test_executor_pg.py -v
```

---

## Project structure

```
touchstone/
├── core/                                 # 7 files — no imports from outside core/
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
│   ├── resolver.py                       # MantleResolver (Bedrock/Mantle LLM) + MockResolver fallback
│   ├── mantle_client.py                  # Mantle OpenAI-compatible client (ultra/deepseek_pro, temp=0)
│   ├── prompts.py                        # domain-aware system prompts (database/api/cache)
│   └── redaction.py                      # token-based redaction + SafeError
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
│   ├── test_skeleton.py                  # 7 files, no external imports
│   ├── test_sqlglot_ast_assumptions.py   # pinned AST shapes (23 tests)
│   ├── test_normalize.py                 # type normaliser
│   ├── test_validator_sql.py             # operation matrix + target binding + widening
│   ├── test_state_transitions.py         # 56-cell state machine
│   ├── test_rollback.py                  # DB refusal + rollback event recording
│   ├── test_cache_reproducibility.py     # cache hit / miss / provenance
│   ├── test_orchestrator_mock.py         # end-to-end with fake infrastructure
│   ├── test_resolver.py                  # MockResolver
│   ├── test_mantle_resolver.py           # MantleResolver: routing, prompts, retry, fallback (20 tests)
│   ├── test_executor_api.py              # ContractExecutor batch, idempotency, demo reset
│   ├── test_ssrf.py                      # SSRF guard
│   ├── test_comparator_api.py            # OpenAPI comparator
│   ├── test_redaction.py                 # token-based redaction
│   ├── test_comparator_cache.py          # Redis cache comparator
│   ├── test_comparator_pg.py             # Postgres comparator (requires DATABASE_URL)
│   ├── test_executor_pg.py               # Postgres executor (requires DATABASE_URL)
│   ├── test_sql_execution.py             # real Postgres SQL execution (requires DATABASE_URL)
│   ├── test_apps_structure.py            # Skeleton Crew proof: 3 apps, 4 files each
│   └── test_properties.py               # Hypothesis PBT (planned)
├── .kiro/
│   ├── specs/touchstone/                 # requirements.md, design.md, tasks.md
│   ├── steering/                         # product.md, tech.md, kiro-features.md
│   ├── hooks/                            # new-adapter, comparator-test, demo-case-cache
│   ├── agents/                           # validator-reviewer.md
│   └── settings/mcp.json                # Context7 MCP (pinned version)
└── .github/workflows/                    # drift.yml CI (planned)
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

Copy `.env_sample` to `.env` and fill in:

```
# Neon Postgres — required for App 1 (Schema Drift)
DATABASE_URL=postgresql://neondb_owner:<password>@<host>/neondb?sslmode=require

# GitHub token — required for App 2 (Contract Observer) to open PRs
GITHUB_TOKEN=ghp_...

# Upstash Redis — required for App 3 (The Poltergeist)
UPSTASH_REDIS_REST_URL=https://your-endpoint.upstash.io
UPSTASH_REDIS_REST_TOKEN=...

# Mantle LLM — required for App 1 and App 2 LLM fix proposals
# Without this, MantleResolver falls back to MockResolver (deterministic, no LLM)
MANTLE_API_KEY=...
MANTLE_BASE_URL=https://bedrock-mantle.us-east-1.api.aws/v1
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

The skeleton is `core/` — 7 files. The skeleton implements the compare-verify workflow and knows nothing about any specific domain.

**What the skeleton is:** `core/interfaces.py` defines six protocols (`InputSource`, `SourceAdapter`, `Comparator`, `Resolver`, `Executor`, `Verifier`). The remaining six files implement state management, validation, caching, and persistence.

**What the apps are:** Each app wires domain adapters to the orchestrator via `main.py`, plus a `config.json` (domain, accent, title), a `README.md`, and a `run.sh`. All domain logic lives in `adapters/`; `core/` owns only the workflow, state machine, and safety rules.

**Verification:**

```bash
python -m pytest tests/test_skeleton.py -v
```

`test_skeleton.py` asserts exactly 7 files in `core/` and that no module inside `core/` imports from `adapters/`, `agents/`, or `apps/`. This runs on every test pass.

---

## License

MIT
