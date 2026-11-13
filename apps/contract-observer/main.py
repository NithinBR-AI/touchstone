"""
Contract Observer app — wires the OpenAPI adapter to the core orchestrator
and exposes the required endpoints.

Target repos are configured in repos.json at the repo root.
The active repo's openapi_url and api_base_url are used for intended/actual.
State: module-level orchestrator singleton, one per process.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

HERE = Path(__file__).parent
REPO = HERE.parent.parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
REPOS = json.loads((REPO / "repos.json").read_text())

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# active repo — first entry marked default, or first entry overall
_active_repo: dict = next((r for r in REPOS if r.get("default")), REPOS[0])

# ----- adapter wiring ------------------------------------------------
from adapters.openapi.actual import probe as probe_actual
from adapters.openapi.comparator import ContractComparator
from adapters.openapi.executor import ContractExecutor
from adapters.openapi.intended import load_from_url as load_intended
from adapters.openapi.validator import ContractValidator
from adapters.openapi.verifier import ContractVerifier
from agents.resolver import MantleResolver
from core.cache import Cache
from core.models import ApplyOutcome, ComparisonTarget, GhostState
from core.orchestrator import Orchestrator
from core.persistence import Persistence

_reports_dir = HERE / ".reports"
_cache_path = HERE / ".cache.json"


def _openapi_url() -> str:
    url = _active_repo.get("openapi_url", "")
    if not url:
        raise HTTPException(503, detail="No openapi_url configured for active repo")
    return url


def _api_base_url() -> str:
    url = _active_repo.get("api_base_url", "")
    if not url:
        raise HTTPException(503, detail="No api_base_url configured for active repo")
    return url


class _InputSource:
    def targets(self):
        return [
            ComparisonTarget(
                "api",
                _active_repo["id"],
                context={
                    "openapi_url": _active_repo.get("openapi_url", ""),
                    "repo": _active_repo.get("github", ""),
                },
            )
        ]


class _SourceAdapter:
    def intended(self, target):
        return load_intended(_openapi_url())

    def actual(self, target):
        intended_state = load_intended(_openapi_url())
        return probe_actual(_api_base_url(), intended_state.data.get("paths", {}))


def _build_orch(repo: dict) -> Orchestrator:
    executor = ContractExecutor(repo=repo.get("github", ""), token=GITHUB_TOKEN)
    return Orchestrator(
        input_source=_InputSource(),
        source_adapter=_SourceAdapter(),
        comparator=ContractComparator(),
        resolver=MantleResolver(),
        executor=executor,
        verifier=ContractVerifier(),
        validator=ContractValidator(),
        persistence=Persistence(_reports_dir),
        cache=Cache(_cache_path),
        domain="api",
        principal="demo-sandbox",
    )


orch = _build_orch(_active_repo)

_latest_report_id: str | None = None

# ----- app -----------------------------------------------------------
app = FastAPI(title="Contract Observer", version="1.0.0")


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
    return {
        "status": "ok",
        "domain": CONFIG["domain"],
        "github_token_set": bool(GITHUB_TOKEN),
        "active_repo": _active_repo["id"],
    }


@app.get("/config")
def get_config():
    return {
        **CONFIG,
        "latest_report_id": _latest_report_id,
        "active_repo": _active_repo["id"],
        "repos": [{"id": r["id"], "name": r["name"], "github": r.get("github", "")} for r in REPOS],
    }


@app.get("/repos")
def list_repos():
    return {"repos": REPOS, "active": _active_repo["id"]}


@app.post("/repos/select")
def select_repo(req: SelectRepoRequest):
    global _active_repo, _latest_report_id, orch
    repo = next((r for r in REPOS if r["id"] == req.repo_id), None)
    if repo is None:
        raise HTTPException(404, detail=f"repo '{req.repo_id}' not found")
    _active_repo = repo
    _latest_report_id = None
    orch = _build_orch(repo)
    return {"active": _active_repo["id"]}


@app.get("/discover")
def discover():
    return {
        "targets": [
            {
                "domain": "api",
                "target": _active_repo["id"],
                "openapi_url": _active_repo.get("openapi_url", ""),
                "api_base_url": _active_repo.get("api_base_url", ""),
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
        raise HTTPException(503, detail=f"compare failed: {type(exc).__name__}") from exc
    _latest_report_id = report.id
    return _serialise_report(report)


@app.post("/apply")
def apply(req: ApplyRequest):
    """
    Batch apply: when user clicks Exorcise on any diff, we batch ALL
    actionable diffs of the same kind into one PR — no per-field conflicts.
    """
    report = orch.report
    if report is None:
        raise HTTPException(503, detail="No report — run /compare first")

    trigger_diff = next((d for d in report.differences if d.id == req.difference_id), None)
    if trigger_diff is None:
        raise HTTPException(404, detail="difference not found")

    # Collect all non-terminal diffs of the same kind to batch into one PR
    same_kind_ids = [
        d.id
        for d in report.differences
        if d.kind == trigger_diff.kind
        and d.state not in (GhostState.RESOLVED, GhostState.SUPERSEDED, GhostState.EXECUTION_FAILED)
    ]

    # human_authorized=True: user clicked Exorcise after seeing the LLM proposal
    outcomes = orch.apply_batch(same_kind_ids, human_authorized=True)

    response_outcome = next((o for o in outcomes if o.difference_id == req.difference_id), None)
    if response_outcome is None:
        response_outcome = ApplyOutcome(difference_id=req.difference_id, state=trigger_diff.state)
    return _serialise_outcome(response_outcome)


@app.post("/demo/reset")
def demo_reset():
    """Close all open touchstone/* PRs on GitHub and reset the report."""
    global _latest_report_id
    closed = orch.executor.close_touchstone_prs()
    _latest_report_id = None
    return {"reset": True, "prs_closed": closed}


@app.post("/rollback")
def rollback(req: RollbackRequest):
    try:
        event = orch.rollback(req.difference_id)
    except KeyError:
        raise HTTPException(404, detail="difference not found")
    return _serialise_outcome(event)


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
