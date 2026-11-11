```markdown
# Product Steering — Touchstone

## What this is
Touchstone compares an intended system state (from repo files) against an
actual system state (from live environments), classifies drift
deterministically, proposes one fix per difference via a single LLM agent,
validates each proposal against an operation matrix, and applies approved
fixes with evidence-backed re-verification.

Ghosts are the gaps — tables, fields, and dependencies that exist in one
state but not the other.

## The skeleton and its applications

**The skeleton** is `core/` — 7 files, ~600 lines, frozen at the end of
week 2. It implements a 10-step compare-verify workflow and knows nothing
about any specific domain.

**The applications** are built on top of the skeleton. Each lives in its own
folder under `apps/` and is a ~30-line wrapper that imports from `core/`.

| App | Domain | Accent | Primary verb | Mandatory? |
|---|---|---|---|---|
| Schema Drift | database | orange `#f97316` | Apply | **Yes** |
| Contract Observer | api | icy cyan `#67e8f9` | Exorcise | **Yes** |
| Dependency Auditor | deps | violet `#8b5cf6` | Apply | Optional (cut if behind) |

Two apps satisfy Skeleton Crew. The third is additional evidence. Cut line:
week 4, day 3.

## The pitch
Your code says what should exist. Your system says what does.
Touchstone finds the gap — and proves every fix.

## Non-negotiable principles

1. **Core frozen at end of week 2**, after the Schema Drift app passes
   end-to-end with a working (mock) resolver in the loop.

2. **LLM proposes; validator decides; human authorizes.** The validator
   independently enforces safety via a structural operation matrix.

3. **Validator is the safety boundary.** MCP improves quality. The validator
   enforces safety — SQL allowlist, canonical AST scope, target binding,
   path confinement, hash locks, SSRF validation.

4. **Every apply is verified with evidence.** VerificationResult carries the
   exact state that was re-read. A boolean is not sufficient.

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
   via CDN, three animations maximum. Domain-specific evidence rendered via
   a registry inside the shared template. No React, no build step, no
   duplicated UI.

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

13. **Public deployment requires authorization.** Either bearer-token auth
    on /apply, /rollback, /report/:id, OR a strictly isolated read-only
    demo. Server enforces the approval gate, not the browser. CORS
    same-origin is not sufficient.

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

**Schema Drift** (`/apply`): calls `orch.apply_one()` — standard orchestrator path.

**Contract Observer** (`/apply`): bypasses `orch.apply_one()` and calls
`ContractExecutor.execute(diff)` directly. Reason: all API diffs have `safe_fix=False`
(changes require a PR, never auto-applied), so `apply_one()` would refuse with
REVIEW_REQUIRED. The executor opens a GitHub PR instead of touching the live system.
State is updated to RESOLVED or EXECUTION_FAILED and saved via `orch.persistence`.

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

### Page-refresh recovery — `latest_report_id` in `/config`
`/config` response includes `latest_report_id: str | null`. After every
`POST /compare` the server updates this field. On page load the UI reads
`/config`, and if `latest_report_id` is set, fetches `GET /report/{id}` to
restore state without re-running compare. No separate `/report/latest` route
needed.

## Tone
Restrained and atmospheric. A serious instrument with a haunting narrative —
not a Halloween costume.

## When in doubt
Ask: does this require changing core/? If yes, the answer is no. Write an
adapter.
```
