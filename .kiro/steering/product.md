```markdown
# Product Steering — Touchstone

## What this is
Touchstone compares an intended system state (from repo files) against an
actual system state (from live environments), classifies drift
deterministically, proposes one fix per difference via a single LLM agent,
validates each proposal against an operation matrix, and applies approved
fixes with evidence-backed re-verification.

Ghosts are the gaps — tables, fields, and cache keys that exist in one
state but not the other.

## The skeleton and its applications

**The skeleton** is `core/` — 6 files, ~1,300 lines. It implements a
10-step compare-verify workflow and knows nothing about any specific domain;
`tests/test_skeleton.py` enforces that on every test run.

**The applications** are built on top of the skeleton. Each lives in its own
folder under `apps/` and contains FastAPI endpoint handlers, serialisation
helpers, and demo reset logic (~260–310 lines each). No domain logic lives in apps —
all domain-specific behaviour is in `adapters/`.

| App | Domain | Accent | Primary verb | Mandatory? |
|---|---|---|---|---|
| Schema Drift | database | orange `#f97316` | Apply | **Yes** |
| Contract Observer | api | icy cyan `#67e8f9` | Exorcise | **Yes** |
| The Poltergeist | cache | violet `#c084fc` | Exorcise | **Shipped** (third app) |

Three apps shipped. Two mandatory apps satisfy Skeleton Crew. Dependency Auditor was cut.

## The pitch
Your code says what should exist. Your system says what does.
Touchstone finds the gap — and proves every fix.

## Non-negotiable principles

1. **Core stays domain-free** (updated 2026-11-17). The original plan froze
   `core/` at the end of week 2 (commit `87cbacb`). That freeze was dropped
   when Contract Observer needed batch apply and human authorization
   (task 10.4). The rule that replaced it: `core/` may change only in
   domain-agnostic ways, enforced by `tests/test_skeleton.py` (exact file set;
   no imports from adapters, agents or apps; no sqlglot; no branching on a
   domain string). Adding a domain must not need a core change, proven by
   `tests/test_fourth_domain.py` and by The Poltergeist.

2. **LLM proposes; validator decides; human authorizes.** The validator
   independently enforces safety via a structural operation matrix.

3. **Validator is the safety boundary.** MCP improves quality. The validator
   enforces safety — SQL allowlist, canonical AST scope, target binding,
   path confinement, hash locks, SSRF validation.

4. **Every apply is verified with evidence.** VerificationResult carries the
   exact state that was re-read. A boolean is not sufficient. Exception, stated
   plainly: API fixes are pull requests, so the API verifier records that the
   PR is open and not merged (`pr_opened_not_merged`) instead of claiming a
   re-read; a re-scan after the merge confirms the fix.

5. **Rollback is an event, not a state transition.** File-domain rollback
   appends RollbackEvent. Database rollback is not supported — the report
   preserves the originally observed state and the executed SQL for manual
   recovery. It does NOT preserve an "inverse statement" — no inverse is
   generated.

6. **Review-only resolutions have no proposed_change.** Ambiguous
   differences (missing fields/endpoints, renames, status changes,
   dependency mismatches, non-widening type changes, and any operation not
   in the auto-fixable allowlist) are REVIEW_REQUIRED with
   `proposed_change=None`.

7. **File patches are hash-locked, race-free, and atomically written.**
   Directory-fd-relative operations with O_NOFOLLOW, inode re-check before
   rename, temp-file + fsync + os.rename within the same directory fd.

8. **Destructive operations are structurally rejected.** Not by keyword.
   The operation matrix defines exactly which SQL operations are
   auto-fixable. Everything else fails closed.

9. **One shared UI template, per-app config.** Vanilla HTML/JS, Tailwind
   via CDN. Domain-specific evidence rendered via a registry inside the
   shared template. No React, no build step, no duplicated UI.

10. **Determinism is scoped:** same captured inputs → deterministic
    decisions. Live observations may vary; every run records its evidence.

11. **All outbound HTTP is SSRF-protected and every hop is pinned.**
    Loopback, private, link-local, reserved, and IPv4-mapped-IPv6 destinations
    are refused. Redirects are not followed (`follow_redirects=False`).
    Proxy env vars are ignored (`trust_env=False`).

