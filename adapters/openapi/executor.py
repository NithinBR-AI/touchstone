"""
adapters/openapi/executor.py — Req 21, 23

Executor for API contract drift. API fixes are applied as a GitHub PR on the
target repo (Option B). The executor:

  1. Reads patched_content from the validated ChangeProposal payload
     (LLM was called at resolve time — validated content equals executed content)
  2. Fetches the target file sha from the repo default branch via GitHub API
  3. Creates a new branch + commits the change via GitHub API
  4. Opens a pull request — idempotent (returns existing PR if already open)

Implements BatchExecutor protocol: groups diffs by kind, one PR per kind.

Batching rules:
  FIELD_EXTRA  → one PR patching openapi.yaml with ALL undocumented fields.
  FIELD_MISSING → one PR patching main.py with ALL missing fields.
  Other kinds  → no automated PR; returns success=True with review note.

Idempotency: if an open PR on a touchstone/* branch with the same title prefix
  already exists, its URL is returned immediately.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

from core.models import ChangeProposal, Difference, ExecutionResult, Resolution

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
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def _get_file_sha(repo: str, path: str, ref: str = "main") -> str:
    """Returns sha for a file on *ref* (needed for commit API)."""
    info = _gh_request(f"/repos/{repo}/contents/{path}?ref={ref}")
    return info["sha"]


class ContractExecutor:
    """
    Executor protocol implementation for the OpenAPI contract domain.
    Also implements BatchExecutor protocol.

    Reads patched_content from each diff's ChangeProposal payload.
    The LLM was called at resolve time (MantleResolver._resolve_api);
    the content was validated by ContractValidator before reaching here.
    """

    def __init__(self, repo: str, token: str | None = None):
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "")

    def execute(self, resolution: Resolution, difference: Difference) -> ExecutionResult:
        """Single-diff execute — delegates to execute_batch."""
        return self.execute_batch([(resolution, difference)])[0]

    def execute_batch(self, items: list[tuple[Resolution, Difference]]) -> list[ExecutionResult]:
        """
        Batch execute: group by kind, produce one PR per kind.
        Reads patched_content from each resolution's ChangeProposal payload.
        Returns one ExecutionResult per input item.
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

        field_extra = [(r, d) for r, d in items if d.kind in ("FIELD_EXTRA", "FIELD_UNDOCUMENTED")]
        field_missing = [(r, d) for r, d in items if d.kind == "FIELD_MISSING"]
        other = [
            (r, d)
            for r, d in items
            if d.kind not in ("FIELD_EXTRA", "FIELD_UNDOCUMENTED", "FIELD_MISSING")
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
        """Close all open PRs whose head branch starts with 'touchstone/'."""
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
        LLM patch generation + validation happens here at execute time.
        Collects ALL fields from all diffs, makes one combined LLM call.
        Returns (pr_url, error_code, error_summary).
        """
        field_names = sorted(
            {
                f
                for _, d in items
                for f in (d.context.get("fields") or [d.context.get("field", d.target)])
                if f
            }
        )

        all_paths = sorted({d.context.get("path", "") for _, d in items if d.context.get("path")})
        path = all_paths[0] if len(all_paths) == 1 else ", ".join(all_paths)

        # schema_name and operation_id from first diff that has it
        schema_name = next(
            (d.context.get("schema_name", "") for _, d in items if d.context.get("schema_name")),
            "",
        )
        operation_id = next(
            (d.context.get("operation_id", "") for _, d in items if d.context.get("operation_id")),
            "",
        )

        branch = f"{branch_prefix}{uuid.uuid4().hex[:8]}"

        try:
            existing = self._find_existing_pr(title_prefix)
            if existing:
                return existing, "", f"Existing PR: {existing}"

            # Always call LLM at execute time with ALL fields combined
            patched_content, validated = self._llm_patch_and_validate(
                kind, field_names, path, file_name, schema_name, operation_id
            )
            if not patched_content:
                return "", "LLM_FAILED", "LLM returned empty patched_content"

            file_sha = _get_file_sha(self.repo, file_name, ref="main")
            head_sha = self._head_sha()
            self._create_branch(branch, head_sha)

            commit_msg = (
                f"fix(contract): {kind.lower().replace('_', ' ')} — "
                f"{', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            )
            self._commit_file(file_name, patched_content, file_sha, branch, message=commit_msg)

            field_lines = "\n".join(f"- `{f}`" for f in field_names)
            title = (
                f"{title_prefix}: {', '.join(field_names[:3])}{'…' if len(field_names) > 3 else ''}"
            )
            is_draft = not validated

            body = (
                f"Touchstone detected **{kind}** drift.\n\n"
                f"**Fields fixed ({len(field_names)}):**\n{field_lines}\n\n"
            )
            if is_draft:
                body += (
                    "⚠️ **Draft PR — patch validation flagged potential issues.**\n"
                    "Review the diff carefully before merging. The patch was generated by the LLM "
                    "but could not be fully validated automatically."
                )
            else:
                body += "_Patch validated and authorized before PR creation._"

            pr = self._open_pr(branch=branch, title=title, body=body, draft=is_draft)
            pr_url = pr.get("html_url", "")
            return pr_url, "", f"PR opened: {pr_url}"

        except urllib.error.HTTPError as exc:
            body = exc.read().decode() if hasattr(exc, "read") else ""
            return "", str(exc.code), f"GitHub API error {exc.code}: {body[:200]}"
        except Exception as exc:
            return "", type(exc).__name__, str(exc)

    def _llm_patch_and_validate(
        self,
        kind: str,
        all_fields: list[str],
        path: str,
        file_name: str,
        schema_name: str = "",
        operation_id: str = "",
    ) -> tuple[str, bool]:
        """
        Fetch current file, generate patch via deepseek_pro, validate with ultra.
        Returns (patched_content, validated).
        validated=False → draft PR. Empty string → PR blocked.
        """
        from agents.mantle_client import deepseek_pro, strip_llm_wrapper, ultra
        from agents.prompts import _API_SYSTEM_PROMPT, build_api_batch_user_message

        current_content = ""
        for _ in range(2):
            try:
                current_content = self._get_file(file_name)
                break
            except Exception:
                pass
        if not current_content:
            return "", False

        # Preserve original trailing newline convention
        original_ends_newline = current_content.endswith("\n")

        def _generate() -> str:
            user = build_api_batch_user_message(
                kind, all_fields, path, file_name, current_content, schema_name, operation_id
            )
            raw = deepseek_pro(system=_API_SYSTEM_PROMPT, user=user)
            raw = strip_llm_wrapper(raw)
            payload = json.loads(raw)
            content = payload.get("patched_content", "")
            # Normalise trailing newline to match original
            if content:
                content = content.rstrip("\n")
                if original_ends_newline:
                    content += "\n"
            return content

        def _validate(patched: str) -> bool:
            # Build a precise, scoped description of the ONE change that was made.
            if kind in ("FIELD_EXTRA", "FIELD_UNDOCUMENTED"):
                location = f"schema '{schema_name}'" if schema_name else f"path '{path}'"
                change_desc = f"Add fields {all_fields} to {location} in {file_name}."
                scope_note = (
                    f"SCOPE: This drift is scoped to {location} only. "
                    f"If the same field name appears elsewhere in the file (other schemas, "
                    f"other functions), that is NOT part of this diff — do not flag it as missing."
                )
                rules = (
                    "1. Are ALL of these fields present in the diff: {fields}? → fail if any are missing\n"
                    "2. Were they added to {location} and nowhere else?\n"
                    "3. Were any existing lines removed? → fail if yes\n"
                    "4. Were any existing lines modified (content, indentation, quotes)? → fail if yes\n"
                    "Reply valid=true only if checks 1-4 pass. "
                    "Do NOT fail because other locations in the file look inconsistent — that is out of scope."
                ).format(fields=all_fields, location=location)
            elif kind == "FIELD_MISSING":
                func_name = operation_id if operation_id else path
                change_desc = f"Add fields {all_fields} to the return dict of function '{func_name}' in {file_name}."
                scope_note = (
                    f"SCOPE: This drift is scoped to function '{func_name}' only. "
                    f"Other functions in the file may not return these fields — that is NOT a bug in this patch."
                )
                rules = (
                    "1. Are ALL of these fields present in the return dict of '{func_name}': {fields}? → fail if any are missing\n"
                    "2. Were they added only to '{func_name}' and no other function?\n"
                    "3. Were any existing lines removed? → fail if yes\n"
                    "4. Were any existing lines modified? → fail if yes\n"
                    "Reply valid=true only if checks 1-4 pass. "
                    "Do NOT fail because other functions don't have these fields — that is out of scope."
                ).format(func_name=func_name, fields=all_fields)
            else:
                change_desc = f"Fix {kind} drift for fields {all_fields} in {file_name}."
                scope_note = "SCOPE: Only the lines directly related to this drift should change."
                rules = (
                    "1. Were the intended fields fixed correctly?\n"
                    "2. Were any unrelated lines changed? → fail if yes\n"
                    "Reply valid=true only if the patch is correct and surgical."
                )

            system = (
                "You are a code patch validator. Your job is to verify one specific, scoped change.\n"
                "You MUST stay within the stated scope — do not flag issues outside it.\n"
                'Reply with JSON only: {"valid": true/false, "reason": "..."}'
            )
            user = (
                f"INTENDED CHANGE: {change_desc}\n\n"
                f"{scope_note}\n\n"
                f"VALIDATION RULES:\n{rules}\n\n"
                f"ORIGINAL:\n{current_content}\n\n"
                f"PATCHED:\n{patched}"
            )
            try:
                raw = ultra(system=system, user=user)
                raw = strip_llm_wrapper(raw)
                result = json.loads(raw)
                return bool(result.get("valid", False))
            except Exception:
                return False

        # Attempt 1
        try:
            patched = _generate()
        except Exception:
            return "", False

        if not patched:
            return "", False

        if _validate(patched):
            return patched, True

        # Attempt 2
        try:
            patched = _generate()
        except Exception:
            return patched, False  # return last attempt as unvalidated draft

        if not patched:
            return "", False

        if _validate(patched):
            return patched, True

        # Both attempts failed validation — return unvalidated, open draft PR
        return patched, False

    def _get_file(self, file_name: str) -> str:
        """Fetch current file content from GitHub main branch."""
        info = _gh_request(
            f"/repos/{self.repo}/contents/{file_name}?ref=main",
            token=self.token,
        )
        return base64.b64decode(info["content"]).decode()

    def _find_existing_pr(self, title_prefix: str) -> str:
        try:
            prs = _gh_request(
                f"/repos/{self.repo}/pulls?state=open&per_page=100",
                token=self.token,
            )
            for pr in prs:
                head_ref = pr.get("head", {}).get("ref", "")
                if head_ref.startswith("touchstone/") and pr.get("title", "").startswith(
                    title_prefix
                ):
                    return pr.get("html_url", "")
        except Exception:
            pass
        return ""

    def _head_sha(self) -> str:
        ref_info = _gh_request(f"/repos/{self.repo}/git/ref/heads/main", token=self.token)
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

    def _open_pr(self, branch: str, title: str, body: str, draft: bool = False) -> dict:
        return _gh_request(
            f"/repos/{self.repo}/pulls",
            method="POST",
            body={"title": title, "body": body, "head": branch, "base": "main", "draft": draft},
            token=self.token,
        )
