"""
Schema Drift app — wires the Postgres adapter to the core orchestrator
and exposes the required endpoints plus /repos and /demo/reset.

Target repos are configured in repos.json at the repo root.
The active repo's models.py is fetched from its models_url (GitHub raw).
State: module-level orchestrator singleton, one per process.
"""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

HERE = Path(__file__).parent
REPO = HERE.parent.parent
CONFIG = json.loads((HERE / "config.json").read_text())
REPOS = json.loads((REPO / "repos.json").read_text())

DB_URL = os.environ.get("DATABASE_URL", "")

# active repo — first entry marked default, or first entry overall
_active_repo: dict = next((r for r in REPOS if r.get("default")), REPOS[0])

# ----- adapter wiring ------------------------------------------------
from adapters.postgres.actual import load_actual_schema
from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.executor import SQLExecutor
from adapters.postgres.intended import load_intended_schema
from adapters.postgres.validator import SQLValidator
from adapters.postgres.verifier import SchemaVerifier
from adapters.remote_source import fetch as fetch_remote
from adapters.remote_source import invalidate as invalidate_remote
from agents.resolver import MantleResolver
from core.cache import Cache
from core.models import ComparisonTarget
from core.orchestrator import Orchestrator
from core.persistence import Persistence

_reports_dir = HERE / ".reports"
_cache_path = HERE / ".cache.json"


def _models_path() -> Path:
    """Return local path for the active repo's models.py (fetched from URL)."""
    url = _active_repo.get("models_url", "")
    if not url:
        raise HTTPException(503, detail="No models_url configured for active repo")
    return fetch_remote(url)


class _InputSource:
    def targets(self):
        return [ComparisonTarget("database", "public")]


class _SourceAdapter:
    def intended(self, target):
        return load_intended_schema(_models_path())

    def actual(self, target):
        return load_actual_schema(DB_URL)


def _build_orch() -> Orchestrator:
    return Orchestrator(
        input_source=_InputSource(),
        source_adapter=_SourceAdapter(),
        comparator=SchemaComparator(),
        resolver=MantleResolver(),
        executor=SQLExecutor(DB_URL),
        verifier=SchemaVerifier(
            load_intended=lambda: load_intended_schema(_models_path()),
            load_actual=lambda: load_actual_schema(DB_URL),
        ),
        validator=SQLValidator(),
        persistence=Persistence(_reports_dir),
        cache=Cache(_cache_path),
        domain="database",
        principal="demo-sandbox",
    )


orch = _build_orch()

_latest_report_id: str | None = None

# ----- app -----------------------------------------------------------
app = FastAPI(title="Schema Drift", version="1.0.0")


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
    return {"status": "ok", "domain": CONFIG["domain"], "db_configured": bool(DB_URL)}


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
    invalidate_remote()  # force re-fetch of models on next compare
    _latest_report_id = None
    orch = _build_orch()
    return {"active": _active_repo["id"]}


@app.get("/discover")
def discover():
    return {
        "targets": [
            {
                "domain": "database",
                "target": "public",
                "source": _active_repo.get("models_url", ""),
                "repo": _active_repo.get("github", ""),
            }
        ]
    }


@app.post("/compare")
def compare():
    global _latest_report_id
    if not DB_URL:
        raise HTTPException(503, detail="DATABASE_URL not set")
    report = orch.run()
    _latest_report_id = report.id
    return _serialise_report(report)


@app.post("/apply")
def apply(req: ApplyRequest):
    if not DB_URL:
        raise HTTPException(503, detail="DATABASE_URL not set")
    try:
        # human_authorized=True: user clicked Exorcise — allows REVIEW_REQUIRED
        # diffs (e.g. narrowing type changes, DROP operations) to execute after
        # human review of the LLM-proposed SQL shown in the card.
        outcome = orch.apply_one(req.difference_id, human_authorized=True)
    except KeyError:
        raise HTTPException(404, detail="difference not found")
    return _serialise_outcome(outcome)


@app.post("/rollback")
def rollback(req: RollbackRequest):
    if not DB_URL:
        raise HTTPException(503, detail="DATABASE_URL not set")
    try:
        event = orch.rollback(req.difference_id)
    except KeyError:
        raise HTTPException(404, detail="difference not found")
    return _serialise_outcome(event)


@app.post("/demo/reset")
def demo_reset():
    """Re-seeds the Neon DB with drift so judges can replay the experience."""
    if not DB_URL:
        raise HTTPException(503, detail="DATABASE_URL not set")
    try:
        _seed_path = REPO / "fixtures" / "seed.py"
        _spec = importlib.util.spec_from_file_location("fixtures.seed", _seed_path)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.main(DB_URL)
    except Exception as exc:
        raise HTTPException(500, detail=f"seed failed: {exc}") from exc
    global _latest_report_id
    _latest_report_id = None
    invalidate_remote()  # re-fetch models on next compare
    return {
        "status": "reset",
        "message": "Demo drift re-seeded. Click Hunt Ghosts to scan again.",
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