12. **Redaction is token-based and complete.** Headers, query strings, URL
    userinfo, form bodies, JSON (depth ≤ 5, subtrees beyond discarded), and
    error evidence (stored as code/class/generated-summary — never raw
    exception text). Redaction happens before persistence, logging, and the
    resolver sees the data.

13. **Public deployment uses open apply mode for demo.** Apply is fully
    open — no bearer token. Principal recorded in audit log as
    `"demo-sandbox"`. For non-demo deployments: bearer-token auth on
    /apply, /rollback, /report/:id. Server enforces the gate, not the
    browser. CORS same-origin is not sufficient.

## What Touchstone does NOT do
- Install packages (declaration repair only; runtime unchanged)
- Prove contract correctness (observes one response per endpoint)
- Auto-fix non-widening type changes or ambiguous API diffs
- Roll back database changes automatically
- Fix every ghost

## Adding a fourth domain
Comparator + domain adapter code (intended.py, actual.py, executor.py,
verifier.py) + a renderer registered in the shared UI. The Kiro hook
scaffolds plumbing — CLI, config, test scaffold, README, renderer stub —
but does NOT implement the adapter itself.

## App phase decisions (updated 2026-11-11)

### Authorization mode — Apply is OPEN
Apply is fully open — no 403, no password. Judges can apply fixes directly in the UI.
Principal recorded in audit log as `"demo-sandbox"`. No bearer token.

**Schema Drift** (`/apply`): calls `orch.apply_one(id, human_authorized=True)` — standard orchestrator path. `human_authorized=True` allows REVIEW_REQUIRED diffs (e.g. narrowing type changes, DROP operations) to execute after human review.

**Contract Observer** (`/apply`): calls `orch.apply_batch(same_kind_ids, human_authorized=True)`. Batches all non-terminal diffs of the same kind into one PR (one PR for all FIELD_EXTRA, one for all FIELD_MISSING). `ContractExecutor.execute_batch()` handles grouping and idempotency internally. State managed entirely by orchestrator.

**The Poltergeist** (`/apply`): calls `orch.apply_one(id, human_authorized=True)`. `CacheExecutor` derives the action from the Difference directly (KEY_MISSING → POST /cache/warm, UNDECLARED_KEY → DELETE /cache/delete). No proposed_change needed.

### Demo-target repo — `touchstone-demo-target`
The declared schema lives in a separate repo:
[NithinBR-AI/touchstone-demo-target-repo](https://github.com/NithinBR-AI/touchstone-demo-target-repo)
containing `models.py`, `openapi.yaml`, `requirements.txt`. Touchstone fetches the declared
schema from the configured GitHub raw URL (via `adapters/remote_source.py`); no env var
or local file needed — repos are configured in `repos.json` at the project root.

### Demo reset — `POST /demo/reset`
After a judge applies fixes, they can click **↺ Reset Demo** to re-seed the Neon DB
with drift and start over. The endpoint calls `fixtures.seed.main(DB_URL)`, clears
`_latest_report_id`, and calls `invalidate_remote()`. A 4-second banner confirms the reset in the UI.

### Orchestrator lifecycle — module-level singleton
Each app process holds **one orchestrator instance** at module level (not
per-request). The instance is created at import time with the app's adapter
wiring. `orch.run()` replaces `orch._report` in-place; callers use
`orch.report` to read the latest. This means a single concurrent user is the
target — correct for demo.

### Page-refresh recovery — same tab only (updated 2026-11-16)
After every `POST /compare` the UI stores the report ID in `sessionStorage`
(scoped to that browser tab and app origin). On page load the UI fetches
`GET /report/{id}` for that stored ID, restoring the tab's own hunt without
re-running compare. A new visitor or a new tab always starts on the Hunt
Ghosts screen, so nobody lands on another person's session from the shared
orchestrator. Reset Demo and switching repo clear the stored ID; a 404
(server restarted) clears it too. `/config` still returns `latest_report_id`
but the UI no longer restores from it. No separate `/report/latest` route.

## Tone
Restrained and atmospheric. A serious instrument with a haunting narrative —
not a Halloween costume.

## When in doubt
Ask: does this require changing core/? If yes, the answer is no. Write an
adapter.
```
