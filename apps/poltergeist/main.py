"""
Poltergeist app — wires the Redis cache adapter to the core orchestrator
and exposes the required endpoints.

Target repos are configured in repos.json at the repo root.
Intended state: cache_config.yaml fetched from GitHub raw URL.
Actual state:   /cache/status endpoint on the live target app.
State: module-level orchestrator singleton, one per process.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from adapters.redis.actual import _CLIENT
from adapters.redis.actual import load_from_url as load_actual
from adapters.redis.comparator import CacheComparator
from adapters.redis.executor import CacheExecutor
from adapters.redis.intended import load_from_url as load_intended
from adapters.redis.verifier import CacheVerifier
from agents.resolver import MockResolver
from core.cache import Cache
from core.models import ApplyOutcome, ComparisonTarget, GhostState
from core.orchestrator import Orchestrator
from core.persistence import Persistence
from core.validator import FilePatchValidator, SQLValidator

load_dotenv()

HERE = Path(__file__).parent
REPO = HERE.parent.parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
REPOS = json.loads((REPO / "repos.json").read_text())

_active_repo: dict = next((r for r in REPOS if r.get("default")), REPOS[0])

_reports_dir = HERE / ".reports"
_cache_path = HERE / ".cache.json"


def _cache_config_url() -> str:
    url = _active_repo.get("cache_config_url", "")
    if not url:
        raise HTTPException(
            503, detail="No cache_config_url configured for active repo"
        )
    return url


def _cache_status_url() -> str:
    url = _active_repo.get("cache_status_url", "")
    if not url:
        raise HTTPException(
            503, detail="No cache_status_url configured for active repo"
        )
    return url


def _cache_warm_url() -> str:
    url = _active_repo.get("cache_warm_url", "")
    if not url:
        raise HTTPException(503, detail="No cache_warm_url configured for active repo")
    return url


class _InputSource:
    def targets(self):
        return [ComparisonTarget("cache", _active_repo["id"])]


class _SourceAdapter:
    def intended(self, target):
        return load_intended(_cache_config_url())

    def actual(self, target):
        return load_actual(_cache_status_url())


_executor = CacheExecutor(cache_warm_url=_active_repo.get("cache_warm_url", ""))
_verifier = CacheVerifier(cache_status_url=_active_repo.get("cache_status_url", ""))

orch = Orchestrator(
    input_source=_InputSource(),
    source_adapter=_SourceAdapter(),
    comparator=CacheComparator(),
    resolver=MockResolver(),
    executor=_executor,
    verifier=_verifier,
    validator_sql=SQLValidator(),
    validator_file=FilePatchValidator(REPO),
    persistence=Persistence(_reports_dir),
    cache=Cache(_cache_path),
    domain="cache",
    principal="demo-sandbox",
)

_latest_report_id: str | None = None

# ----- app -----------------------------------------------------------
app = FastAPI(title="The Poltergeist", version="1.0.0")


# ----- request bodies ------------------------------------------------
class ApplyRequest(BaseModel):
    difference_id: str


class RollbackRequest(BaseModel):
    difference_id: str


class SelectRepoRequest(BaseModel):
    repo_id: str


# ----- endpoints -----------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "domain": CONFIG["domain"]}


@app.get("/config")
def get_config():
    return {
        **CONFIG,
        "latest_report_id": _latest_report_id,
        "active_repo": _active_repo["id"],
        "repos": [
            {"id": r["id"], "name": r["name"], "github": r.get("github", "")}
            for r in REPOS
        ],
    }


@app.get("/repos")
def list_repos():
    return {"repos": REPOS, "active": _active_repo["id"]}


@app.post("/repos/select")
def select_repo(req: SelectRepoRequest):
    global _active_repo, _latest_report_id, _executor, _verifier
    repo = next((r for r in REPOS if r["id"] == req.repo_id), None)
    if repo is None:
        raise HTTPException(404, detail=f"repo '{req.repo_id}' not found")
    _active_repo = repo
    _latest_report_id = None
    _executor = CacheExecutor(cache_warm_url=repo.get("cache_warm_url", ""))
    _verifier = CacheVerifier(cache_status_url=repo.get("cache_status_url", ""))
    return {"active": _active_repo["id"]}


@app.get("/discover")
def discover():
    return {
        "targets": [
            {
                "domain": "cache",
                "target": _active_repo["id"],
                "cache_config_url": _active_repo.get("cache_config_url", ""),
                "cache_status_url": _active_repo.get("cache_status_url", ""),
                "repo": _active_repo.get("github", ""),
            }
        ]
    }


@app.post("/compare")
def compare():
    global _latest_report_id
    try:
        report = orch.run()
    except Exception as exc:
        raise HTTPException(503, detail=f"compare failed: {exc}") from exc

    # Cache domain bypasses the SQL resolver — reset safe_fix diffs that the
    # orchestrator incorrectly flipped to BLOCKED (MockResolver returns no SQL
    # for cache kinds; core sees proposed_change=None and blocks them).
    for diff in report.differences:
        if diff.context.get("safe_fix") and diff.state == GhostState.BLOCKED:
            diff.state = GhostState.PENDING

    _latest_report_id = report.id
    return _serialise_report(report)


@app.post("/apply")
def apply(req: ApplyRequest):
    report = orch._report
    if report is None:
        raise HTTPException(503, detail="No report — run /compare first")
    diff = next((d for d in report.differences if d.id == req.difference_id), None)
    if diff is None:
        raise HTTPException(404, detail="difference not found")

    try:
        execution = _executor.execute(diff)
    except Exception as exc:
        diff.state = GhostState.EXECUTION_FAILED
        orch.persistence.save(report)
        return {
            "difference_id": req.difference_id,
            "state": "EXECUTION_FAILED",
            "error": str(exc),
        }

    if execution.success:
        diff.state = GhostState.RESOLVED
        verification = _verifier.verify(diff, execution)
        if not verification.resolved:
            diff.state = GhostState.STILL_PRESENT
    else:
        diff.state = GhostState.EXECUTION_FAILED
        verification = None

    outcome = ApplyOutcome(
        difference_id=req.difference_id,
        state=diff.state,
        execution=execution,
        verification=verification,
    )
    report.outcomes[req.difference_id] = outcome
    orch.persistence.save(report)
    return _serialise_outcome(outcome)


@app.post("/rollback")
def rollback(req: RollbackRequest):
    raise HTTPException(400, detail="Rollback not supported for cache domain")


@app.post("/demo/reset")
def demo_reset():
    """Restore drift state via /cache/warm on the target app."""
    global _latest_report_id
    try:
        resp = _CLIENT.post(_cache_warm_url(), timeout=30.0)
        resp.raise_for_status()
    except Exception as exc:
        raise HTTPException(500, detail=f"cache warm failed: {exc}") from exc
    _latest_report_id = None
    return {
        "status": "reset",
        "message": "Cache drift restored. Click Hunt Ghosts to scan again.",
    }


@app.get("/report/{report_id}")
def get_report(report_id: str):
    report = orch.persistence.load(report_id)
    if report is None:
        raise HTTPException(404, detail="report not found")
    return _serialise_report(report)


# ----- serialisation -------------------------------------------------
def _default(o: object) -> object:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if hasattr(o, "value"):
        return o.value
    if isinstance(o, datetime.datetime):
        return o.isoformat()
    if isinstance(o, bytes):
        return o.hex()
    raise TypeError(type(o).__name__)


def _serialise_outcome(obj) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(obj), default=_default))


def _serialise_report(report) -> dict:
    return json.loads(json.dumps(dataclasses.asdict(report), default=_default))


# ----- static UI (must be last mount) --------------------------------
app.mount(
    "/",
    StaticFiles(directory=HERE.parent / "shared" / "ui", html=True),
    name="ui",
)
