```markdown
# Tasks — Touchstone

## 0. Day-1 setup (before week 1 task 1.1)

- [ ] 0.1 Verify Kiroween rules on Devpost; request 4,000 credits
- [ ] 0.2 Create repo, add MIT LICENSE, configure in About section
- [ ] 0.3 Add `.kiro/` — steering, specs, hooks, agents, settings
- [ ] 0.4 **Verify hook schema** by creating `adapters/test/comparator.py`
      and firing each hook in Kiro IDE. Confirm trigger names, matcher
      syntax, and event payload field names. Adjust hook JSON to match
      installed version.
- [ ] 0.5 **Verify custom agent** loads in Kiro: `.kiro/agents/validator-reviewer.md`
      with YAML frontmatter. Test with a sample SQL input.
- [ ] 0.6 Install and pin sqlglot. Record exact version in `pyproject.toml`.
- [ ] 0.7 Install and pin Context7 MCP version in `.kiro/settings/mcp.json`
      (replace `PIN_VERSION_HERE`).

## 1. Week-1 acceptance — green before week-2 freeze

- [ ] 1.0a State and API contracts — test_state_transitions.py (56 cells)
- [ ] 1.0b SQL validation — test_validator_sql.py + test_sql_execution.py +
      test_sqlglot_ast_assumptions.py + target binding + widening
- [ ] 1.0c File patch validation — test_validator_file.py
- [ ] 1.0d Rollback — test_rollback.py (file domain; DB unsupported; inode race)
- [ ] 1.0e Repeatability and caching — test_cache_reproducibility.py
- [ ] 1.0f Resolver in loop — mock resolver works end-to-end against Postgres
- [ ] 1.0g Scope and claims — audit submission copy

## 2. Foundation (Week 1 — core + Postgres end-to-end WITH mock resolver)

- [ ] 2.1 Project scaffold: pyproject.toml, package, tests/
  - _Req 1_
- [ ] 2.2 core/models.py
  - _Req 2_
- [ ] 2.3 core/interfaces.py
  - _Req 1_
- [ ] 2.4 core/validator.py — operation matrix, SQLValidator with target
      binding + widening, FilePatchValidator skeleton
  - _Req 17, 18_
- [ ] 2.5 core/persistence.py
  - _Req 19_
- [ ] 2.6 core/cache.py
  - _Req 15, 33_
- [ ] 2.7 core/orchestrator.py — run/apply_one/rollback
  - _Req 19, 24, 25_
- [ ] 2.8 test_sqlglot_ast_assumptions.py — verify AST before matcher relies
  - _Req 17_
- [ ] 2.9 adapters/postgres/normalize.py + test_normalize.py (30 triples)
  - _Req 6_
- [ ] 2.10 adapters/postgres/intended.py, actual.py, comparator.py
  - _Req 5, 7, 8_
- [ ] 2.11 adapters/postgres/executor.py — **connection-level timeout**,
      verifier.py
  - _Req 20, 23_
- [ ] 2.12 agents/resolver.py — **MOCK** returning canned Resolution
  - _Req 15_
- [ ] 2.13 test_validator_sql.py — all matrix rows, injection, target binding,
      widening/non-widening, SET rejected, USING/expression/partial rejected
  - _Req 17_
- [ ] 2.14 test_sql_execution.py — real Postgres
  - _Req 17_
- [ ] 2.15 test_state_transitions.py — 56 cells + invariants + persisted
  - _Req 19_
- [ ] 2.16 test_rollback.py — file success, external edit, double rollback,
      DB refusal, inode race
  - _Req 21, 25_
- [ ] 2.17 test_skeleton.py — 7 files, no external imports
  - _Req 1, 38_
- [ ] 2.18 demo/models.py, docker-compose.yml, seed.py, cases.json
  - _Req 37_
- [ ] 2.19 **Postgres path passes end-to-end with mock resolver** — one
      auto-fixable diff, mock proposes valid SQLChange, validator passes,
      executor applies, verifier confirms

## 3. Freeze (Week 2)

- [ ] 3.1 Refine core as Postgres teaches what's missing
- [ ] 3.2 **FREEZE core/. One commit. No further changes.**

## 4. App 1 — Schema Drift (Week 2–3)

- [ ] 4.1 apps/shared/ui/ — one template with renderer registry
  - _Req 29_
- [ ] 4.2 apps/schema-drift/{main.py, config.json, README.md, run.sh}
  - _Req 28, 30_
- [ ] 4.3 test_apps_structure.py — 2 or 3 apps; each has 4 files; shared
      UI has 3
  - _Req 30_
- [ ] 4.4 Verify Schema Drift works via `./apps/schema-drift/run.sh`
  - _Req 28, 30_
- [ ] 4.5 Deploy mode decision: bearer-token OR isolated demo. Implement.
  - _Req 26_

## 5. App 2 — Contract Observer (Week 3)

- [ ] 5.1 adapters/openapi/ssrf.py — pin every hop, IPv4-mapped, redirects,
      proxy disable
  - _Req 10_
- [ ] 5.2 adapters/openapi/intended.py, actual.py
  - _Req 9, 10_
- [ ] 5.3 agents/redaction.py — token-based, SafeError
  - _Req 11_
- [ ] 5.4 adapters/openapi/comparator.py — safe_fix
  - _Req 11_
- [ ] 5.5 adapters/openapi/executor.py — atomic write via directory-fd,
      verifier.py
  - _Req 21, 23_
- [ ] 5.6 test_ssrf.py — all cases
  - _Req 42_
- [ ] 5.7 test_redaction.py — all cases
  - _Req 43_
- [ ] 5.8 test_comparator_api.py
  - _Req 11_
- [ ] 5.9 apps/contract-observer/{main.py, config.json, README.md, run.sh}
  - _Req 28, 30_
- [ ] 5.10 Verify `git diff core/` still empty
  - _Req 1, 38_

## 6. App 3 — Dependency Auditor (Week 4 — OPTIONAL, cuttable)

- [ ] 6.1 adapters/deps/* — DECLARED_UNPINNED produces PENDING (not auto-apply)
  - _Req 12, 13, 14, 22, 34_
- [ ] 6.2 test_comparator_deps.py
  - _Req 14_
- [ ] 6.3 apps/dependency-auditor/{main.py, config.json, README.md, run.sh}
  - _Req 28, 30_
- [ ] 6.4 Verify `git diff core/` still empty
  - _Req 1, 38_
- [ ] 6.5 **CUT LINE: week 4 day 3 if behind — delete
      apps/dependency-auditor/ and adapters/deps/. Ship two apps.**

## 7. Real resolver + orchestration (Week 4)

- [ ] 7.1 Replace mock resolver with real Bedrock resolver + MCP
  - _Req 15, 16_
- [ ] 7.2 agents/cache.json — pre-recorded resolutions for all demo cases
  - _Req 15, 33_
- [ ] 7.3 inputs/repo_discovery.py, manual_form.py
  - _Req 3, 4_
- [ ] 7.4 Superseded handling
  - _Req 27_

## 8. Kiro integrations (Week 4)

- [ ] 8.1 Three hooks per Req 35 (already verified day-1 task 0.4)
  - _Req 35_
- [ ] 8.2 .kiro/settings/mcp.json — pinned version
  - _Req 16_
- [ ] 8.3 .kiro/agents/validator-reviewer.md — frontmatter + input contract
      + constraints (already verified day-1 task 0.5)
  - _Req 15_
- [ ] 8.4 README "Adding a fourth domain"
  - _Req 35_

## 9. PBT (Week 4)

- [ ] 9.1 test_properties.py — reference round-trip, SQL positive+negative
      (executes against real Postgres), state machine 56 cells, redaction
      variants
  - _Req 41_
- [ ] 9.2 Document PBT catches in submission write-up
  - _Req 41_

## 10. CI and proof (Week 5)

- [ ] 10.1 .github/workflows/drift.yml
  - _Req 36_
- [ ] 10.2 Manual run + screenshot PR comment
  - _Req 36, 38_
- [ ] 10.3 README "Skeleton proof"
  - _Req 38, 40_
- [ ] 10.4 Confirm one freeze commit
  - _Req 1, 38_

## 11. Deployment (Week 5)

- [ ] 11.1 docker-compose.yml — two mandatory apps in default profile; third
      behind profile
  - _Req 37_
- [ ] 11.2 Deploy two mandatory apps to URLs
  - _Req 37_
- [ ] 11.3 Apps accessible until Jan 29, 2027
  - _Req 37_

## 12. Repeatability (Week 5)

- [ ] 12.1 test_cache_reproducibility.py
  - _Req 33_
- [ ] 12.2 test_demo_repeatability.py
  - _Req 39_

## 13. Submission (Week 5)

- [ ] 13.1 2-min video (Act 1–5 per design.md)
  - _Req 38_
- [ ] 13.2 60-second vertical short
  - _Req 38_
- [ ] 13.3 Devpost write-up answering all Kiro questions (kiro-features.md §9)
  - _Req 38_
- [ ] 13.4 Blog post with Kiroween 2026 mention
  - _Req 38_
- [ ] 13.5 Social post starting with `#KiroweenHackathon26`
  - _Req 38_
- [ ] 13.6 Every MANDATORY requirement passes; optional listed in "What's next"
  - _Req 38_
```

---
