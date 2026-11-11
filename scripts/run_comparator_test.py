import json
import sys
import subprocess
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

    test_file = f"tests/test_comparator_{domain}.py"
    if domain == "postgres":
        test_file = "tests/test_comparator_pg.py"
    if not Path(test_file).exists():
        print(f"Skipping: {test_file} not found")
        sys.exit(0)

    result = subprocess.run([sys.executable, "-m", "pytest", "-q", test_file], cwd=str(Path(__file__).parent.parent), capture_output=True, text=True)
    print(result.stdout or result.stderr)
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()