```markdown
# Kiro Features Steering — Touchstone

## The claim
One lean skeleton (`core/`, ~600 lines) plus two mandatory applications (and
one optional) built on top. Adding a domain requires a comparator + adapter
code; the hook scaffolds plumbing. One shared UI template.

## 1. Agent Hooks (`.kiro/hooks/`)
Three hooks. **Verify exact schema (trigger names, matcher placement, action
payload field) against installed Kiro version on day 1 before relying on
them.** The v4.4.2 files represent the current documented format
(`trigger` string, `matcher` sibling, `PostFileSave` for saves) but were not
fired in the target Kiro version yet.

- **new-adapter** (file created in `adapters/**/comparator.py`) — scaffolds
  app folder, config, test stub, README, renderer stub. Does NOT modify
  `core/`. Does NOT implement domain adapters.
- **comparator-test** (file edited in `adapters/**/comparator.py`) — runs
  matching test via a Python helper script (avoids shell quoting; receives
  event JSON on stdin).
- **demo-case-cache** (file edited in `demo/cases.json`) — generates cache
  keyed by `Difference.signature`.

**Day-1 verification task:** create a throwaway `adapters/test/comparator.py`,
trigger each hook manually, confirm it fires and receives the expected
payload. Adjust trigger names/matcher syntax/payload field names to match.

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
Runs in parallel to the deterministic validator. **Advisory only** — the
deterministic validator's status is authoritative. Its verdict is recorded
in the report; disagreements are surfaced but never override.

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
git log --oneline core/          # freeze commit + no others
git diff core/                   # empty after all apps
ls apps/                         # 2–3 app folders + shared/
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
