"""
ContractValidator binds an API proposal to its difference (review fix).

The proposal is metadata only (file + fields); the patch is written at
Exorcise time. The validator decides which file may change and for which
fields. Also covers the executor's draft-PR outcome: a twice-rejected patch
still opens a draft PR for review but is reported as a failure.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from adapters.openapi.executor import ContractExecutor
from adapters.openapi.validator import ContractValidator
from core.models import ChangeProposal, Difference, GhostState, Resolution


def _diff(kind: str, target: str = "GET /users 200.account_status") -> Difference:
    return Difference(
        id=str(uuid.uuid4()),
        signature=f"sig-{kind}-{target}",
        domain="api",
        kind=kind,
        target=target,
        severity="MEDIUM",
        state=GhostState.BLOCKED,
        context={"path": "/users", "field": "account_status"},
    )


def _proposal(
    diff: Difference,
    *,
    file_name: str | None = None,
    fields: object = ("account_status",),
    target: str | None = None,
    operation: str = "FILE_PATCH",
) -> ChangeProposal:
    default_file = "main.py" if diff.kind == "FIELD_MISSING" else "openapi.yaml"
    return ChangeProposal(
        domain="api",
        target=diff.target if target is None else target,
        operation=operation,
        payload={
            "kind": diff.kind,
            "fields": list(fields) if isinstance(fields, tuple) else fields,
            "file_name": default_file if file_name is None else file_name,
        },
    )


V = ContractValidator()


def test_field_extra_metadata_proposal_is_valid():
    d = _diff("FIELD_EXTRA")
    assert V.validate(_proposal(d), d).status == "valid"


def test_field_missing_needs_human_authorization():
    d = _diff("FIELD_MISSING")
    assert V.validate(_proposal(d), d).status == "review_required"
    assert V.validate(_proposal(d), d, human_authorized=True).status == "valid"


@pytest.mark.parametrize(
    "kind,wrong_file",
    [
        ("FIELD_EXTRA", "main.py"),
        ("FIELD_EXTRA", "../secrets.env"),
        ("FIELD_MISSING", "openapi.yaml"),
        ("FIELD_MISSING", ".github/workflows/deploy.yml"),
    ],
)
def test_proposal_aimed_at_another_file_is_blocked(kind, wrong_file):
    d = _diff(kind)
    result = V.validate(_proposal(d, file_name=wrong_file), d, human_authorized=True)
    assert result.status == "blocked"
    assert result.ghost_state == GhostState.BLOCKED


def test_target_mismatch_is_rejected():
    d = _diff("FIELD_EXTRA")
    result = V.validate(_proposal(d, target="GET /orders 200.total"), d)
    assert result.status == "invalid"


@pytest.mark.parametrize("fields", [[], [""], None, "account_status"])
def test_proposal_without_fields_is_blocked(fields):
    d = _diff("FIELD_EXTRA")
    assert V.validate(_proposal(d, fields=fields), d).status == "blocked"


def test_wrong_operation_is_blocked():
    d = _diff("FIELD_EXTRA")
    assert V.validate(_proposal(d, operation="SQL"), d).status == "blocked"


@pytest.mark.parametrize("kind", ["PATH_MISSING", "TYPE_MISMATCH", "MADE_UP"])
def test_kinds_without_a_file_are_blocked_even_when_authorized(kind):
    d = _diff(kind)
    result = V.validate(_proposal(d), d, human_authorized=True)
    assert result.status == "blocked"


def test_twice_rejected_patch_opens_draft_but_is_a_failure():
    ex = ContractExecutor(repo="owner/repo", token="tok")
    d = _diff("FIELD_EXTRA")
    res = Resolution(
        difference_id=d.id, difference_signature=d.signature, proposed_change=None
    )
    with (
        patch("adapters.openapi.executor._gh_request") as gh,
        patch.object(
            ContractExecutor,
            "_llm_patch_and_validate",
            return_value=("patched: content\n", False),
        ),
    ):
        gh.side_effect = [
            [],  # _find_existing_pr → none
            {"sha": "abc"},  # _get_file_sha
            {"object": {"sha": "head"}},  # _head_sha
            {},  # _create_branch
            {},  # _commit_file
            {"html_url": "https://github.com/owner/repo/pull/7"},  # _open_pr
        ]
        result = ex.execute(res, d)
        open_pr_body = gh.call_args_list[-1]

    assert result.success is False
    assert result.error_code == "PATCH_UNVALIDATED"
    assert result.file_path_ref == "https://github.com/owner/repo/pull/7"
    assert "draft" in str(open_pr_body).lower()
