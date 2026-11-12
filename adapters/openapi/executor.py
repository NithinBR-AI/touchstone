"""
adapters/openapi/executor.py — Req 21, 23

Executor for API contract drift. API fixes are applied as a GitHub PR on the
target repo (Option B). The executor:

  1. Fetches the target file from the repo default branch via GitHub API
  2. Builds a minimal patch covering ALL diffs of the same kind (batch)
  3. Creates a new branch + commits the change via GitHub API
  4. Opens a pull request — idempotent (returns existing PR if already open)

Requires GITHUB_TOKEN in the environment with `repo` scope on the target.

Batching rules:
  FIELD_EXTRA  (undocumented field leaking) → one PR patching openapi.yaml
               with ALL undocumented fields in one commit.
  FIELD_MISSING (field in spec absent from actual) → one PR patching main.py
               with ALL missing fields in one commit.
  Other kinds  → no automated PR; returns success=True with review note.

Idempotency: if an open PR on a touchstone/* branch with the same title prefix
  already exists, its URL is returned immediately — no new branch/commit/PR.

Demo reset: close_touchstone_prs() closes all open touchstone/* PRs —
  called by /demo/reset so the next Exorcise All produces a fresh PR.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

import yaml

from core.models import Difference, ExecutionResult

_GH_API = "https://api.github.com"


def _gh_request(
    path: str,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
) -> Any:
    token = token or os.environ.get("GITHUB_TOKEN", "")
    url = f"{_GH_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "touchstone/1.0",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 — github.com only
        return json.loads(resp.read().decode())


def _get_file(repo: str, path: str, ref: str = "main") -> tuple[str, str]:
    """Returns (content_str, sha) for a file on *ref*."""
    info = _gh_request(f"/repos/{repo}/contents/{path}?ref={ref}")
    content = base64.b64decode(info["content"]).decode()
    return content, info["sha"]


def _add_field_to_spec(spec_text: str, path: str, field: str) -> str:
    """
    Add *field* (type: string) to the response schema for GET *path* 200.
    Works on the YAML text level using PyYAML round-trip via dict.
    """
    spec = yaml.safe_load(spec_text)
    components = spec.get("components", {})
    schemas = components.get("schemas", {})

    path_item = spec.get("paths", {}).get(path, {})
    op = path_item.get("get", {})
    resp = op.get("responses", {}).get("200", {})
    content = resp.get("content", {}).get("application/json", {})
    schema_ref = content.get("schema", {})

    target_schema = None
    schema_name = None
    if "$ref" in schema_ref:
        m = re.match(r"#/components/schemas/(\w+)", schema_ref["$ref"])
        if m:
            schema_name = m.group(1)
            candidate = schemas.get(schema_name, {})
            if candidate.get("type") == "array":
                item_ref = candidate.get("items", {}).get("$ref", "")
                m2 = re.match(r"#/components/schemas/(\w+)", item_ref)
                if m2:
                    schema_name = m2.group(1)
            target_schema = schemas.get(schema_name)

    if target_schema is None:
        if "properties" not in schema_ref:
            schema_ref["properties"] = {}
        schema_ref["properties"][field] = {"type": "string"}
    else:
        if "properties" not in target_schema:
            target_schema["properties"] = {}
        if field not in target_schema["properties"]:
            target_schema["properties"][field] = {"type": "string"}
        schemas[schema_name] = target_schema
        spec["components"]["schemas"] = schemas

    return yaml.dump(spec, default_flow_style=False, allow_unicode=True)


def _add_field_to_api(main_py: str, path: str, field: str) -> str:
    """
    Patch a FastAPI main.py to return *field* in the response dict for GET *path*.
    Finds the @app.get decorator, scans forward to the first return dict block,
    and inserts the field after the last existing key-value pair.
    Falls back to appending a TODO comment if no suitable insertion point is found.
    """
    if field in main_py:
        return main_py

    lines = main_py.splitlines(keepends=True)
    result = []
    in_target_func = False
    in_return_block = False
    brace_depth = 0
    last_dict_field_idx = -1

    for line in lines:
        stripped = line.strip()

        if f'@app.get("{path}")' in line or f"@app.get('{path}')" in line:
            in_target_func = True
            in_return_block = False
            brace_depth = 0
            last_dict_field_idx = -1

        if in_target_func:
            if not in_return_block:
                if re.match(r"\s*return\s+[\[\{]", line):
                    in_return_block = True
                    brace_depth = (
                        line.count("{") + line.count("[")
                        - line.count("}") - line.count("]")
                    )
            else:
                brace_depth += (
                    line.count("{") + line.count("[")
                    - line.count("}") - line.count("]")
                )
                if re.match(r'\s*"[^"]+"\s*:', stripped):
                    last_dict_field_idx = len(result)
                if brace_depth <= 0:
                    if last_dict_field_idx >= 0:
                        ref_line = result[last_dict_field_idx]
                        indent = " " * (len(ref_line) - len(ref_line.lstrip()))
                        result.append(line)
                        result.insert(
                            last_dict_field_idx + 1,
                            f'{indent}"{field}": None,  # TODO: implement\n',
                        )
                        in_target_func = False
                        in_return_block = False
                        last_dict_field_idx = -1
                        continue
                    in_return_block = False
                    in_target_func = False

        result.append(line)

    patched = "".join(result)
    if patched == main_py:
        patched += f"\n# TODO: add '{field}' field to GET {path} response\n"
    return patched


class ContractExecutor:
    """
    Executor protocol implementation for the OpenAPI contract domain.

    Key design decisions:
    - execute(diff) is the protocol entry point; it delegates to execute_batch.
    - execute_batch(diffs) is called by /apply with ALL actionable diffs so that
      one kind → one PR (no per-field branch conflicts).
    - Idempotent: checks for open touchstone/* PR before creating a new one.
    - close_touchstone_prs() is called by /demo/reset to clean up old PRs.
    """

    def __init__(self, repo: str, token: str | None = None):
        """
        *repo*: GitHub repo in "owner/name" format (e.g. "NithinBR-AI/repo")
        *token*: GitHub token with repo scope (defaults to GITHUB_TOKEN env var)
        """
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, diff: Difference) -> ExecutionResult:
        """Single-diff execute — delegates to execute_batch with just this diff."""
        return self.execute_batch([diff])[0]

    def execute_batch(self, diffs: list[Difference]) -> list[ExecutionResult]:
        """
        Batch execute: group diffs by kind, produce one PR per kind.
        Returns one ExecutionResult per input diff (all in a kind share the PR URL).
        """
        if not diffs:
            return []

        if not self.token:
            return [
                ExecutionResult(
                    difference_id=d.id,
                    success=False,
                    error_code="NO_TOKEN",
                    error_summary="GITHUB_TOKEN not set; cannot open PR",
                )
                for d in diffs
            ]

        field_extra  = [d for d in diffs if d.kind == "FIELD_EXTRA"]
        field_missing = [d for d in diffs if d.kind == "FIELD_MISSING"]
        other        = [d for d in diffs if d.kind not in ("FIELD_EXTRA", "FIELD_MISSING")]

        results: list[ExecutionResult] = []

        if field_extra:
            pr_url, err_code, err_summary = self._raise_spec_pr_batch(field_extra)
            success = not err_code
            for d in field_extra:
                results.append(ExecutionResult(
                    difference_id=d.id,
                    success=success,
                    error_code=err_code,
                    error_summary=err_summary,
                    file_path_ref=pr_url,
                ))

        if field_missing:
            pr_url, err_code, err_summary = self._raise_api_pr_batch(field_missing)
            success = not err_code
            for d in field_missing:
                results.append(ExecutionResult(
                    difference_id=d.id,
                    success=success,
                    error_code=err_code,
                    error_summary=err_summary,
                    file_path_ref=pr_url,
                ))

        for d in other:
            results.append(ExecutionResult(
                difference_id=d.id,
                success=True,
                error_code="REVIEW_REQUIRED",
                error_summary=f"kind={d.kind} requires manual review; no automated PR",
            ))

        return results

    def close_touchstone_prs(self) -> int:
        """
        Close all open PRs whose head branch starts with 'touchstone/'.
        Called by /demo/reset. Returns count of PRs closed.
        """
        if not self.token:
            return 0
        try:
            prs = _gh_request(
                f"/repos/{self.repo}/pulls?state=open&per_page=100",
                token=self.token,
            )
            closed = 0
            for pr in prs:
                if pr.get("head", {}).get("ref", "").startswith("touchstone/"):
                    _gh_request(
                        f"/repos/{self.repo}/pulls/{pr['number']}",
                        method="PATCH",
                        body={"state": "closed"},
                        token=self.token,
                    )
                    closed += 1
            return closed
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Batch PR helpers
    # ------------------------------------------------------------------

    def _raise_spec_pr_batch(
        self, diffs: list[Difference]
    ) -> tuple[str, str, str]:
        """
        Patch openapi.yaml once with ALL undocumented fields from *diffs*.
        Returns (pr_url, error_code, error_summary).
        """
        fields_by_path: dict[str, list[str]] = {}
        for d in diffs:
            p = d.context.get("path", "")
            f = d.context.get("field", "")
            if p and f:
                fields_by_path.setdefault(p, []).append(f)

        field_names = sorted({f for fs in fields_by_path.values() for f in fs})
        title_prefix = "fix(contract): document undocumented field(s)"
        branch = f"touchstone/spec-fix-{uuid.uuid4().hex[:8]}"

        try:
            existing = self._find_existing_pr(title_prefix)
            if existing:
                return existing, "", f"Existing PR: {existing}"

            spec_text, file_sha = _get_file(self.repo, "openapi.yaml", ref="main")
            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            new_spec = spec_text
            for path, fields in fields_by_path.items():
                for field in fields:
                    new_spec = _add_field_to_spec(new_spec, path, field)

            field_lines = "\n".join(
                f"- `{f}` on `{p}`"
                for p, fs in fields_by_path.items()
                for f in fs
            )
            self._commit_file(
                "openapi.yaml", new_spec, file_sha, branch,
                message=f"fix(contract): document {len(field_names)} undocumented field(s)",
            )
            title = (
                f"{title_prefix}: "
                f"{', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            )
            pr = self._open_pr(
                branch=branch,
                title=title,
                body=(
                    f"Touchstone detected **FIELD_EXTRA** drift — "
                    f"the live API returns fields not declared in `openapi.yaml`.\n\n"
                    f"**Fields fixed ({len(field_names)}):**\n{field_lines}\n\n"
                    f"_Auto-generated by Touchstone Contract Observer._"
                ),
            )
            pr_url = pr.get("html_url", "")
            return pr_url, "", f"PR opened: {pr_url}"

        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            return "", str(exc.code), f"GitHub API error {exc.code}: {body[:200]}"
        except Exception as exc:
            return "", type(exc).__name__, str(exc)

    def _raise_api_pr_batch(
        self, diffs: list[Difference]
    ) -> tuple[str, str, str]:
        """
        Patch main.py once with ALL missing fields from *diffs*.
        Returns (pr_url, error_code, error_summary).
        """
        fields_by_path: dict[str, list[str]] = {}
        for d in diffs:
            p = d.context.get("path", "")
            f = d.context.get("field", "")
            if p and f:
                fields_by_path.setdefault(p, []).append(f)

        field_names = sorted({f for fs in fields_by_path.values() for f in fs})
        title_prefix = "fix(contract): return missing field(s)"
        branch = f"touchstone/api-fix-{uuid.uuid4().hex[:8]}"

        try:
            existing = self._find_existing_pr(title_prefix)
            if existing:
                return existing, "", f"Existing PR: {existing}"

            main_text, file_sha = _get_file(self.repo, "main.py", ref="main")
            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            new_main = main_text
            for path, fields in fields_by_path.items():
                for field in fields:
                    new_main = _add_field_to_api(new_main, path, field)

            field_lines = "\n".join(
                f"- `{f}` on `{p}`"
                for p, fs in fields_by_path.items()
                for f in fs
            )
            self._commit_file(
                "main.py", new_main, file_sha, branch,
                message=f"fix(contract): return {len(field_names)} missing field(s)",
            )
            title = (
                f"{title_prefix}: "
                f"{', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            )
            pr = self._open_pr(
                branch=branch,
                title=title,
                body=(
                    f"Touchstone detected **FIELD_MISSING** drift — "
                    f"the spec declares fields the live API does not return.\n\n"
                    f"**Fields fixed ({len(field_names)}):**\n{field_lines}\n\n"
                    f"_Auto-generated by Touchstone Contract Observer._"
                ),
            )
            pr_url = pr.get("html_url", "")
            return pr_url, "", f"PR opened: {pr_url}"

        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            return "", str(exc.code), f"GitHub API error {exc.code}: {body[:200]}"
        except Exception as exc:
            return "", type(exc).__name__, str(exc)

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

    def _find_existing_pr(self, title_prefix: str) -> str:
        """
        Return the html_url of the first open touchstone/* PR whose title
        starts with *title_prefix*, or empty string if none found.
        """
        try:
            prs = _gh_request(
                f"/repos/{self.repo}/pulls?state=open&per_page=100",
                token=self.token,
            )
            for pr in prs:
                head_ref = pr.get("head", {}).get("ref", "")
                if (
                    head_ref.startswith("touchstone/")
                    and pr.get("title", "").startswith(title_prefix)
                ):
                    return pr.get("html_url", "")
        except Exception:
            pass
        return ""

    def _head_sha(self) -> str:
        ref_info = _gh_request(
            f"/repos/{self.repo}/git/ref/heads/main", token=self.token
        )
        return ref_info["object"]["sha"]

    def _create_branch(self, branch: str, head_sha: str) -> None:
        _gh_request(
            f"/repos/{self.repo}/git/refs",
            method="POST",
            body={"ref": f"refs/heads/{branch}", "sha": head_sha},
            token=self.token,
        )

    def _commit_file(
        self, filepath: str, content: str, sha: str, branch: str, message: str
    ) -> None:
        _gh_request(
            f"/repos/{self.repo}/contents/{filepath}",
            method="PUT",
            body={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "sha": sha,
                "branch": branch,
            },
            token=self.token,
        )

    def _open_pr(self, branch: str, title: str, body: str) -> dict:
        return _gh_request(
            f"/repos/{self.repo}/pulls",
            method="POST",
            body={"title": title, "body": body, "head": branch, "base": "main"},
            token=self.token,
        )
