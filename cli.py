"""
cli.py — Touchstone CLI entry point.

Declared in pyproject.toml as:
    [project.scripts]
    touchstone = "cli:main"

Usage:
    touchstone compare --target <target>
    touchstone compare --target users.email --domain database
    touchstone compare --target ix_orders_user_id --domain database
    touchstone apply --id <difference-id>
    touchstone report
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env() -> None:
    """Load .env from the project root (best-effort)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def cmd_compare(args: argparse.Namespace) -> int:
    _load_env()
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL not set. Add it to .env or export it.", file=sys.stderr)
        return 1

    target = args.target
    domain = args.domain

    # Dynamically locate demo/models.py for the database domain
    models_path = Path(__file__).parent / "demo" / "models.py"
    if not models_path.exists():
        print(f"ERROR: demo/models.py not found at {models_path}", file=sys.stderr)
        return 1

    from adapters.postgres.actual import load_actual_schema
    from adapters.postgres.comparator import SchemaComparator
    from adapters.postgres.intended import load_intended_schema

    try:
        intended = load_intended_schema(models_path)
        actual = load_actual_schema(db_url)
    except Exception as exc:
        print(f"ERROR loading schema: {exc}", file=sys.stderr)
        return 1

    comparator = SchemaComparator()
    report = comparator.compare(intended, actual)

    if not report.differences:
        print("No differences found.")
        return 0

    print(f"Found {len(report.differences)} difference(s):\n")
    for diff in report.differences:
        if target and diff.target != target:
            continue
        print(f"  [{diff.state.value:17s}] {diff.kind:30s}  target={diff.target}")
        if diff.context:
            for k, v in diff.context.items():
                print(f"               {k}: {v}")
        print()

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    _load_env()
    cache_dir = Path(args.cache_dir) if args.cache_dir else Path("reports")
    if not cache_dir.exists():
        print(f"No reports at {cache_dir}.")
        return 0
    reports = sorted(cache_dir.glob("*.json"))
    if not reports:
        print(f"No JSON reports in {cache_dir}.")
        return 0
    import json
    for path in reports[-5:]:
        data = json.loads(path.read_text())
        print(f"  {path.name}  —  differences: {len(data.get('differences', []))}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="touchstone",
        description="Touchstone — ghost detector for database / API / deps drift",
    )
    sub = parser.add_subparsers(dest="command")

    # touchstone compare
    p_compare = sub.add_parser("compare", help="Compare declared vs actual schema")
    p_compare.add_argument(
        "--target", default="", help="Filter to this target (e.g. users.email)"
    )
    p_compare.add_argument(
        "--domain", default="database", choices=["database", "api", "deps"],
        help="Domain to compare (default: database)",
    )

    # touchstone report
    p_report = sub.add_parser("report", help="Show recent comparison reports")
    p_report.add_argument("--cache-dir", default="", help="Path to reports directory")

    args = parser.parse_args()

    if args.command == "compare":
        sys.exit(cmd_compare(args))
    elif args.command == "report":
        sys.exit(cmd_report(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
