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

Touchstone detects each ghost, proposes a fix, validates it, applies it on human approval, and verifies the result.

---

## Flow

1. Read targets
2. Read both sides, normalize into `State`
3. Compare → classify each `Difference`
4. Resolver proposes one fix per ghost
5. Validator applies structural + semantic gates
6. Human approves → Executor applies atomically
7. Verifier re-reads state → `RESOLVED`, `STILL_PRESENT`, or `EXECUTION_FAILED`

---

## Structure

```
core/           # Frozen after Postgres path passes end-to-end
adapters/       # postgres/, openapi/, deps/
agents/
apps/           # schema-drift, contract-observer, dependency-auditor
inputs/
demo/
tests/
scripts/
```
