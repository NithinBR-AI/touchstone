"""
Schema Drift app — ~40 lines.

Wires the Postgres adapter to the core orchestrator and exposes
the 7 required endpoints. Auth: demo-sandbox (apply/rollback → 403).
State: module-level orchestrator singleton, one per process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

HERE   = Path(__file__).parent
REPO   = HERE.parent.parent
CONFIG = json.loads((HERE / "config.json").read_text())

DB_URL      = os.environ.get("DATABASE_URL", "")
DEMO_MODELS = REPO / "demo" / "models.py"

# ----- adapter wiring ------------------------------------------------
from adapters.postgres.actual   import load_actual_schema
from adapters.postgres.comparator import SchemaComparator
from adapters.postgres.executor import SQLExecutor
from adapters.postgres.intended import load_intended_schema
from adapters.postgres.verifier import SchemaVerifier
from agents.resolver            import MockResolver
from core.cache                 import Cache
from core.models                import ComparisonTarget
from core.orchestrator          import Orchestrator
from core.persistence           import Persistence
from core.validator             import FilePatchValidator, SQLValidator

_reports_dir = HERE / ".reports"
_cache_path  = HERE / ".cache.json"

class _InputSource:
    def targets(self):
        return [ComparisonTarget("database", "public")]

class _SourceAdapter:
    def intended(self, target):
        return load_intended_schema(DEMO_MODELS)
    def actual(self, target):
        return load_actual_schema(DB_URL)

orch = Orchestrator(
    input_source   = _InputSource(),
    source_adapter = _SourceAdapter(),
    comparator     = SchemaComparator(),
    resolver       = MockResolver(),
    executor       = SQLExecutor(DB_URL),
    verifier       = SchemaVerifier(
        load_intended=lambda: load_intended_schema(DEMO_MODELS),
        load_actual  =lambda: load_actual_schema(DB_URL),
    ),
    validator_sql  = SQLValidator(),
    validator_file = FilePatchValidator(REPO),
    persistence    = Persistence(_reports_dir),
    cache          = Cache(_cache_path),
    domain         = "database",
    principal      = "demo-sandbox",
)

_latest_report_id: str | None = None

# ----- app -----------------------------------------------------------
app = FastAPI(title="Schema Drift", version="1.0.0")

# ----- auth guard ----------------------------------------------------
def _require_write() -> None:
    raise HTTPException(403, detail="apply disabled in demo mode")

# ----- request bodies ------------------------------------------------
class ApplyRequest(BaseModel):
    difference_id: str

class RollbackRequest(BaseModel):
    difference_id: str

# ----- endpoints -----------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "domain": CONFIG["domain"], "db_configured": bool(DB_URL)}

@app.get("/config")
def get_config():
    return {**CONFIG, "latest_report_id": _latest_report_id}

@app.get("/discover")
def discover():
    return {"targets": [{"domain": "database", "target": "public", "source": str(DEMO_MODELS)}]}

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
    _require_write()

@app.post("/rollback")
def rollback(req: RollbackRequest):
    _require_write()

@app.get("/report/{report_id}")
def get_report(report_id: str):
    report = orch.persistence.load(report_id)
    if report is None:
        raise HTTPException(404, detail="report not found")
    return _serialise_report(report)

# ----- serialisation -------------------------------------------------
def _serialise_report(report) -> dict:
    import dataclasses, datetime
    def default(o):
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if hasattr(o, "value"):      # enum
            return o.value
        if isinstance(o, datetime.datetime):
            return o.isoformat()
        if isinstance(o, bytes):
            return o.hex()
        raise TypeError(type(o).__name__)
    import json as _json
    return _json.loads(_json.dumps(dataclasses.asdict(report), default=default))

# ----- static UI (must be last mount) --------------------------------
app.mount(
    "/",
    StaticFiles(directory=HERE.parent / "shared" / "ui", html=True),
    name="ui",
)
