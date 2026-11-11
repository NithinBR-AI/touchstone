# Touchstone

> Every system has ghosts — tables, fields, and dependencies that exist in one state but not the other. Touchstone finds them, fixes them, and proves it.

**Kiroween 2026 — Skeleton Crew category**

---

## What it does

Two states exist for every system: what the code declares, and what the system actually runs. The gaps are ghosts.

| Domain | Declared | Actual |
|---|---|---|
| Database | SQLAlchemy models | Live Postgres schema |
| API Contracts | `openapi.yaml` | One live request per endpoint |
| Dependencies | `pyproject.toml` / `requirements.txt` | Installed package versions |

Touchstone detects each ghost, proposes a validated fix, and — on human approval — applies it atomically and verifies the result.

**The skeleton is exactly 7 files.** Two mandatory applications (`schema-drift`, `contract-observer`) run on top of it, each a ~30-line wrapper. That is the Skeleton Crew entry.

---

## Flow

```
1. Read targets
2. Read both sides, normalize into State
3. Compare → classify each Difference
4. LLM Resolver proposes one fix per ghost (via Context7 MCP for doc-grounded SQL)
5. Validator independently enforces the operation matrix (SQL AST + path confinement + hash locks)
6. Validator Reviewer custom agent gives an advisory opinion
7. Human approves → Executor applies atomically (connection-level timeout, no SET statements)
8. Verifier re-reads live state → RESOLVED, STILL_PRESENT, or EXECUTION_FAILED
```

LLM proposes. Validator decides. Human authorizes. That order never changes.

---

## Ghost classification

| Ghost kind | Auto-fix | Policy |
|---|---|---|
| `INDEX_MISSING` | `CREATE INDEX` | Auto-fixable |
| `TYPE_CHANGED` (widening) | `ALTER COLUMN TYPE` | Auto-fixable |
| `TYPE_CHANGED` (narrowing) | — | REVIEW_REQUIRED |
| `COLUMN_MISSING` | `ADD COLUMN` | REVIEW_REQUIRED |
| `FIELD_UNDOCUMENTED` | `FILE_PATCH` to openapi.yaml | Auto-fixable |
| `TABLE_MISSING`, `COLUMN_EXTRA`, `TABLE_EXTRA`, `INDEX_EXTRA` | — | BLOCKED |

Anything not in the matrix fails closed. The validator enforces this from the SQL AST — not keyword matching.

---

## Safety boundaries

- **Operation matrix** — only 9 operation kinds recognized; every other SQL node is blocked
- **Target binding** — validator extracts table/column/index from the AST and compares against `Difference.target`; a mismatch blocks regardless of resolver confidence
- **Widening table** — `TYPE_CHANGED` only auto-fixes `smallint→integer`, `integer→bigint`, `real→double precision`, `varchar(n)→varchar(m) where m>n`, `varchar(n)→text`
- **USING clause / expression index / partial index** — structurally rejected
- **Connection-level timeout** — 30s via `options="-c statement_timeout=30000"` at connect time, never `SET`
- **File patches** — hash-locked (`before_sha256` verified before apply, `after_sha256` verified after), symlink check before resolve, path confined to project root
- **SSRF protection** — all outbound HTTP (Contract Observer) blocks loopback, private, link-local, reserved, and IPv4-mapped-IPv6 destinations; redirects re-validated and re-pinned at each hop; `trust_env=False`
- **Redaction** — token-based, applied before persistence and before the resolver sees the data; errors stored as code/class/summary — never raw exception text

---

## Demo — real Neon DB

The `demo/` directory contains a real Neon Postgres 18 database with three intentional drift cases:

| Case | Ghost | Policy |
|---|---|---|
| `ix_orders_user_id` absent | `INDEX_MISSING` | Auto-fix → `CREATE INDEX` |
| `users.email` is `VARCHAR(50)`, declared `VARCHAR(255)` | `TYPE_CHANGED` (widening) | Auto-fix → `ALTER COLUMN TYPE` |
| `users.age` is `BIGINT`, declared `INTEGER` | `TYPE_CHANGED` (narrowing) | REVIEW_REQUIRED |

