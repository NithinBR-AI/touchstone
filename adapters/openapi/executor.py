"""
adapters/openapi/executor.py — Req 21, 23

Executor for API contract drift. API fixes are applied as a GitHub PR on the
target repo (Option B). The executor:

  1. Fetches the target file from the repo default branch via GitHub API
  2. Builds a minimal patch
  3. Creates a new branch + commits the change via GitHub API
  4. Opens a pull request

Requires GITHUB_TOKEN in the environment with `repo` scope on the target.

For FIELD_EXTRA (undocumented field leaking): patches openapi.yaml to add the field to the spec.
For FIELD_MISSING (field in spec but absent from actual): patches main.py to add the field to the API response.
Other kinds: returns a no-op result with a note.
"""

from __future__ import annotations

import base64
import copy
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
    with urllib.request.urlopen(
        req, timeout=20
    ) as resp:  # noqa: S310 — github.com only
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

    # Find the schema for GET {path} 200
    path_item = spec.get("paths", {}).get(path, {})
    op = path_item.get("get", {})
    resp = op.get("responses", {}).get("200", {})
    content = resp.get("content", {}).get("application/json", {})
    schema_ref = content.get("schema", {})

    # Resolve $ref
    target_schema = None
    schema_name = None
    if "$ref" in schema_ref:
        m = re.match(r"#/components/schemas/(\w+)", schema_ref["$ref"])
        if m:
            schema_name = m.group(1)
            # Unwrap array
            candidate = schemas.get(schema_name, {})
            if candidate.get("type") == "array":
                item_ref = candidate.get("items", {}).get("$ref", "")
                m2 = re.match(r"#/components/schemas/(\w+)", item_ref)
                if m2:
                    schema_name = m2.group(1)
            target_schema = schemas.get(schema_name)

    if target_schema is None:
        # Inline schema fallback — patch directly
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

    Generic strategy (works on any FastAPI app):
    1. Find the @app.get decorator for *path*
    2. Scan forward to the first `return {` or `return [` block
    3. Find the last `"key": value` line inside that block
    4. Insert `"<field>": None,  # TODO: implement` after it

    Falls back to appending a TODO comment if no suitable insertion point is found.
    """
    if field in main_py:
        # Field already present — no-op
        return main_py

    lines = main_py.splitlines(keepends=True)
    result = []
    in_target_func = False
    in_return_block = False
    brace_depth = 0
    last_dict_field_idx = -1  # index in result[] of the last "key": value line

    for line in lines:
        stripped = line.strip()

        # Detect the decorator for this path
        if f'@app.get("{path}")' in line or f"@app.get('{path}')" in line:
            in_target_func = True
            in_return_block = False
            brace_depth = 0
            last_dict_field_idx = -1

        if in_target_func:
            if not in_return_block:
                # Look for start of a return dict/list
                if re.match(r"\s*return\s+[\[\{]", line):
                    in_return_block = True
                    brace_depth = (
                        line.count("{")
                        + line.count("[")
                        - line.count("}")
                        - line.count("]")
                    )
            else:
                brace_depth += (
                    line.count("{")
                    + line.count("[")
                    - line.count("}")
                    - line.count("]")
                )
                # Track last "key": value line in the dict
                if re.match(r'\s*"[^"]+"\s*:', stripped):
                    last_dict_field_idx = len(result)
                if brace_depth <= 0:
                    # End of return block — insert after the last field line
                    if last_dict_field_idx >= 0:
                        # Determine indent from that line
                        ref_line = result[last_dict_field_idx]
                        indent = " " * (len(ref_line) - len(ref_line.lstrip()))
                        result.append(line)  # add closing line first
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
        # Fallback: append a TODO comment at end of file
        patched += f"\n# TODO: add '{field}' field to GET {path} response\n"
    return patched


class ContractExecutor:
    """Executor protocol implementation for the OpenAPI contract domain."""

    def __init__(self, repo: str, token: str | None = None):
        """
        *repo*: GitHub repo in "owner/name" format
        *token*: GitHub token with repo scope (defaults to GITHUB_TOKEN env var)
        """
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "")

    def execute(self, diff: Difference) -> ExecutionResult:
        if diff.kind not in ("FIELD_EXTRA", "FIELD_MISSING"):
            return ExecutionResult(
                success=True,
                applied_change=None,
                raw_output=f"kind={diff.kind} requires manual review; no automated PR",
            )

        if not self.token:
            return ExecutionResult(
                success=False,
                applied_change=None,
                error="GITHUB_TOKEN not set; cannot open PR",
            )

        if diff.kind == "FIELD_EXTRA":
            return self._raise_spec_pr(diff)
        else:
            return self._raise_api_pr(diff)

    def _raise_spec_pr(self, diff: Difference) -> ExecutionResult:
        """FIELD_EXTRA: patch openapi.yaml to document the undocumented field."""
        path = diff.context.get("path", "")
        field = diff.context.get("field", "")
        branch = f"touchstone/contract-fix-{field}-{uuid.uuid4().hex[:6]}"

        try:
            spec_text, file_sha = _get_file(self.repo, "openapi.yaml", ref="main")
            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            new_spec = _add_field_to_spec(spec_text, path, field)
            self._commit_file(
                "openapi.yaml",
                new_spec,
                file_sha,
                branch,
                message=f"fix: document {field} field on {path}",
            )

            pr = self._open_pr(
                branch=branch,
                title=f"fix(contract): document undocumented field '{field}' on {path}",
                body=(
                    f"Touchstone detected **FIELD_EXTRA** drift:\n\n"
                    f"- **Path**: `{path}`\n"
                    f"- **Field**: `{field}`\n\n"
                    f"The live API returns `{field}` but it is not declared in `openapi.yaml`. "
                    f"This PR adds it to the spec.\n\n"
                    f"_Auto-generated by Touchstone Contract Observer._"
                ),
            )
            pr_url = pr.get("html_url", "")
            return ExecutionResult(
                success=True,
                applied_change={
                    "pr_url": pr_url,
                    "branch": branch,
                    "field": field,
                    "file": "openapi.yaml",
                },
                raw_output=f"PR opened: {pr_url}",
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            return ExecutionResult(
                success=False,
                applied_change=None,
                error=f"GitHub API error {exc.code}: {body[:200]}",
            )
        except Exception as exc:
            return ExecutionResult(success=False, applied_change=None, error=str(exc))

    def _raise_api_pr(self, diff: Difference) -> ExecutionResult:
        """FIELD_MISSING: patch main.py to return the missing field."""
        path = diff.context.get("path", "")
        field = diff.context.get("field", "")
        branch = f"touchstone/contract-add-{field}-{uuid.uuid4().hex[:6]}"

        try:
            main_text, file_sha = _get_file(self.repo, "main.py", ref="main")
            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            new_main = _add_field_to_api(main_text, path, field)
            self._commit_file(
                "main.py",
                new_main,
                file_sha,
                branch,
                message=f"fix: return {field} field on {path}",
            )

            pr = self._open_pr(
                branch=branch,
                title=f"fix(contract): return missing field '{field}' on {path}",
                body=(
                    f"Touchstone detected **FIELD_MISSING** drift:\n\n"
                    f"- **Path**: `{path}`\n"
                    f"- **Field**: `{field}`\n\n"
                    f"The spec declares `{field}` but the live API does not return it. "
                    f"This PR adds the field to the API response.\n\n"
                    f"_Auto-generated by Touchstone Contract Observer._"
                ),
            )
            pr_url = pr.get("html_url", "")
            return ExecutionResult(
                success=True,
                applied_change={
                    "pr_url": pr_url,
                    "branch": branch,
                    "field": field,
                    "file": "main.py",
                },
                raw_output=f"PR opened: {pr_url}",
            )

        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            return ExecutionResult(
                success=False,
                applied_change=None,
                error=f"GitHub API error {exc.code}: {body[:200]}",
            )
        except Exception as exc:
            return ExecutionResult(success=False, applied_change=None, error=str(exc))

    # ------------------------------------------------------------------
    # GitHub API helpers
    # ------------------------------------------------------------------

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
