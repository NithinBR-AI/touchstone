```markdown
# Kiro Features Steering — Touchstone

## The claim
One lean skeleton (`core/`, 6 files, ~1,300 lines) plus two mandatory
applications and a third (The Poltergeist) built on top. Adding a domain requires a comparator + adapter
code; the hook scaffolds plumbing. One shared UI template.

## 1. Agent Hooks (`.kiro/hooks/`)
Three hooks, all verified firing in Kiro IDE (updated 2026-11-18). What we
learned: the trigger names are `PostFileSave` and `PostFileCreate`
(`FileSave`/`FileCreate` never fire); on Windows the matcher must accept both
slashes (`adapters[\\/].+[\\/]comparator\.py$`); command hooks receive
`{session_id, hook_event_name, cwd, file_path}` as JSON on stdin; hooks load
at session start and fire on edits made by the Kiro agent, not on manual saves.

- **new-adapter** (file created in `adapters/**/comparator.py`) — scaffolds
  app folder, config, test stub, README, renderer stub. Does NOT modify
  `core/`. Does NOT implement domain adapters.
- **comparator-test** (file edited in `adapters/**/comparator.py`) — runs
  matching test via a Python helper script (avoids shell quoting; receives
  event JSON on stdin).
- **demo-case-cache** (file edited in `fixtures/cases.json`) — checks every
  demo case will produce a stable `Difference.signature` cache key. Read-only:
  the cache itself is written only after apply+verify (Req 15/33).

**Evidence:** `new-adapter` scaffolded `apps/test/` from a throwaway
`adapters/test/comparator.py` (removed afterwards); each `comparator-test` run
appends a line to `.kiro/hook-runs.log`, which is committed.

## 2. MCP — Context7
Resolver fetches library docs before proposing a fix. **Package version is
pinned** in `.kiro/settings/mcp.json` (tested version, not `@latest`).

If docs don't cover the proposed syntax → `confidence: LOW`. If MCP
unreachable → proceed without docs, force `LOW`. MCP improves quality;
validator enforces safety independently.

## 3. Spec-Driven Development
One spec, three adapter requirement groups, one app structure.

## 4. Steering Docs
- `product.md` — what the product is, what it refuses to do
- `tech.md` — stack, conventions, operation matrix, SSRF, redaction
- `kiro-features.md` — this file

## 5. Custom Agent — Validator Reviewer
`.kiro/agents/validator-reviewer.md` — YAML frontmatter with `name` and
`description`; body contains role, process, input contract, constraints.
A development-time reviewer invoked from Kiro chat; no app calls it at
runtime and its verdict is not stored in Touchstone reports. **Advisory
only** — the deterministic validator is authoritative; a disagreement shows
up in Kiro chat and never overrides it.

Input contract:
```json
{
  "difference_kind": "...",
  "difference_target": "...",
  "proposed_sql": "...",
  "old_type": "..." | null,
  "new_type": "..." | null
}
```

6. Kiro Powers

One or two powers used during development (testing; security/SQL). Record
which powers were used in the submission write-up.

7. Property-Based Testing

tests/test_properties.py using hypothesis verifies:

· Type normalizer: reference table round-trip, unsupported types raise,
  invariant: canonical string always non-empty for supported types
· SQL validator: positive (valid SQL for every auto-fixable row with
  matching target → accepted) + negative (any mutation → rejected).
  Generator produces SQL that executes against real Postgres
· State machine: enumerate 7 states × 8 events = 56 cells, assert every
  cell matches the documented transition table; invariant: no sequence
  transitions BLOCKED→PENDING/RESOLVED or REVIEW_REQUIRED→PENDING; rollback
  never changes state to PENDING; persisted state matches in-memory state
· Redaction: token variants (authorization/Authorization/client_secret/
  clientSecret/CLIENTSECRET/private_key/privateKey/privatekey) all match;
  monkey/accessibility/author do NOT match
· SQL generator must produce SQL that executes against real Postgres for
  every positive test case

8. What a reviewer should verify

```
pytest tests/test_skeleton.py tests/test_fourth_domain.py   # core/ is domain-free; a 4th domain needs no core change
git log --oneline core/          # history, incl. the dropped week-2 freeze (87cbacb)
ls apps/                         # 3 app folders + shared/
cat .kiro/steering/product.md    # rules written first
```

9. Submission write-up must answer

· Kiro IDE vs CLI vs Web vs Crew — which used, how they differed
· Custom agents — the Validator Reviewer, its input contract, its impact
· Agent hooks — three hooks, what they automated, whether verified
· Spec-driven development — how the spec structured
· Steering docs — strategy that made the biggest difference
· MCP — Context7 role
· Property-based testing — what it caught that unit tests missed
· Kiro powers — which leveraged

```

---