```bash
# Restore drift state
python demo/seed.py $DATABASE_URL

# Run end-to-end against Neon
DATABASE_URL=<neon-url> python -m pytest tests/test_postgres_e2e.py tests/test_sql_execution.py -v
```

Result: **9 passed, 1 skipped** (cache test skips when drift is consumed by earlier tests in the same run — passes when seeded and run in isolation).

---

## How Kiro was used

### Spec-driven development

The entire project was driven from a single spec written before any code existed:

- `.kiro/specs/touchstone/requirements.md` — 38 numbered requirements across four categories: core skeleton, database domain, API domain, deps domain
- `.kiro/specs/touchstone/design.md` — state machine diagram, 56-cell transition table (7 states × 8 events), operation matrix, cache key design, SSRF rules
- `.kiro/specs/touchstone/tasks.md` — phased implementation plan: week 1 acceptance criteria (1.0a–1.0g), 23 section-2 tasks, freeze gate at task 3.2

The difference from vibe coding was structural: the spec forced resolution of ambiguous cases before implementation began. For example, the question "what happens when a narrowing type change is proposed?" was answered in the requirements doc (`REVIEW_REQUIRED`, `proposed_change=None`) before the resolver or validator were written. That made the 56-cell state machine test suite mechanically derivable from the spec — no test needed to guess behavior.

The tasks file became a living verification log: each task entry was updated with a `VERIFIED:` note showing test counts and specific findings, not just a checkbox. Judges can read it as a correctness audit.

### Steering docs

Three steering docs guided every Kiro response throughout the build:

- **`product.md`** — 13 non-negotiable principles written on day 1. The most consequential: "LLM proposes; validator decides; human authorizes" and "Core frozen at end of week 2." These prevented scope creep in both directions — Kiro never suggested adding a feature that required touching `core/` after the freeze.
- **`tech.md`** — stack, operation matrix, SSRF rules, redaction contract, type widening table. Having the widening pairs spelled out here meant the validator implementation required no judgment calls: the table was the code.
- **`kiro-features.md`** — documented which Kiro features to use and how, including the PBT strategy, the custom agent contract, and what the submission write-up must cover.

The biggest single impact: writing `product.md` before writing any code forced the product boundary to be explicit. When implementation revealed a tension (e.g., "should rollback generate an inverse SQL statement?"), the steering doc had already answered it ("NO — preserve the observed state; no inverse generated").

### Custom agent — Validator Reviewer

`.kiro/agents/validator-reviewer.md` defines an independent SQL safety auditor with a strict input contract:

```json
{
  "difference_kind": "TYPE_CHANGED | INDEX_MISSING | ...",
  "difference_target": "users.email",
  "proposed_sql": "ALTER TABLE ...",
  "old_type": "varchar(100)",
  "new_type": "varchar(255)"
}
```

The agent receives no resolver rationale, no confidence score, and no context that could bias its evaluation. It re-derives its verdict from the operation matrix independently — the same rules as the deterministic validator, applied by a different mechanism.

**Advisory only**: the deterministic validator in `core/validator.py` is authoritative. The agent's verdict is recorded alongside the validator's status. Disagreements are surfaced in the report and escalate to human review — the agent cannot approve what the matrix blocks, and can flag concerns the matrix cannot express.

This dual-review architecture catches two distinct failure classes: the validator catches structural violations (wrong SQL node type, wrong target, missing widening pair), and the agent catches semantic concerns the matrix cannot enumerate (e.g., "this widening is technically valid but the column size 32767 is suspiciously large for a typical identifier column").

### Agent hooks

Three hooks automate the most repetitive parts of the development cycle:

**`new-adapter`** (trigger: `FileCreate` on `adapters/**/comparator.py`)
When a new domain adapter comparator is created, Kiro automatically scaffolds the full application plumbing: `apps/<domain>/main.py`, `config.json`, `README.md`, `run.sh`, a renderer stub in the shared UI `RENDERERS` registry, and a comparator test file. The hook prompt explicitly prohibits modifying `core/` — if any step requires a core change, the agent stops and reports the conflict. This enforces the architecture rule at the tool level, not just in steering text.

