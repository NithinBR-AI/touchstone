"""
Req 30 — each app folder must ship exactly 4 files; shared UI must have 3 files.
Skeleton Crew proof: at least 2 app folders must be complete.
"""

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
APPS_DIR  = REPO_ROOT / "apps"
SHARED_UI = APPS_DIR / "shared" / "ui"

REQUIRED_APP_FILES = {"main.py", "config.json", "README.md", "run.sh"}
REQUIRED_UI_FILES  = {"index.html", "app.js", "style.css"}

# Apps that must be complete before submission (Skeleton Crew — 2+ required)
MANDATORY_APPS = ["schema-drift", "contract-observer"]


def _app_dirs() -> list[Path]:
    """All app dirs except shared/."""
    return [
        p for p in APPS_DIR.iterdir()
        if p.is_dir() and p.name != "shared"
    ]


def _present_files(app_dir: Path) -> set[str]:
    return {
        f.name for f in app_dir.iterdir()
        if f.is_file() and f.name != ".gitkeep"
    }


# ----- shared UI -----

def test_shared_ui_has_required_files():
    present = {f.name for f in SHARED_UI.iterdir() if f.is_file() and f.name != ".gitkeep"}
    missing = REQUIRED_UI_FILES - present
    assert not missing, f"shared/ui missing: {missing}"


def test_shared_ui_no_extra_js_or_css():
    """Single app.js and style.css — no per-domain duplicates."""
    js_files  = list(SHARED_UI.glob("*.js"))
    css_files = list(SHARED_UI.glob("*.css"))
    assert len(js_files)  == 1, f"Expected 1 .js file, found: {[f.name for f in js_files]}"
    assert len(css_files) == 1, f"Expected 1 .css file, found: {[f.name for f in css_files]}"


# ----- per-app -----

@pytest.mark.parametrize("app_name", MANDATORY_APPS)
def test_mandatory_app_has_required_files(app_name):
    app_dir = APPS_DIR / app_name
    assert app_dir.exists(), f"apps/{app_name}/ does not exist"
    present = _present_files(app_dir)
    missing = REQUIRED_APP_FILES - present
    assert not missing, f"apps/{app_name}/ missing: {missing}"


@pytest.mark.parametrize("app_name", MANDATORY_APPS)
def test_mandatory_app_config_has_required_keys(app_name):
    import json
    config_path = APPS_DIR / app_name / "config.json"
    assert config_path.exists(), f"apps/{app_name}/config.json missing"
    cfg = json.loads(config_path.read_text())
    for key in ("domain", "accent", "title"):
        assert key in cfg, f"apps/{app_name}/config.json missing key '{key}'"


def test_skeleton_crew_at_least_two_complete_apps():
    """Kiroween Skeleton Crew: ≥2 app folders each complete."""
    complete = []
    for app_dir in _app_dirs():
        present = _present_files(app_dir)
        if REQUIRED_APP_FILES.issubset(present):
            complete.append(app_dir.name)
    assert len(complete) >= 2, (
        f"Skeleton Crew requires ≥2 complete apps; complete so far: {complete}"
    )


def test_all_complete_apps_use_shared_ui():
    """Every app main.py must reference shared/ui (not bundle its own copy)."""
    for app_dir in _app_dirs():
        main_py = app_dir / "main.py"
        if not main_py.exists():
            continue
        source = main_py.read_text()
        assert "shared" in source and "ui" in source, (
            f"apps/{app_dir.name}/main.py does not mount shared/ui"
        )
