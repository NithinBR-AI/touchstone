import json
import subprocess
import sys
from pathlib import Path


def get_file_path(event: dict) -> str | None:
    KNOWN_KEYS = ("file_path", "filePath", "path", "file", "filename", "uri")
    for key in KNOWN_KEYS:
        if key in event:
            return str(event[key])
    for v in json.dumps(event, default=str).split():
        if "adapters" in v and "comparator.py" in v:
            return v
    return None


def main():
    try:
        data = sys.stdin.read()
        event = json.loads(data)
    except Exception:
        print("Skipping: invalid JSON")
        sys.exit(0)

    file_path = get_file_path(event)
    if not file_path:
        print("Skipping: no file path in event")
        sys.exit(0)

    try:
        path = Path(file_path)
        if "adapters" not in str(path) or "comparator.py" not in str(path):
            print("Skipping: not an adapter comparator.py")
            sys.exit(0)
        domain = path.parent.name
    except Exception:
        print("Skipping: bad path")
        sys.exit(0)

    # Adapter folder name -> test file suffix (folder names differ from test names).
    TEST_SUFFIX = {"postgres": "pg", "openapi": "api", "redis": "cache"}
    test_file = f"tests/test_comparator_{TEST_SUFFIX.get(domain, domain)}.py"
    if not Path(test_file).exists():
        print(f"Skipping: {test_file} not found")
        sys.exit(0)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", test_file],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True,
        text=True,
    )
    output = result.stdout or result.stderr
    print(output)
    _log_run(test_file, output, result.returncode)
    sys.exit(result.returncode)


def _log_run(test_file: str, output: str, returncode: int) -> None:
    """Append one line per hook run to .kiro/hook-runs.log (evidence that the hook fired)."""
    import datetime

    lines = [ln for ln in output.strip().splitlines() if ln.strip()]
    summary = lines[-1] if lines else "no output"
    log = Path(__file__).resolve().parent.parent / ".kiro" / "hook-runs.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} "
                f"{test_file} exit={returncode} {summary}\n")


if __name__ == "__main__":
    main()