**`comparator-test`** (trigger: `FileSave` on `adapters/**/comparator.py`)
Runs the matching comparator test suite on every save via `scripts/run_comparator_test.py`. The hook receives the event JSON on stdin and resolves the test file name from the domain folder name — `adapters/postgres/comparator.py` triggers `tests/test_comparator_pg.py`. Using a Python helper instead of a raw shell command avoids cross-platform quoting issues.

**`demo-case-cache`** (trigger: `FileSave` on `demo/cases.json`)
When the demo cases file changes, Kiro invokes the resolver against each new case and writes the resolution into `agents/cache.json` keyed by `Difference.signature` (not UUID). Each cache entry is required to include `cache_version`, `validator_contract`, and `prompt_hash` — the same provenance keys the cache lookup verifies before returning a hit.

### MCP — Context7

The resolver fetches library documentation from Context7 MCP before proposing a SQL fix. For a `TYPE_CHANGED` difference, the resolver looks up Postgres type widening semantics before generating the `ALTER COLUMN TYPE` statement — grounding the SQL in documented behavior rather than training data recall.

If Context7 is unreachable, the resolver proceeds without docs and forces `confidence: LOW`. MCP improves proposal quality; the validator enforces safety independently. The MCP server version is pinned in `.kiro/settings/mcp.json` — `@latest` is explicitly avoided.

### Property-based testing (PBT)

`tests/test_properties.py` uses Hypothesis to verify invariants that unit tests cannot enumerate:

- **Type normalizer** — round-trip property: every supported Postgres type name normalizes to a canonical string that is non-empty and stable across repeated calls
- **SQL validator** — positive generator: for every auto-fixable row in the operation matrix, generate a valid SQL statement targeting a matching identifier → must be accepted. Negative generator: mutate any part of the valid SQL → must be rejected. The generator produces SQL that would execute against real Postgres, ensuring the test population is realistic
- **State machine** — enumerate all 56 cells (7 states × 8 events), assert every transition matches the documented table. Invariant: no sequence can reach `PENDING` from `BLOCKED` or `REVIEW_REQUIRED`. Rollback never transitions state to `PENDING`. Persisted state always matches in-memory state
- **Redaction** — token boundary coverage: `authorization`, `Authorization`, `client_secret`, `clientSecret`, `CLIENTSECRET`, `private_key`, `privateKey`, `privatekey` all trigger redaction. `monkey`, `accessibility`, `author` do not

PBT caught one real defect: the initial state machine implementation allowed a `REVIEW_REQUIRED → PENDING` transition via a specific event sequence that unit tests (which only tested one event at a time) missed. The Hypothesis shrinking showed the minimal two-step sequence that triggered it.

### Kiro IDE

Built entirely in Kiro IDE. The spec editor was used throughout — viewing requirements.md and design.md side-by-side with implementation files kept the connection between requirement numbers and code explicit. Kiro's ability to reference spec requirement IDs in task comments (`_Req 17_`, `_Req 20_`) meant every task had a traceable link back to the original requirement without duplicating text.

---

## Architecture — the skeleton

```
core/                    ← 7 files, frozen after week 2
├── interfaces.py        ← 6 protocols: InputSource, SourceAdapter, Comparator,
│                           Resolver, Executor, Verifier
├── models.py            ← GhostState (7), Difference, Resolution, SQLChange,
│                           FilePatch, ExecutionResult, VerificationResult,
│                           ApplyOutcome, RollbackEvent, Report
├── validator.py         ← SQLValidator (matrix + AST binding + widening table)
│                           FilePatchValidator (symlink + hash + key confinement)
├── cache.py             ← JSON-backed resolution cache; lookup verifies
│                           cache_version + validator_contract + prompt_hash
├── persistence.py       ← atomic report store (temp + fsync + rename)
└── orchestrator.py      ← state machine: run() / apply_one() / rollback()
```

The skeleton has no knowledge of Postgres, OpenAPI, or `pyproject.toml`. It operates on `State` objects (dicts of normalized facts) and `Difference` objects (kind + target + context). Adding a domain requires a comparator + adapter code; it never requires touching `core/`.

