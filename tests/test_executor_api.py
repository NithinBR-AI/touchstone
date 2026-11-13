"""
tests/test_executor_api.py

Regression tests for ContractExecutor batch behaviour, idempotency,
demo reset, and ExecutionResult field correctness.

No real GitHub API calls — all _gh_request calls are patched.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, call, patch

import pytest

from adapters.openapi.executor import ContractExecutor
from core.models import Difference, GhostState, Resolution


def _stub_resolution(diff: Difference) -> Resolution:
    return Resolution(
        difference_id=diff.id, difference_signature=diff.signature, proposed_change=None
    )


def _items(diffs: list) -> list:
    return [(_stub_resolution(d), d) for d in diffs]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff(kind: str, path: str = "/products", field: str = "sku") -> Difference:
    return Difference(
        id=str(uuid.uuid4()),
        signature=f"{kind}-{path}-{field}",
        kind=kind,
        target=f"{path} 200.{field}",
        domain="api",
        context={"path": path, "field": field},
    )


def _make_executor(repo: str = "owner/repo", token: str = "tok") -> ContractExecutor:
    return ContractExecutor(repo=repo, token=token)


# ---------------------------------------------------------------------------
# ExecutionResult field correctness — no wrong kwargs
# ---------------------------------------------------------------------------


class TestExecutionResultFields:
    """Regression: executor previously used error= and raw_output= which don't
    exist on ExecutionResult — causing TypeError after PR was already created."""

    def test_no_token_returns_valid_result(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        ex = ContractExecutor(repo="owner/repo", token="")
        d = _diff("FIELD_EXTRA")
        result = ex.execute(_stub_resolution(d), d)
        assert result.difference_id  # not empty
        assert result.success is False
        assert result.error_code == "NO_TOKEN"
        assert "GITHUB_TOKEN" in result.error_summary

    def test_review_required_kind_returns_valid_result(self):
        """Non-batchable kinds return success=True with REVIEW_REQUIRED code."""
        ex = _make_executor()
        d = _diff("PATH_MISSING", field="")
        with patch("adapters.openapi.executor._gh_request", return_value=[]):
            result = ex.execute(_stub_resolution(d), d)
        assert result.success is True
        assert result.error_code == "REVIEW_REQUIRED"
        assert result.difference_id  # not empty

    def test_field_extra_success_sets_file_path_ref(self):
        """Successful FIELD_EXTRA batch sets file_path_ref to the PR URL."""
        ex = _make_executor()
        diff = _diff("FIELD_EXTRA")

        fake_pr = {"html_url": "https://github.com/owner/repo/pull/42"}
        fake_sha = {"sha": "abc123"}
        fake_ref = {"object": {"sha": "deadbeef"}}

        with (
            patch("adapters.openapi.executor._gh_request") as mock_gh,
            patch.object(
                ContractExecutor,
                "_llm_patch_and_validate",
                return_value=("patched: content\n", True),
            ),
        ):
            mock_gh.side_effect = [
                [],  # _find_existing_pr: list open PRs → none
                fake_sha,  # _get_file_sha
                fake_ref,  # _head_sha
                {},  # _create_branch
                {},  # _commit_file
                fake_pr,  # _open_pr
            ]
            result = ex.execute(_stub_resolution(diff), diff)

        assert result.success is True
        assert result.file_path_ref == "https://github.com/owner/repo/pull/42"
        assert result.difference_id == diff.id

    def test_github_error_returns_failure_not_exception(self):
        """HTTPError from GitHub is caught and returned as success=False, not raised."""
        import urllib.error

        ex = _make_executor()
        diff = _diff("FIELD_EXTRA")

        err = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/pulls",
            code=422,
            msg="Unprocessable",
            hdrs={},  # type: ignore[arg-type]
            fp=None,
        )

        with (
            patch("adapters.openapi.executor._gh_request") as mock_gh,
            patch.object(
                ContractExecutor, "_llm_patch_and_validate", return_value=("patched\n", True)
            ),
        ):
            mock_gh.side_effect = [
                [],
                err,
            ]  # find_existing_pr OK, then error on _get_file
            result = ex.execute(_stub_resolution(diff), diff)

        assert result.success is False
        assert result.error_code == "422"
        assert result.difference_id == diff.id


# ---------------------------------------------------------------------------
# Batching — one PR per kind
# ---------------------------------------------------------------------------


class TestBatching:

    def test_multiple_field_extra_produce_one_pr(self):
        """3 FIELD_EXTRA diffs → one PR, all marked success."""
        ex = _make_executor()
        diffs = [
            _diff("FIELD_EXTRA", "/products", "sku"),
            _diff("FIELD_EXTRA", "/products", "weight"),
            _diff("FIELD_EXTRA", "/orders", "tracking_id"),
        ]

        fake_pr = {"html_url": "https://github.com/owner/repo/pull/1"}
        fake_sha = {"sha": "abc"}
        fake_ref = {"object": {"sha": "deadbeef"}}

        with (
            patch("adapters.openapi.executor._gh_request") as mock_gh,
            patch.object(
                ContractExecutor, "_llm_patch_and_validate", return_value=("patched\n", True)
            ),
        ):
            mock_gh.side_effect = [
                [],  # find_existing_pr
                fake_sha,  # _get_file_sha
                fake_ref,  # head_sha
                {},  # create_branch
                {},  # commit_file
                fake_pr,  # open_pr
            ]
            results = ex.execute_batch(_items(diffs))

        assert len(results) == 3
        assert all(r.success for r in results)
        # All share the same PR URL
        assert all(r.file_path_ref == "https://github.com/owner/repo/pull/1" for r in results)
        # Each result maps to its own diff id
        assert {r.difference_id for r in results} == {d.id for d in diffs}

    def test_mixed_kinds_produce_separate_prs(self):
        """FIELD_EXTRA and FIELD_MISSING in one batch → two separate PRs."""
        ex = _make_executor()
        extra = _diff("FIELD_EXTRA", "/products", "sku")
        missing = _diff("FIELD_MISSING", "/products", "weight")

        fake_spec_pr = {"html_url": "https://github.com/owner/repo/pull/1"}
        fake_api_pr = {"html_url": "https://github.com/owner/repo/pull/2"}
        fake_sha = {"sha": "abc"}
        fake_ref = {"object": {"sha": "deadbeef"}}

        with (
            patch("adapters.openapi.executor._gh_request") as mock_gh,
            patch.object(
                ContractExecutor, "_llm_patch_and_validate", return_value=("patched\n", True)
            ),
        ):
            mock_gh.side_effect = [
                [],  # find_existing_pr (spec)
                fake_sha,  # _get_file_sha openapi.yaml
                fake_ref,  # head_sha (spec)
                {},  # create_branch (spec)
                {},  # commit_file (spec)
                fake_spec_pr,  # open_pr (spec)
                [],  # find_existing_pr (api)
                fake_sha,  # _get_file_sha main.py
                fake_ref,  # head_sha (api)
                {},  # create_branch (api)
                {},  # commit_file (api)
                fake_api_pr,  # open_pr (api)
            ]
            results = ex.execute_batch(_items([extra, missing]))

        assert len(results) == 2
        pr_urls = {r.file_path_ref for r in results}
        assert "https://github.com/owner/repo/pull/1" in pr_urls
        assert "https://github.com/owner/repo/pull/2" in pr_urls

    def test_other_kinds_get_review_required(self):
        """Non-batchable kinds return success=True with REVIEW_REQUIRED, no GH call."""
        ex = _make_executor()
        diffs = [
            _diff("PATH_MISSING", "/orders", ""),
            _diff("TYPE_MISMATCH", "/products", "price"),
        ]
        with patch("adapters.openapi.executor._gh_request") as mock_gh:
            results = ex.execute_batch(_items(diffs))

        mock_gh.assert_not_called()
        assert all(r.success is True for r in results)
        assert all(r.error_code == "REVIEW_REQUIRED" for r in results)

    def test_empty_batch_returns_empty(self):
        ex = _make_executor()
        assert ex.execute_batch(_items([])) == []


# ---------------------------------------------------------------------------
# Idempotency — existing PR returned, no new branch/commit
# ---------------------------------------------------------------------------


class TestIdempotency:

    def test_existing_spec_pr_returned_without_new_branch(self):
        """If open touchstone/* spec PR exists, return its URL — no create_branch call."""
        ex = _make_executor()
        diff = _diff("FIELD_EXTRA")

        existing_prs = [
            {
                "head": {"ref": "touchstone/spec-fix-abc123"},
                "title": "fix(contract): document undocumented field(s): sku",
                "html_url": "https://github.com/owner/repo/pull/99",
            }
        ]

        with patch("adapters.openapi.executor._gh_request") as mock_gh:
            mock_gh.return_value = existing_prs
            result = ex.execute(_stub_resolution(diff), diff)

        # Only one GH call — the list PRs for idempotency check
        assert mock_gh.call_count == 1
        assert result.success is True
        assert result.file_path_ref == "https://github.com/owner/repo/pull/99"

    def test_existing_api_pr_returned_without_new_branch(self):
        """If open touchstone/* api PR exists, return its URL."""
        ex = _make_executor()
        diff = _diff("FIELD_MISSING")

        existing_prs = [
            {
                "head": {"ref": "touchstone/api-fix-abc123"},
                "title": "fix(contract): return missing field(s): weight",
                "html_url": "https://github.com/owner/repo/pull/88",
            }
        ]

        with patch("adapters.openapi.executor._gh_request") as mock_gh:
            mock_gh.return_value = existing_prs
            result = ex.execute(_stub_resolution(diff), diff)

        assert mock_gh.call_count == 1
        assert result.success is True
        assert result.file_path_ref == "https://github.com/owner/repo/pull/88"

    def test_non_touchstone_pr_does_not_match(self):
        """PR on a non-touchstone branch is ignored — a new PR is created."""
        ex = _make_executor()
        diff = _diff("FIELD_EXTRA")

        non_touchstone_prs = [
            {
                "head": {"ref": "feature/my-branch"},
                "title": "fix(contract): document undocumented field(s): sku",
                "html_url": "https://github.com/owner/repo/pull/50",
            }
        ]

        fake_pr = {"html_url": "https://github.com/owner/repo/pull/51"}
        fake_sha = {"sha": "abc"}
        fake_ref = {"object": {"sha": "deadbeef"}}

        with (
            patch("adapters.openapi.executor._gh_request") as mock_gh,
            patch.object(
                ContractExecutor, "_llm_patch_and_validate", return_value=("patched\n", True)
            ),
        ):
            mock_gh.side_effect = [
                non_touchstone_prs,  # find_existing_pr — no match
                fake_sha,  # _get_file_sha
                fake_ref,  # _head_sha
                {},  # _create_branch
                {},  # _commit_file
                fake_pr,  # _open_pr
            ]
            result = ex.execute(_stub_resolution(diff), diff)

        assert result.file_path_ref == "https://github.com/owner/repo/pull/51"


# ---------------------------------------------------------------------------
# Demo reset — close_touchstone_prs
# ---------------------------------------------------------------------------


class TestDemoReset:

    def test_closes_all_touchstone_prs(self):
        """close_touchstone_prs() patches state=closed on each touchstone/* PR."""
        ex = _make_executor()
        open_prs = [
            {"number": 1, "head": {"ref": "touchstone/spec-fix-aaa"}},
            {"number": 2, "head": {"ref": "touchstone/api-fix-bbb"}},
            {"number": 3, "head": {"ref": "feature/unrelated"}},
        ]

        with patch("adapters.openapi.executor._gh_request") as mock_gh:
            mock_gh.return_value = open_prs
            # First call returns list; subsequent PATCH calls return {}
            mock_gh.side_effect = [open_prs, {}, {}]
            closed = ex.close_touchstone_prs()

        assert closed == 2  # only the 2 touchstone/* PRs
        # Verify PATCH was called for PR 1 and 2 but not 3
        patch_calls = [
            c
            for c in mock_gh.call_args_list
            if c.kwargs.get("method") == "PATCH" or (len(c.args) > 1 and c.args[1] == "PATCH")
        ]
        assert len(patch_calls) == 2

    def test_no_token_returns_zero(self, monkeypatch):
        """close_touchstone_prs() with no token returns 0 without calling GH."""
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        ex = ContractExecutor(repo="owner/repo", token="")
        with patch("adapters.openapi.executor._gh_request") as mock_gh:
            closed = ex.close_touchstone_prs()
        mock_gh.assert_not_called()
        assert closed == 0

    def test_github_error_returns_zero_not_exception(self):
        """GH API failure during reset is swallowed — returns 0."""
        ex = _make_executor()
        with patch(
            "adapters.openapi.executor._gh_request",
            side_effect=Exception("network down"),
        ):
            closed = ex.close_touchstone_prs()
        assert closed == 0
