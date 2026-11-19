# Touchstone

**Schema drift is invisible until your DB migration fails in production. API contract drift is invisible until a client breaks. Cache drift is invisible until you wonder why your keys aren't there.**

Touchstone makes all three visible — and fixable — before they cause incidents. It compares what your code declares against what actually runs, classifies every gap deterministically, proposes one validated fix per ghost (with an LLM where reasoning is needed), and — on human approval — applies it and re-verifies the result.

**Kiroween 2026 — Skeleton Crew category** · Built with Qwen3 and DeepSeek models via [AWS Bedrock Mantle](https://aws.amazon.com/bedrock/)

![Touchstone architecture — one skeleton (core/), three apps](docs/architecture.png)

*One skeleton, three apps: the `core/` pipeline is shared, and each app plugs in its own adapters. Interactive version: [`docs/architecture.html`](docs/architecture.html).*

**Try it live:** [The Crypt](https://touchstone-schema-drift.onrender.com) · [The Séance](https://touchstone-contract-observer.onrender.com) · [The Poltergeist](https://touchstone-poltergeist.onrender.com) — the first load can take about a minute while the free-tier services wake.

---

## What it does

Two states exist for every system: what the code declares, and what the system actually runs. The gaps are ghosts.

| Domain | Declared | Actual | Ghost example |
|---|---|---|---|
| Database | SQLAlchemy models | Live Postgres schema | Index declared in models, missing from live DB |
| API contracts | `openapi.yaml` | Live API response | Field returned by API, absent from spec |
| Cache | `cache_config.yaml` | Live Redis key list | Key declared in config, missing from live cache |

Touchstone detects each ghost, proposes a fix, validates it against a strict rule set, and — on human approval — applies it and verifies the result. Every step is audited. Every fix is proven before it closes.

---

## Skeleton Crew entry

The skeleton is `core/` — 6 files (5 modules + `__init__.py`). Three applications (`schema-drift`, `contract-observer`, and `poltergeist`) run on top of it. The apps contain no core code and wire their own adapters; `core/` was extended once during the build (batch apply and human authorization for Contract Observer), and `test_skeleton.py` keeps it domain-free. Two mandatory apps satisfy the Skeleton Crew entry.

**What the skeleton is:** `core/interfaces.py` defines seven base protocols (`InputSource`, `SourceAdapter`, `Comparator`, `Resolver`, `Validator`, `Executor`, `Verifier`) plus two optional capability protocols (`BatchExecutor`, `RollbackSupport`). The remaining four files implement state management, caching, persistence, and the orchestrator. Domain-specific validators and models live in `adapters/`, not in `core/`.

**What the apps are:** Each app wires domain adapters to the orchestrator via `main.py`, plus a `config.json` (domain, accent, title), a `README.md`, and a `run.sh`. All domain logic lives in `adapters/`; `core/` owns only the workflow and state machine.

**Extensibility proof — fourth domain, zero core changes:** `tests/test_fourth_domain.py` implements a complete in-memory `config` domain — its own `InputSource`, `SourceAdapter`, `Comparator`, `Resolver`, `Validator`, `Executor`, and `Verifier` — and runs the full lifecycle (detect → resolve → validate → execute → verify → persist) without importing from any existing adapter package and without modifying a single line in `core/`. If `core/` needed a change to support a new domain, the test would catch it.

**AST-enforced boundary:** `test_skeleton.py` uses Python's `ast` module — not raw text search, so renaming a variable cannot slip past it — to enforce four rules on every test run:
- Exact file set in `core/` (no domain files creeping in)
- No module in `core/` imports from `adapters/`, `agents/`, or `apps/`
- No `sqlglot` import anywhere in `core/` (domain library — forbidden)
- No Compare node in `core/` where `.domain` is compared to a string literal (no domain branching)

**Verification:**

```bash
python -m pytest tests/test_skeleton.py tests/test_fourth_domain.py tests/test_apps_structure.py -v
```

**Test suite:** 688 passed, 12 skipped offline (no credentials); 695 passed, 5 skipped with a Neon `DATABASE_URL`, or 699 passed, 1 skipped right after a re-seed (the end-to-end tests need fresh drift to act on).

---

## The apps

### App 1 — Schema Drift · *The Crypt* · port 8001

Compares SQLAlchemy model declarations against a live Postgres schema. Ghosts are columns, tables, indexes, and type mismatches that exist on one side but not the other.

**Demo drift cases (pre-seeded in Neon DB):**

| Ghost | Kind | Badge after Hunt Ghosts |
|---|---|---|
| `ix_orders_user_id` declared, missing from live DB | `INDEX_MISSING` · Missing Specter | 👻 HAUNTING (auto-fix) |
| `users.email` declared `VARCHAR(255)`, live `VARCHAR(50)` | `TYPE_CHANGED` (widening) · Type Wraith | 👻 HAUNTING (auto-fix) |
| `users.age` declared `INTEGER`, live `BIGINT` | `TYPE_CHANGED` (narrowing) · Type Wraith | 🕯 SÉANCE NEEDED |
| `ix_users_email` in live DB, not declared | `INDEX_EXTRA` · Phantom Index | 🕯 SÉANCE NEEDED |

Clicking **Exorcise** applies the fix directly to the Neon DB. **↺ Reset Demo** re-seeds the drift.

### App 2 — Contract Observer · *The Séance* · port 8002

Probes a live API and compares each response against the declared `openapi.yaml`. Ghosts are fields the live API returns that the spec doesn't declare (Shadow Fields), and fields the spec declares that the API no longer returns (Vanished Fields).

**Demo drift cases (live on Render):**

| Ghost | Kind | Badge | Exorcise opens |
|---|---|---|---|
| `account_status` returned by `/users`, not in spec | `FIELD_EXTRA` · Shadow Field | ⛓ BOUND | PR on `openapi.yaml` |
| `sku` returned by `/products`, not in spec | `FIELD_EXTRA` · Shadow Field | ⛓ BOUND | PR on `openapi.yaml` |
| `loyalty_tier` declared in spec, not returned by `/users` | `FIELD_MISSING` · Vanished Field | 🕯 SÉANCE NEEDED | PR on `main.py` |

The live API runs at **[touchstone-demo-target-repo.vercel.app](https://touchstone-demo-target-repo.vercel.app/health)** as a Vercel serverless function, so it has no long cold start. The declared spec lives in **[NithinBR-AI/touchstone-demo-target-repo](https://github.com/NithinBR-AI/touchstone-demo-target-repo)**.

All drifts of the same kind are batched into **one PR per file**. Clicking Exorcise again returns the already-open PR (idempotent). **↺ Reset Demo** closes open Touchstone PRs so the next run produces a fresh one.

### App 3 — The Poltergeist · *Redis Cache Drift Detector* · port 8003

Compares the keys declared in `cache_config.yaml` against the keys that actually exist in the live Redis cache.

**Demo drift cases (live on Render + Upstash):**

| Ghost | Kind | Badge |
|---|---|---|
| `products:list` declared, absent from live cache | `KEY_MISSING` · Missing Cache Specter | 👻 HAUNTING (auto-fix) |
| `debug:session_dump` in live cache, never declared | `UNDECLARED_KEY` · Phantom Cache Key | ⛓ BOUND |

Exorcise on `KEY_MISSING` warms the key via `/cache/warm`; on `UNDECLARED_KEY` it deletes the key via `/cache/delete` (a human must authorize the deletion). The verifier confirms each action before marking RESOLVED. **↺ Reset Demo** restores both cases.

---

## Demo

Each app is deployed on its own:

| App | Live URL |
|---|---|
| Schema Drift · *The Crypt* | **[touchstone-schema-drift.onrender.com](https://touchstone-schema-drift.onrender.com)** |
| Contract Observer · *The Séance* | **[touchstone-contract-observer.onrender.com](https://touchstone-contract-observer.onrender.com)** |
| The Poltergeist | **[touchstone-poltergeist.onrender.com](https://touchstone-poltergeist.onrender.com)** |

> **First load can take about a minute.** The three apps run on Render's free tier and sleep when idle; the first visit wakes them, and after that scans are fast. The demo target API runs on Vercel and has no long cold start. To run locally instead, see [Setup](#setup).

Target repos are declared in **`repos.json`** at the repo root. Touchstone fetches declared sources from their GitHub raw URLs at runtime — no cloning. The header's **repo selector** shows Touchstone is built to monitor more than one repo.

**Schema Drift ([live](https://touchstone-schema-drift.onrender.com) · locally http://localhost:8001).** Click **↺ Reset Demo**, then **Hunt Ghosts**. Four cards appear. The Missing Specter and the `users.email` Type Wraith are 👻 HAUNTING — the validator has accepted their SQL. The `users.age` narrowing and the Phantom Index wait at 🕯 SÉANCE NEEDED. Click **⚡ Exorcise** on any card: the SQL runs on Neon, the verifier re-reads the schema, and the card moves to ✝ LAID TO REST.

**Contract Observer ([live](https://touchstone-contract-observer.onrender.com) · locally http://localhost:8002).** Click **Hunt Ghosts**. Shadow Fields and a Vanished Field appear. Click **⚡ Exorcise** on a Shadow Field: the LLM writes the spec patch, a second model checks it, and a pull request opens on the demo repo, with a **View PR ↗** link.

**The Poltergeist ([live](https://touchstone-poltergeist.onrender.com) · locally http://localhost:8003).** Click **Hunt Ghosts**. Exorcise the Missing Cache Specter to warm the key, then the Phantom Cache Key to delete it.

> **Replaying the demo:** after Reset Demo, Schema Drift and The Poltergeist replay every exorcised ghost from the cache with no LLM call (marked CACHED). Contract Observer's scan also repeats without an LLM call, but its Exorcise always generates a fresh patch with the LLM and opens a new GitHub PR, so it is never replayed from cache.

> **Security note:** Schema Drift fetches and executes `models.py` from the configured `models_url` (a GitHub raw URL). Anyone who can push to that branch can run arbitrary code on the Touchstone server. For production use, pin the URL to a specific commit SHA (`/blob/<sha>/models.py`) rather than a branch (`/blob/main/models.py`), and treat the target repo's write access as equivalent to server access.

---

## How it works

### Flow

```
1. Read targets    (repos.json → declared source URL + live environment URL)
2. Read both sides (SourceAdapter normalizes each side into a State)
3. Compare         (domain Comparator classifies each gap as a Difference)
4. Resolve         (Resolver returns a ChangeProposal per ghost)
                     · database → LLM (ultra) generates SQL, grounded in Context7 docs
                     · api      → field metadata only; the LLM runs at step 7
                     · cache    → deterministic CACHE_WARM / CACHE_DELETE; no LLM
5. Validate        (domain Validator enforces safety rules; rejects if violated)
6. Authorize       (human reviews the proposal in the UI → clicks Exorcise)
                     · human_authorized=True lets REVIEW_REQUIRED and BLOCKED through
7. Execute         (Executor applies the fix)
                     · database → SQLExecutor runs DDL against Postgres
                     · api      → ContractExecutor generates the patch with the LLM,
                                  checks it, opens a GitHub PR
                     · cache    → CacheExecutor calls /cache/warm or /cache/delete
8. Verify          (Verifier re-reads live state → RESOLVED / STILL_PRESENT / EXECUTION_FAILED)
```

**LLM proposes. Validator decides. Human authorizes. That order never changes.**

### Architecture

```
core/
├── interfaces.py    ← the 7 + 2 protocols (see Skeleton Crew entry)
├── models.py        ← GhostState (7 states), Difference, ChangeProposal,
│                       Resolution, ExecutionResult, VerificationResult,
│                       ApplyOutcome, RollbackEvent, Report
├── cache.py         ← JSON-backed resolution cache; written only after apply+verify succeeds;
│                       keyed by Difference.signature with cache_version, validator_contract,
│                       prompt_hash; 7-day TTL; null proposals never cached
├── persistence.py   ← atomic report store (temp + fsync + rename)
└── orchestrator.py  ← state machine: run() / apply_one() / apply_batch() / rollback()

adapters/postgres/    ← Schema Drift          apps/schema-drift/       ← mandatory app 1 — The Crypt
adapters/openapi/     ← Contract Observer     apps/contract-observer/  ← mandatory app 2 — The Séance
adapters/redis/       ← The Poltergeist       apps/poltergeist/        ← third app
                                              apps/shared/ui/          ← one template + RENDERERS registry
```

Each domain adapter implements the protocols, including its own validator (`adapters/postgres/validator.py` holds `SQLValidator` and the operation matrix). The skeleton calls the protocols; it never imports from adapters.

### State machine

Seven states × eight events = 56 cells. Every cell is defined in [`design.md`](.kiro/specs/touchstone/design.md) (state diagram and transition table); `tests/test_state_transitions.py` and `tests/test_properties.py` test them.

| State | Badge | Meaning |
|---|---|---|
| `PENDING` | 👻 HAUNTING | A validated fix is ready; Exorcise applies it |
| `REVIEW_REQUIRED` | 🕯 SÉANCE NEEDED | Not auto-fixable; a human must review and authorize |
| `BLOCKED` | ⛓ BOUND | Rejected by policy or the validator; only an explicit human Exorcise may proceed |
| `RESOLVED` | ✝ LAID TO REST | Applied and verified by re-reading the live system (database, cache); for API drift, the fix is an open PR awaiting merge |
| `STILL_PRESENT` | 😱 STILL HAUNTING | Applied, but the verifier found the ghost persists |
| `EXECUTION_FAILED` | 💀 RITUAL FAILED | The executor rejected the change or timed out |
| `SUPERSEDED` | 🌫 FADED | Reserved — defined in the model and UI, not yet produced by any code path |

---

## Ghost classification

The **Policy** column is how the comparator classifies a ghost. The card can end up in a different state once the validator checks the proposed fix: for example, an `INDEX_EXTRA` with a proposed `DROP INDEX` shows 🕯 SÉANCE NEEDED, because the human may still authorize it.

### Database (Schema Drift)

| Kind | Condition | Fix | Policy |
|---|---|---|---|
| `INDEX_MISSING` | non-unique index | `CREATE INDEX` | AUTO-FIX |
| `INDEX_MISSING` | unique index | — | REVIEW_REQUIRED |
| `TYPE_CHANGED` | widening pair | `ALTER COLUMN TYPE` | AUTO-FIX |
| `TYPE_CHANGED` | non-widening pair | — | REVIEW_REQUIRED |
| `COLUMN_MISSING` | nullable or has default | `ADD COLUMN` | REVIEW_REQUIRED |
| `COLUMN_MISSING` | NOT NULL + no default | — | BLOCKED |
| `TABLE_MISSING`, `COLUMN_EXTRA`, `TABLE_EXTRA`, `INDEX_EXTRA` | — | — | BLOCKED |

Widening pairs (auto-approved): `smallint→integer`, `integer→bigint`, `real→double precision`, `varchar(n)→varchar(m)` (m > n), `varchar(n)→text`. Everything else is non-widening and goes to REVIEW_REQUIRED.

### API contracts (Contract Observer)

| Kind | UI label | Policy |
|---|---|---|
| `FIELD_EXTRA` | Shadow Field | BLOCKED — PR-based fix to `openapi.yaml` |
| `FIELD_MISSING` | Vanished Field | REVIEW_REQUIRED — PR-based fix to `main.py` |
| `TYPE_MISMATCH` | — | REVIEW_REQUIRED — manual review |
| `PATH_MISSING` | Lost Endpoint | BLOCKED |
| `PATH_EXTRA` | Phantom Endpoint | BLOCKED |

API contract changes are never applied to a live system. The executor opens a GitHub PR.

### Cache keys (The Poltergeist)

| Kind | UI label | Policy |
|---|---|---|
| `KEY_MISSING` | Missing Cache Specter | AUTO-FIX — warms the key via `/cache/warm?key=<key>` |
| `UNDECLARED_KEY` | Phantom Cache Key | BLOCKED — deletes the key via `/cache/delete?key=<key>` once a human authorizes |

---

## Safety boundaries

### All domains
- **Validator is the safety boundary** — not the LLM, not keyword matching. The domain validator enforces the operation matrix structurally.
- **Redaction** — token-based, applied before persistence and before the resolver sees the data. Errors stored as code/class/summary — never raw exception text.
- **Atomic persistence** — report store uses temp file + fsync + rename. No partial writes on crash.
- **Outbound HTTP** — every request Touchstone makes (API probes, cache status and warmup, Context7, and the `models.py` download) goes through the shared SSRF guard in `adapters/ssrf.py`, with `trust_env=False` (no proxy env vars) and no automatic redirects.

### Schema Drift (database)
- **Operation matrix** — only recognized operation kinds are accepted; every other SQL node is blocked.
- **Target binding** — validator extracts table/column/index from the SQL AST and compares against `Difference.target`; a mismatch blocks regardless of resolver confidence.
- **Connection-level timeout** — 30s via `options="-c statement_timeout=30000"` at connect time; never via `SET`.
- **USING clause / expression index / partial index** — structurally rejected. `SET`, `DELETE` and `TRUNCATE` stay forbidden even with human authorization.
- **CREATE INDEX is exactly the declared index** — name must be `ix_<table>_<cols>`, columns in the declared order, plain B-tree (no `USING hash`/`gin`), default sort order (no `DESC` / `NULLS FIRST`). These rules come from tech.md; property-based testing showed the validator was not enforcing them (see [How Kiro was used](#property-based-testing-pbt)).

### Contract Observer (API)
- **Two-stage validation.** The proposal is metadata only: the patch is written at Exorcise, so Hunt Ghosts stays fast. **Stage 1, deterministic:** `ContractValidator` decides *which file* and *which fields* may change. The operation must be `FILE_PATCH`, the file must be exactly the one mapped to the ghost's kind (`openapi.yaml` for Shadow Fields, `main.py` for Vanished Fields), the target must match, and at least one field must be named; Vanished Fields also need a human click. The orchestrator runs this gate for every ghost in a batch (`apply_batch()` uses the same gates as `apply_one()`), so a ghost with no proposal or a rejected one never reaches the executor. **Stage 2, model check:** `ContractExecutor` writes the patch with `deepseek_pro()` and checks it with `ultra()` before opening a PR. If `ultra()` rejects it twice, a **draft** PR still opens for a human, but the card shows 💀 RITUAL FAILED: it never counts as a success. **Stage 3, human:** a person reviews and merges every PR. For The Séance, ✝ LAID TO REST means the fix is an open PR, not that the live API was re-read; the verifier records `pr_opened_not_merged`, and a re-scan after the merge confirms the ghost is gone.
- **SSRF protection** — before every request, every resolved address (including IPv4-mapped IPv6) is checked against the exact blocked-range list in tech.md, and anything multicast or not globally routable is refused. The same guard covers Context7 calls.
- **`follow_redirects=False`** — redirects are not auto-followed. Same-host redirects are followed manually (max 5 hops) with SSRF revalidation on each hop; cross-host redirects are skipped.
- **`trust_env=False`** — no proxy env vars.
- **No live mutations** — the executor opens a GitHub PR; it never patches the live API.
- **Unreachable is not missing (Req 10.6).** Each probe has a 10 s timeout. A path that times out, fails to connect, answers 5xx or redirects is recorded as not observed and never becomes a Lost Endpoint ghost; only a real 4xx answer does. If no declared endpoint answers, Hunt Ghosts reports "The target API is unreachable" and shows no ghosts.

---

## Models — AWS Bedrock Mantle

All LLM calls route through **[AWS Bedrock Mantle](https://aws.amazon.com/bedrock/)** — a single OpenAI-compatible endpoint (`https://bedrock-mantle.us-east-1.api.aws/v1`) that exposes Qwen3, DeepSeek, and NVIDIA models under one API key. The client is in `agents/mantle_client.py`, adapted from the same pattern used in [GhostVendor](https://github.com/NithinBR-AI/ghostvendor).

| App | Model | Why |
|---|---|---|
| App 1 — Schema Drift | **Qwen3-235B** (`qwen.qwen3-235b-a22b-2507`) via `ultra()` | SQL fix proposals need multi-step reasoning: trace the diff context, check the identifier is safe unquoted, produce exactly one DDL statement matching the target. |
| App 2 — Contract Observer | **DeepSeek v3.2** (`deepseek.v3.2`) via `deepseek_pro()` + **Qwen3-235B** via `ultra()` | At Exorcise, `deepseek_pro()` writes the patched file and `ultra()` checks it (see [Safety boundaries](#contract-observer-api)). No LLM runs at scan time. |
| App 3 — The Poltergeist | **No LLM** (`MockResolver`) | Warming or deleting a key is fully deterministic. |

**Fallback chain:** `ultra()` falls back to Nemotron Super (`nvidia.nemotron-super-3-120b`) on transient Mantle error. `deepseek_pro()` falls back to Qwen3-235B. Both are on the same Mantle endpoint — the fallback is a model name swap only.

**Temperature:** `0.0` on all calls, so the same ghost produces the same proposal and replays stay consistent.

**No key → no problem:** If `MANTLE_API_KEY` is not set, `MantleResolver` falls back to `MockResolver` and logs a warning. The apps start and scan normally; only the fix proposals are canned rather than LLM-generated.

---

## Key Design Decisions

**Cache written only after apply + verify.** The resolution cache (`core/cache.py`) is never written at resolve time — only after `apply_one()` succeeds and the verifier confirms the ghost is gone. Null proposals are never cached. Cached resolutions are proven fixes, not guesses, and they still pass through the validator on every replay.

**API fixes are PRs, never live mutations.** `ContractExecutor` calls the GitHub API, not a file writer. Nothing changes until a human merges the PR. The verifier does not re-read anything: it records that the PR is open and not merged (`pr_opened_not_merged`), and a re-scan after the merge confirms the fix.

**Batch by kind, one PR per file.** Three `FIELD_EXTRA` drifts become one PR on `openapi.yaml`, not three conflicting PRs on the same file. `FIELD_EXTRA` and `FIELD_MISSING` produce separate PRs because they patch different files. `apply_batch()` in the orchestrator runs the same per-ghost gates as `apply_one()` (proposal present, validator passes), then sends only the validated ghosts to `BatchExecutor.execute_batch()`.

**Human authorization unlocks REVIEW_REQUIRED and BLOCKED.** Exorcise always sends `human_authorized=True`: the human has seen the proposal and chosen to apply it. The validator still runs — it only stops gating on auto-fixability. EXECUTION_FAILED is the one state that cannot be retried.

**Draft PR on double validation failure.** If `ultra()` rejects the generated patch twice, `ContractExecutor` still opens a draft PR with a warning body, so a human can fix the patch before merging. The outcome is a failure (`PATCH_UNVALIDATED`, 💀 RITUAL FAILED), never a success.

---

## How Kiro was used

### Kiro client

The whole project was built in **Kiro IDE**: specs, steering, hooks, the custom agent and all code. Kiro CLI, Web and Crew were not used.

### Spec-driven development

The entire project was driven from a single spec written before any code existed:

- `.kiro/specs/touchstone/requirements.md` — numbered requirements across categories: core skeleton, database domain, API domain, cache domain
- `.kiro/specs/touchstone/design.md` — repository layout, state machine diagram and 56-cell transition table (7 states × 8 events), validator design per domain, SSRF and atomic-write designs, testing strategy
- `.kiro/steering/tech.md` — the SQL operation matrix and type-widening table the validator enforces
- `.kiro/specs/touchstone/tasks.md` — phased implementation plan; each completed task carries a `VERIFIED:` note with test counts

The plan had a freeze gate at task 3.2, and `core/` was frozen at commit `87cbacb`. It was reopened later when Contract Observer needed batch apply and human authorization (`apply_batch`, `human_authorized`) and the SQL validator moved out of `core/` into `adapters/postgres/`. The freeze claim was dropped (task 10.4). The Skeleton Crew proof now rests on `tests/test_skeleton.py`, which checks the `core/` file set and import rules on every test run.

The difference from vibe coding was structural: the spec forced resolution of ambiguous cases before implementation began. For example, "what happens when a narrowing type change is proposed?" was answered in requirements before the resolver or validator were written: `bigint → integer` is not in the widening table, so the ghost is `REVIEW_REQUIRED`. When the spec later added human authorization, it changed only who may apply that fix (a human clicking Exorcise). The validator still decides the state.

### Steering docs

Three steering docs guided every Kiro response throughout the build:

- **`product.md`** — 13 non-negotiable principles. The most consequential: "LLM proposes; validator decides; human authorizes" and its closing rule: "Ask: does this require changing core/? If yes, the answer is no. Write an adapter." These prevented scope creep in both directions.
- **`tech.md`** — stack, code conventions (including "no module outside `core/` may be imported by any module inside `core/`"), operation matrix, SSRF rules, redaction contract, type widening table. The widening pairs spelled out here meant the validator implementation required no judgment calls.
- **`kiro-features.md`** — documented which Kiro features to use and how, including the PBT strategy, the custom agent contract, and what the submission write-up must cover.

The biggest single impact: writing `product.md` before writing any code forced the product boundary to be explicit. When implementation revealed a tension (e.g., "should rollback generate an inverse SQL statement?"), the steering doc had already answered it ("NO — preserve the observed state; no inverse generated").

### Custom agent — Validator Reviewer

`.kiro/agents/validator-reviewer.md` defines an independent safety auditor with a strict input contract. The agent receives no resolver rationale, no confidence score, and no context that could bias its evaluation. It re-derives its verdict from the operation matrix independently.

**Advisory only**: the deterministic validator in `adapters/postgres/validator.py` is authoritative. The agent is a development-time reviewer invoked from Kiro chat; no app calls it at runtime. It must not approve anything the matrix blocks, and it can flag concerns the matrix cannot express (e.g., a widening that is technically valid but suspiciously large for a typical identifier column).

It stays strict on purpose. When a human clicks Exorcise, the deterministic validator accepts DROP and non-widening changes it would otherwise refuse. The reviewer still rejects them, so reviewing a proposed fix in Kiro chat gives a second opinion that does not loosen with authorization.

**Verified** in Kiro IDE with `{"difference_kind": "TABLE_EXTRA", "difference_target": "users", "proposed_sql": "DROP TABLE users"}`. It returned `"verdict": "reject"`, citing a blocked kind, a destructive operation and no database rollback.

### Agent hooks

Three hooks in `.kiro/hooks/`. File hooks fire when the Kiro agent creates or saves a file, not on manual editor saves. All three were **verified** in Kiro IDE by having the agent make each triggering edit.

**`new-adapter`** (trigger: `PostFileCreate` on `adapters/**/comparator.py`)
Scaffolds the full application plumbing when a new domain comparator is created: `apps/<domain>/main.py`, `config.json`, `README.md`, `run.sh`, a renderer stub in the shared UI `RENDERERS` registry, and a comparator test file. The hook prompt explicitly prohibits modifying `core/`. Verified by creating `adapters/test/comparator.py`: the agent scaffolded `apps/test/`, a test file, a renderer stub and a README section, and left `core/` untouched. The scaffold was then removed.

**`comparator-test`** (trigger: `PostFileSave` on `adapters/**/comparator.py`)
Runs the matching comparator test suite when the agent saves a comparator. Maps the domain folder to its test file: `postgres` → `tests/test_comparator_pg.py`, `openapi` → `tests/test_comparator_api.py`, `redis` → `tests/test_comparator_cache.py`. Each run appends a line to `.kiro/hook-runs.log` (committed as evidence), e.g. `tests/test_comparator_pg.py exit=0 10 passed`, because Kiro does not show a save hook's command output in chat.

**`demo-case-cache`** (trigger: `PostFileSave` on `fixtures/cases.json`)
Checks every demo case will produce a stable `Difference.signature` cache key: required fields, a kind the comparator emits, the comparator's target format, the context fields the validator binds against, and an expected state consistent with the operation matrix. Read-only — cache entries are written only by `apply_one()` after verification succeeds. Verified by editing a case description; the agent reported PASS for case-01 to case-03 and wrote no files.

What verification taught us about the installed Kiro version:

- Trigger names are `PostFileSave` and `PostFileCreate`. The first drafts used `FileSave` and `FileCreate`, which never fired.
- `matcher` is a regex on the absolute file path. On Windows that path uses `\`, so the matchers accept both separators: `adapters[\\/].+[\\/]comparator\.py$`.
- Command hooks receive `{session_id, hook_event_name, cwd, file_path}` as JSON on stdin.
- Hooks load at session start. Edited hook files need a new session.

### MCP — Context7

Context7 is used in two places.

**During development.** `.kiro/settings/mcp.json` pins the Context7 MCP server at `@upstash/context7-mcp@4.6.0`. Kiro uses it to look up library documentation (sqlglot, psycopg, httpx) before generating code, so its answers rest on current API behaviour rather than training recall.

**At runtime, in Schema Drift.** Before the database resolver asks the LLM for a SQL fix, `agents/context7.py` fetches a PostgreSQL documentation excerpt for that ghost's kind (for example `ALTER COLUMN TYPE` for a Type Wraith) and appends it to the prompt, capped at 2,000 characters. It calls the same Context7 service over its REST API (`GET https://context7.com/api/v2/context`, library `/websites/postgresql`), so the app needs no Node or MCP client.

- **Docs improve proposals; the validator still decides.** The prompt tells the LLM to use the docs for syntax only. That matters: the docs Context7 returns for `ALTER COLUMN TYPE` show a `USING` clause and several columns changed in one statement, and the validator rejects both. If the LLM copied them anyway, the validator would block the proposal.
- **Graceful fallback.** If Context7 is unreachable or returns nothing, the resolver proceeds without docs and the card's rationale ends with `(confidence: LOW, no Context7 docs)`.
- **Same outbound-HTTP rules as the rest of the app:** SSRF check, `trust_env=False`, `follow_redirects=False`, a 5-second timeout. Results are cached per ghost kind for 6 hours; failures for 60 seconds.
- **No API key needed.** Context7 serves anonymous requests at a lower rate limit; `CONTEXT7_API_KEY` is used only if set.
- **Database only.** Contract Observer calls its LLM inside the executor, and The Poltergeist uses no LLM.

Observed effect: with docs in the prompt, the resolver's rationale for narrowing `users.age` from `bigint` to `integer` notes that the change relies on PostgreSQL's implicit cast and is safe only while values fit. That matches the postgresql.org text Context7 returned, and the earlier answer without docs did not mention it.

### Property-based testing (PBT)

`tests/test_properties.py` uses Hypothesis (Req 41). Each test states a rule and Hypothesis generates the inputs, usually 100 per property:

- **Type normalizer** — the 30+ reference triples, any letter case and outer whitespace, generated `varchar(n)` and `numeric(p,s)`, SQLAlchemy ↔ Postgres round-trip, unsupported and array types raise.
- **SQL validator** — generated valid SQL for every auto-fixable matrix row is accepted; every mutation of it (wrong table/column/name, extra statement, `USING`, expression/partial/unique index, other schema, `DELETE`/`TRUNCATE`/`SET`/`DROP`, wrong operation, reordered columns, unnamed, non-btree, `DESC`) is rejected, with and without human authorization. Narrowing type changes need human authorization. The generated SQL is also executed through `SQLExecutor` on real Postgres (Neon), on throwaway `pbt_*` tables that are created and dropped inside the test.
- **State machine** — all 56 cells (7 states × 8 events) checked against the transition table in `design.md`, with and without human authorization; random event sequences keep the invariants (nothing returns to PENDING, rollback never changes state, terminal states stay put, gated states need authorization), and the report reloaded from disk always matches memory.
- **Redaction** — the required key variants (`clientSecret`, `CLIENTSECRET`, `private_key`, …) are redacted and `monkey` / `accessibility` / `author` are not; generated camelCase, snake, kebab and header-style keys built around sensitive words are redacted, while look-alikes (`keyboard`, `tokenizer`, `secretary`) are not; a secret never survives at any nesting depth.
- **SSRF guard** — random addresses from every blocked range in tech.md, plain and IPv4-mapped, are refused; public addresses pass.

**What PBT caught that unit tests missed:**

1. **SSRF guard let blocked ranges through.** It relied on Python's `is_private`-style flags. Hypothesis found `100.64.0.0` (carrier-grade NAT, also as `::ffff:100.64.0.0`), then `224.0.0.0` (multicast), then `192.0.0.9` (Python treats it as global; tech.md blocks all of `192.0.0.0/24`). The 13 existing SSRF unit tests passed throughout because they only tried addresses someone thought of. Fix: the guard now checks tech.md's range list directly, plus multicast and non-global.
2. **The index validator accepted "almost right" SQL.** It compared columns sorted, so `ON t (b, a)` passed for a declared `(a, b)` index, and it accepted unnamed, `USING hash` and `DESC` indexes, all contrary to tech.md. A reversed index would have been created and then reported STILL_PRESENT. Fix: columns are compared in order, the `ix_` name is required, and only plain B-tree with default sort order is accepted. The LLM prompt now states the same rules.

Both fixes have plain regression tests in `test_ssrf.py` and `test_validator_sql.py`.

**Limitation:** the 56-cell table in `design.md` was written from the code, so it catches future changes but can't prove the rules themselves are right. The random-sequence test is the independent check: its rules (nothing returns to PENDING, rollback never changes state, saved report matches memory) come from the spec, not the code.

### Kiro powers

No Kiro powers were used. Context7 is configured as a plain MCP server, not as a power.

---

## Scope & Constraints

| Constraint | Detail |
|---|---|
| **No automatic database rollback** | Schema changes are not reverted automatically. The report keeps the originally observed state and the executed SQL for manual recovery; no inverse statement is generated. |
| **One user per app** | Each app process holds one orchestrator and one active report. Correct for a demo; concurrent users would share state. |
| **Contract Observer Exorcise is live** | Every Exorcise generates a fresh patch with the LLM and opens a real GitHub PR, so it is never replayed from cache. The scan itself is deterministic. |
| **`models.py` is executed** | Schema Drift imports the declared models from a GitHub raw URL. Write access to that repo is effectively code execution on the server (see the security note in [Demo](#demo)). |
| **Postgres `public` schema only** | The SQL validator rejects any other schema. Identifiers are compared case-insensitively. |
| **Context7 grounding is database-only** | API and cache fixes are not grounded in documentation. |
| **SUPERSEDED is reserved** | The state exists in the model and UI, but no code path produces it yet. Contract Observer's one-PR-per-file batching avoids the sibling-conflict case it was meant for. |

---

## What's Next

Touchstone works end-to-end on three domains: real Postgres, a live API on Vercel, and live Redis on Upstash, with every fix validated, authorized and verified. What's left is widening what it can see and taking it from a demo to a team tool.

### Stage 2 — Wider Drift Coverage

- **More schema ghosts** — nullability, default and constraint drift (the UI already has labels for Null Shade, Altered Omen and Broken Ward)
- **SUPERSEDED handling** — when one fix makes a sibling ghost obsolete, mark it FADED instead of leaving it to fail
- **Docs grounding for API patches** — fetch OpenAPI documentation through Context7 before `deepseek_pro()` writes a spec patch
- **More databases** — MySQL and SQLite adapters behind the same protocols, with no change to `core/`

### Stage 3 — CI & Automation

- **Drift check on every PR** — a GitHub Action that runs a compare when `models.py`, migrations or `openapi.yaml` change, and posts the ghosts as a PR comment (never applies)
- **Scheduled scans** — nightly compare against each repo in `repos.json`, with a report link when new ghosts appear
- **Replayable Contract Observer** — cache the generated patch alongside the file's SHA, so an unchanged spec replays without a new LLM call

### Stage 4 — Production

- **Multi-user sessions** — one orchestrator per session or report instead of one per process
- **Authorized apply** — bearer-token auth on `/apply`, `/rollback` and `/report`, with the principal recorded in the audit log
- **Safer intended schemas** — pin `models_url` to commit SHAs, or load models in a sandboxed process
- **Persistent cache and reports** — a shared store instead of per-process JSON files, so redeploys keep proven fixes

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

# The Poltergeist (App 3) needs nothing here: it reaches Redis through the
# demo target API, which holds the Upstash credentials.

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

# Restore Schema Drift demo state (same as the Reset Demo button)
python fixtures/seed.py $DATABASE_URL
```

### Deploy to Render

Each app deploys on its own, from its own Dockerfile. Every image is the shared `core/` skeleton plus that app's adapter folder only — The Crypt's image contains no API-contract or cache code, and so on. Images are built from the repo root because the skeleton lives outside the app folders.

| App | Dockerfile | Copies | Secrets |
|---|---|---|---|
| Schema Drift | `apps/schema-drift/Dockerfile` | `core/` + `adapters/postgres/` + `fixtures/seed.py` | `DATABASE_URL`, `MANTLE_API_KEY` (optional `CONTEXT7_API_KEY`) |
| Contract Observer | `apps/contract-observer/Dockerfile` | `core/` + `adapters/openapi/` | `GITHUB_TOKEN`, `MANTLE_API_KEY` |
| The Poltergeist | `apps/poltergeist/Dockerfile` | `core/` + `adapters/redis/` | none |

All three also copy `agents/`, `adapters/ssrf.py`, the shared UI and `repos.json`.

**Blueprint:** in Render, choose *New → Blueprint* and select this repo. `render.yaml` creates three web services — one URL per app — and asks for the secrets. **Locally:**

```bash
docker build -f apps/schema-drift/Dockerfile -t touchstone-schema-drift .
docker run --rm -p 8001:8000 --env-file .env touchstone-schema-drift
```

On the free tier each service sleeps after 15 minutes without traffic and takes about a minute to wake; its fix cache and reports reset on every restart. The demo target API moved from Render to Vercel: a sleeping free Render service answered requests from the other Render apps with an immediate 502 and never woke, so The Séance and The Poltergeist could not reach it after a quiet spell. On Vercel it runs as a serverless function, and the apps' wake check (`/demo/wake`) normally succeeds on the first try. No keep-alive pinger is used: Render grants 750 free instance hours a month per workspace, and keeping the services awake around the clock would use them up and suspend every app.

### Running tests

```bash
# Tests load .env, so if DATABASE_URL is set there they run against Neon.
python -m pytest tests/ -q
# offline (no credentials):        688 passed, 12 skipped
# with Neon DATABASE_URL in .env:   695 passed, 5 skipped; 699 passed, 1 skipped right after a re-seed

# Postgres integration tests only
DATABASE_URL=<neon-url> python -m pytest tests/test_sql_execution.py tests/test_executor_pg.py -v
```

> **Warning:** with a real `DATABASE_URL`, `test_sql_execution.py` widens `users.email` on the demo database, which removes that ghost. Re-seed (or click Reset Demo) afterwards. The property tests only touch throwaway `pbt_*` tables and drop them.

### CLI

```bash
touchstone compare                          # every database ghost
touchstone compare --target users.email     # one target
touchstone report
```

`touchstone compare` is read-only: it reads the declared models and the database catalog, prints each gap with its state, and never writes. Fixes go through the web app, where they are validated, authorized and verified.

### Scan your own database

Point the CLI at any Postgres and any SQLAlchemy `models.py` with two environment variables:

```powershell
pip install -e .
$env:DATABASE_URL = "postgresql://user:pass@host/db?sslmode=require"
$env:MODELS_PATH  = "C:\path\to\your\models.py"   # overrides repos.json
touchstone compare
```

`MODELS_PATH` is executed as Python when it is read, so only use a models file you trust.

**Tried it:** [`examples/scan-your-own/models.py`](examples/scan-your-own/models.py) is the demo schema plus three changes the demo doesn't cover (a new column, a widened column, a new table). Scanned against the demo Neon database, all three came back with the state the operation matrix assigns: the widening as auto-fixable, the nullable column as review-only, the missing table as blocked. The real output is in [`examples/scan-your-own/sample-output.txt`](examples/scan-your-own/sample-output.txt).

---

## Project structure

```
touchstone/
├── core/                                 # 5 modules + __init__.py — no imports from outside core/
│   ├── interfaces.py
│   ├── models.py
│   ├── cache.py
│   ├── persistence.py
│   └── orchestrator.py
├── repos.json                            # target repos registry
├── adapters/
│   ├── remote_source.py                  # downloads models.py etc. (SSRF-checked, no redirects)
│   ├── ssrf.py                           # SSRF guard shared by every domain and Context7
│   ├── postgres/                         # schema-drift domain
│   │   ├── models.py                     # SQLChange, FilePatch (adapter-level models)
│   │   ├── validator.py                  # SQLValidator, operation matrix, safety rules
│   │   ├── normalize.py
│   │   ├── intended.py
│   │   ├── actual.py
│   │   ├── comparator.py
│   │   ├── executor.py
│   │   └── verifier.py
│   ├── openapi/                          # contract-observer domain
│   │   ├── validator.py                  # ContractValidator, payload + target checks
│   │   ├── intended.py
│   │   ├── actual.py
│   │   ├── comparator.py
│   │   ├── executor.py
│   │   └── verifier.py
│   └── redis/                            # poltergeist domain
│       ├── validator.py                  # CacheValidator, operation + target checks
│       ├── actual.py
│       ├── intended.py
│       ├── comparator.py
│       ├── executor.py
│       └── verifier.py
├── agents/
│   ├── resolver.py                       # MantleResolver (Bedrock/Mantle LLM) + MockResolver
│   ├── mantle_client.py                  # Mantle OpenAI-compatible client (ultra/deepseek_pro, temp=0)
│   ├── prompts.py                        # domain-aware system prompts (database/api/cache)
│   ├── context7.py                       # Context7 REST: PostgreSQL docs for the database prompt
│   └── redaction.py                      # token-based redaction + SafeError
├── apps/
│   ├── shared/ui/                        # one shared Halloween-themed template
│   │   ├── index.html
│   │   ├── app.js
│   │   └── style.css
│   ├── schema-drift/                     # mandatory app 1 — The Crypt
│   ├── contract-observer/                # mandatory app 2 — The Séance
│   └── poltergeist/                      # third app — The Poltergeist
│       # each app: main.py, config.json, README.md, run.sh, Dockerfile
├── fixtures/
│   ├── seed.py                           # seeds Neon DB with drift cases
│   ├── cases.json                        # drift cases for demo + cache seeding
│   └── docker-compose.yml                # local Postgres 16 alternative
├── examples/
│   └── scan-your-own/                    # models.py + real CLI output ("Scan your own database")
├── docs/
│   ├── architecture.html                 # architecture diagram (source)
│   └── architecture.png                  # architecture diagram (image, shown above)
├── scripts/
│   └── run_comparator_test.py            # helper for comparator-test hook
├── render.yaml                           # Render Blueprint: one web service per app
├── cli.py                                # CLI: touchstone compare / touchstone report
├── pyproject.toml
├── tests/
│   ├── test_skeleton.py                  # AST-enforced: exact core/ file set, no external imports, no sqlglot, no domain branching
│   ├── test_fourth_domain.py             # extensibility proof: full lifecycle with in-memory config domain, zero core changes
│   ├── test_apps_structure.py            # Skeleton Crew proof: 3 apps, 5 files each
│   ├── test_sqlglot_ast_assumptions.py   # pinned AST shapes
│   ├── test_normalize.py                 # type normaliser
│   ├── test_validator_sql.py             # operation matrix + target binding + widening
│   ├── test_state_transitions.py         # 56-cell state machine
│   ├── test_rollback.py                  # DB refusal + rollback event recording
│   ├── test_cache_reproducibility.py     # cache hit / miss / provenance
│   ├── test_demo_repeatability.py        # each app's demo replays identically (Req 39)
│   ├── test_orchestrator_mock.py         # end-to-end with fake infrastructure
│   ├── test_resolver.py                  # MockResolver
│   ├── test_mantle_resolver.py           # MantleResolver: routing, prompts, retry, fallback
│   ├── test_context7.py                  # Context7 docs grounding (faked HTTP)
│   ├── test_executor_api.py              # ContractExecutor batch, idempotency, demo reset
│   ├── test_ssrf.py                      # SSRF guard
│   ├── test_remote_source.py             # models.py download: SSRF, no redirects, no proxies
│   ├── test_properties.py                # property-based tests (Hypothesis, Req 41)
│   ├── test_comparator_api.py            # OpenAPI comparator
│   ├── test_redaction.py                 # token-based redaction
│   ├── test_comparator_cache.py          # Redis cache comparator
│   ├── test_comparator_pg.py             # Postgres comparator (in-memory schemas)
│   ├── test_executor_pg.py               # Postgres executor unit tests
│   ├── test_verifier_pg.py               # Postgres verifier unit tests
│   ├── test_postgres_e2e.py              # Postgres end-to-end (requires DATABASE_URL)
│   └── test_sql_execution.py             # real Postgres SQL execution (requires DATABASE_URL)
├── .kiro/
│   ├── specs/touchstone/                 # requirements.md, design.md, tasks.md
│   ├── steering/                         # product.md, tech.md, kiro-features.md
│   ├── hooks/                            # new-adapter, comparator-test, demo-case-cache
│   ├── agents/                           # validator-reviewer.md
│   └── settings/mcp.json                 # Context7 MCP (pinned version)
└── .github/workflows/                    # CI (planned — see What's Next)
```

---

## License

MIT