```
adapters/postgres/       ← Schema Drift domain
├── normalize.py         ← SQLAlchemy ↔ Postgres type canonicaliser
├── intended.py          ← reads SQLAlchemy Base.metadata via importlib
├── actual.py            ← queries information_schema (connection-level timeout)
├── comparator.py        ← diffs intended vs actual → 8 Difference kinds
├── executor.py          ← applies SQL (precheck → connect → execute → commit/rollback)
└── verifier.py          ← re-reads schema after apply

apps/schema-drift/       ← mandatory app 1 (~30 lines)
apps/contract-observer/  ← mandatory app 2 (~30 lines)
apps/shared/ui/          ← one HTML template + RENDERERS registry
```

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

## Project structure

```
touchstone/
├── cli.py                                # CLI: touchstone compare / touchstone report
├── pyproject.toml                        # pinned dependencies, editable install
├── core/                                 # frozen — 7 files
├── adapters/
│   ├── postgres/                         # schema-drift domain
│   ├── openapi/                          # contract-observer domain
│   └── deps/                             # dependency-auditor domain (optional)
├── agents/
│   ├── resolver.py                       # LLM resolver (MockResolver in week 1)
│   ├── prompts.py
│   └── cache.json                        # pre-recorded demo resolutions
├── apps/
│   ├── shared/ui/                        # one shared template + RENDERERS registry
│   ├── schema-drift/                     # mandatory
│   └── contract-observer/               # mandatory
├── demo/
│   ├── models.py                         # Neon demo schema (users / orders / products)
│   ├── seed.py                           # seeds live Neon DB with 3 drift cases
│   ├── cases.json                        # 3 drift cases: INDEX_MISSING + 2× TYPE_CHANGED
│   └── docker-compose.yml               # local Postgres 16 alternative
├── tests/
│   ├── test_skeleton.py                  # 7 files, no external imports (15 tests)
│   ├── test_sqlglot_ast_assumptions.py   # pinned AST shapes (25 tests)
│   ├── test_normalize.py                 # type normaliser triples (91 tests)
│   ├── test_validator_sql.py             # matrix + binding + widening (33 tests)
│   ├── test_validator_file.py            # symlink / hash / key / traversal (12 tests)
│   ├── test_state_transitions.py         # 56-cell state machine (66 tests)
│   ├── test_rollback.py                  # file rollback + DB refusal (10 tests)
│   ├── test_cache_reproducibility.py     # cache hit / miss / provenance (8 tests)
│   ├── test_orchestrator_mock.py         # end-to-end with fake infrastructure
│   ├── test_postgres_e2e.py              # real Postgres (requires DATABASE_URL)
│   ├── test_sql_execution.py             # real Postgres SQL execution
│   └── test_properties.py               # Hypothesis PBT
├── .kiro/
│   ├── specs/touchstone/                 # requirements.md, design.md, tasks.md
│   ├── steering/                         # product.md, tech.md, kiro-features.md
│   ├── hooks/                            # new-adapter, comparator-test, demo-case-cache
│   ├── agents/                           # validator-reviewer.md
│   └── settings/mcp.json                # Context7 MCP (pinned version)
└── .github/workflows/
    └── drift.yml                         # CI: compare on models.py / openapi.yaml change
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

### Run tests (offline — no DB needed)

```bash
python -m pytest tests/ -q
# 295 passed, 11 skipped
```

### Run against real Postgres

```bash
# Restore demo drift state
python demo/seed.py $DATABASE_URL

# Run Postgres tests
DATABASE_URL=<neon-url> python -m pytest tests/test_postgres_e2e.py tests/test_sql_execution.py -v
# 9 passed, 1 skipped
```

### CLI

```bash
touchstone compare --target ix_orders_user_id
touchstone compare --target users.email
touchstone report
```

---

## What's next

- **Freeze `core/`** — one commit, no further changes (requires 2.22 + 2.23 green ✅)
- **App 1 — Schema Drift** — FastAPI wrapper around the Postgres adapter; shared UI with live ghost table
- **App 2 — Contract Observer** — OpenAPI spec vs live response; SSRF-protected outbound HTTP
- **Real LLM resolver** — replace `MockResolver` with Bedrock-backed resolver; Context7 MCP already wired
- **App 3 — Dependency Auditor** — optional; `pyproject.toml` vs installed versions

---

## License

MIT
