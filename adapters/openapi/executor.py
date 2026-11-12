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
import urllib.error
import urllib.request
import uuid
from typing import Any

from core.models import Difference, ExecutionResult, FilePatch, Resolution

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

    def execute(
        self, resolution: Resolution, difference: Difference
    ) -> ExecutionResult:
        """Single-diff execute — delegates to execute_batch with resolution context."""
        return self.execute_batch([(resolution, difference)])[0]

    def execute_batch(
        self, items: list[tuple[Resolution, Difference]]
    ) -> list[ExecutionResult]:
        """
        Batch execute: group by kind, produce one PR per kind using LLM-generated content.
        Returns one ExecutionResult per input item (all in a kind share the PR URL).
        """
        if not items:
            return []

        if not self.token:
            return [
                ExecutionResult(
                    difference_id=diff.id,
                    success=False,
                    error_code="NO_TOKEN",
                    error_summary="GITHUB_TOKEN not set; cannot open PR",
                )
                for _, diff in items
            ]

        field_extra = [(r, d) for r, d in items if d.kind == "FIELD_EXTRA"]
        field_missing = [(r, d) for r, d in items if d.kind == "FIELD_MISSING"]
        other = [
            (r, d) for r, d in items if d.kind not in ("FIELD_EXTRA", "FIELD_MISSING")
        ]

        results: list[ExecutionResult] = []

        if field_extra:
            pr_url, err_code, err_summary = self._raise_pr_batch(
                field_extra,
                "openapi.yaml",
                "fix(contract): document undocumented field(s)",
                "touchstone/spec-fix-",
                "FIELD_EXTRA",
            )
            success = not err_code
            for _, d in field_extra:
                results.append(
                    ExecutionResult(
                        difference_id=d.id,
                        success=success,
                        error_code=err_code,
                        error_summary=err_summary,
                        file_path_ref=pr_url,
                    )
                )

        if field_missing:
            pr_url, err_code, err_summary = self._raise_pr_batch(
                field_missing,
                "main.py",
                "fix(contract): return missing field(s)",
                "touchstone/api-fix-",
                "FIELD_MISSING",
            )
            success = not err_code
            for _, d in field_missing:
                results.append(
                    ExecutionResult(
                        difference_id=d.id,
                        success=success,
                        error_code=err_code,
                        error_summary=err_summary,
                        file_path_ref=pr_url,
                    )
                )

        for _, d in other:
            results.append(
                ExecutionResult(
                    difference_id=d.id,
                    success=True,
                    error_code="REVIEW_REQUIRED",
                    error_summary=f"kind={d.kind} requires manual review; no automated PR",
                )
            )

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
    # Batch PR helper
    # ------------------------------------------------------------------

    def _raise_pr_batch(
        self,
        items: list[tuple[Resolution, Difference]],
        file_name: str,
        title_prefix: str,
        branch_prefix: str,
        kind: str,
    ) -> tuple[str, str, str]:
        """
        Open one PR for all diffs of the same kind.

        Uses the LLM-generated patched_content from each resolution's FilePatch.
        When multiple diffs affect the same file, the last resolution's content
        wins (the LLM was given all context including all fields to fix at once
        via the batch prompt — the orchestrator ensures this).

        Returns (pr_url, error_code, error_summary).
        """
        # Collect field names for the PR title/body — context.fields is now a list
        field_names = sorted(
            {
                f
                for _, d in items
                for f in (d.context.get("fields") or [d.context.get("field", d.target)])
                if f
            }
        )

        branch = f"{branch_prefix}{uuid.uuid4().hex[:8]}"

        try:
            existing = self._find_existing_pr(title_prefix)
            if existing:
                return existing, "", f"Existing PR: {existing}"

            # Fetch file once — need both content (for LLM) and sha (for commit API)
            current_content, file_sha = _get_file(self.repo, file_name, ref="main")

            # One LLM call with ALL fields — produces one complete patch
            patched_content, patch_err = self._llm_patch(
                current_content, file_name, kind, items
            )
            if patch_err:
                return "", "NO_PATCH", patch_err

            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            commit_msg = f"fix(contract): {kind.lower().replace('_', ' ')} — {', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            self._commit_file(
                file_name, patched_content, file_sha, branch, message=commit_msg
            )

            field_lines = "\n".join(f"- `{f}`" for f in field_names)
            title = f"{title_prefix}: {', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            pr = self._open_pr(
                branch=branch,
                title=title,
                body=(
                    f"Touchstone detected **{kind}** drift.\n\n"
                    f"**Fields fixed ({len(field_names)}):**\n{field_lines}\n\n"
                    f"_Patch generated by Touchstone LLM. Auto-raised by Contract Observer._"
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
    # LLM patch generation
    # ------------------------------------------------------------------

    def _llm_patch(
        self,
        current_content: str,
        file_name: str,
        kind: str,
        items: list[tuple],
    ) -> tuple[str, str]:
        """
        Make one LLM call with ALL affected fields and the full file content.
        Returns (patched_content, error_string). error_string is "" on success.
        """
        import json

        from agents.mantle_client import deepseek_pro, strip_llm_wrapper
        from agents.prompts import (_API_SYSTEM_PROMPT,
                                    build_api_batch_user_message)

        all_fields = sorted(
            {
                f
                for _, d in items
                for f in (
                    d.context.get("fields")
                    or ([d.context.get("field")] if d.context.get("field") else [])
                )
            }
        )
        path = items[0][1].context.get("path", "") if items else ""

        user = build_api_batch_user_message(
            kind, all_fields, path, file_name, current_content
        )

        try:
            raw = deepseek_pro(system=_API_SYSTEM_PROMPT, user=user)
            raw = strip_llm_wrapper(raw)
            payload = json.loads(raw)
            content = payload.get("patched_content", "")
            if not content:
                return "", "LLM returned empty patched_content"
            return content, ""
        except Exception as exc:
            return "", f"LLM patch failed: {type(exc).__name__}: {exc}"

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
                if head_ref.startswith("touchstone/") and pr.get(
                    "title", ""
                ).startswith(title_prefix):
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
