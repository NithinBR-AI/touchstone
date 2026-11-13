"""
test_comparator_cache.py

Unit tests for CacheComparator. Uses in-memory State fixtures only.
No Redis or network required.
"""

from __future__ import annotations

from adapters.redis.comparator import CacheComparator
from core.models import GhostState, State


def _intended(keys: dict) -> State:
    return State(domain="cache", data={"keys": keys})


def _actual(keys: list[str]) -> State:
    return State(domain="cache", data={"keys": keys})


def _by_kind(diffs, kind):
    return [d for d in diffs if d.kind == kind]


# ---------------------------------------------------------------------------
# No drift
# ---------------------------------------------------------------------------


def test_no_drift_when_states_match() -> None:
    intended = _intended(
        {
            "users:list": {"description": "Cached list of all users"},
            "orders:list": {"description": "Cached list of all orders"},
        }
    )
    actual = _actual(["users:list", "orders:list"])
    assert CacheComparator().compare(intended, actual) == []


# ---------------------------------------------------------------------------
# KEY_MISSING
# ---------------------------------------------------------------------------


def test_key_missing_detected() -> None:
    intended = _intended({"products:list": {"description": "Cached list of all products"}})
    actual = _actual([])
    diffs = CacheComparator().compare(intended, actual)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "KEY_MISSING"
    assert d.target == "products:list"
    assert d.severity == "MEDIUM"
    assert d.state == GhostState.PENDING
    assert d.context["safe_fix"] is True


def test_key_missing_carries_description() -> None:
    intended = _intended({"users:list": {"description": "All users"}})
    actual = _actual([])
    diffs = CacheComparator().compare(intended, actual)
    assert diffs[0].context["description"] == "All users"


def test_multiple_missing_keys() -> None:
    intended = _intended(
        {
            "users:list": {"description": ""},
            "orders:list": {"description": ""},
            "products:list": {"description": ""},
        }
    )
    actual = _actual([])
    diffs = CacheComparator().compare(intended, actual)
    assert len(diffs) == 3
    assert all(d.kind == "KEY_MISSING" for d in diffs)


# ---------------------------------------------------------------------------
# UNDECLARED_KEY
# ---------------------------------------------------------------------------


def test_undeclared_key_detected() -> None:
    intended = _intended({})
    actual = _actual(["debug:session_dump"])
    diffs = CacheComparator().compare(intended, actual)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "UNDECLARED_KEY"
    assert d.target == "debug:session_dump"
    assert d.severity == "HIGH"
    assert d.state == GhostState.BLOCKED
    assert d.context["safe_fix"] is False


def test_multiple_undeclared_keys() -> None:
    intended = _intended({})
    actual = _actual(["debug:session_dump", "tmp:leaked_token"])
    diffs = CacheComparator().compare(intended, actual)
    assert len(diffs) == 2
    assert all(d.kind == "UNDECLARED_KEY" for d in diffs)


# ---------------------------------------------------------------------------
# Mixed drift
# ---------------------------------------------------------------------------


def test_mixed_drift() -> None:
    intended = _intended(
        {
            "users:list": {"description": ""},
            "products:list": {"description": ""},
        }
    )
    actual = _actual(["users:list", "debug:session_dump"])
    diffs = CacheComparator().compare(intended, actual)

    missing = _by_kind(diffs, "KEY_MISSING")
    undeclared = _by_kind(diffs, "UNDECLARED_KEY")

    assert len(missing) == 1
    assert missing[0].target == "products:list"
    assert len(undeclared) == 1
    assert undeclared[0].target == "debug:session_dump"


# ---------------------------------------------------------------------------
# Signature stability
# ---------------------------------------------------------------------------


def test_signature_is_stable_across_runs() -> None:
    intended = _intended({"products:list": {"description": ""}})
    actual = _actual([])
    d1 = CacheComparator().compare(intended, actual)[0]
    d2 = CacheComparator().compare(intended, actual)[0]
    assert d1.signature == d2.signature


def test_different_keys_have_different_signatures() -> None:
    intended = _intended(
        {
            "users:list": {"description": ""},
            "products:list": {"description": ""},
        }
    )
    actual = _actual([])
    diffs = CacheComparator().compare(intended, actual)
    assert diffs[0].signature != diffs[1].signature


# ---------------------------------------------------------------------------
# Verifier signature regression (issue #2)
# ---------------------------------------------------------------------------


def test_verifier_accepts_comparison_target() -> None:
    """CacheVerifier.verify() must accept (ComparisonTarget, Difference) — not (diff, result)."""
    from unittest.mock import MagicMock, patch

    from adapters.redis.verifier import CacheVerifier
    from core.models import ComparisonTarget, GhostState

    verifier = CacheVerifier(cache_status_url="http://example.com/cache/status")

    diff = _by_kind(
        CacheComparator().compare(_intended({"users:list": {"description": ""}}), _actual([])),
        "KEY_MISSING",
    )[0]

    target = ComparisonTarget(domain="cache", target=diff.target)

    mock_state = MagicMock()
    mock_state.data = {"keys": ["users:list"]}  # key is now present — resolved

    with patch("adapters.redis.verifier.load_actual", return_value=mock_state):
        result = verifier.verify(target, diff)

    assert result.resolved is True
    assert result.evidence["key"] == "users:list"


def test_verifier_reports_still_present_when_key_absent() -> None:
    """Verifier returns resolved=False when key still missing after warm."""
    from unittest.mock import MagicMock, patch

    from adapters.redis.verifier import CacheVerifier
    from core.models import ComparisonTarget

    verifier = CacheVerifier(cache_status_url="http://example.com/cache/status")

    diff = _by_kind(
        CacheComparator().compare(_intended({"products:list": {"description": ""}}), _actual([])),
        "KEY_MISSING",
    )[0]

    target = ComparisonTarget(domain="cache", target=diff.target)

    mock_state = MagicMock()
    mock_state.data = {"keys": []}  # still empty

    with patch("adapters.redis.verifier.load_actual", return_value=mock_state):
        result = verifier.verify(target, diff)

    assert result.resolved is False


# ---------------------------------------------------------------------------
# Executor uses diff.target as key (issue #3 regression)
# ---------------------------------------------------------------------------


def test_executor_uses_diff_target_as_key() -> None:
    """CacheExecutor must POST to warm URL with key=diff.target."""
    from unittest.mock import MagicMock, patch

    from adapters.redis.executor import CacheExecutor
    from core.models import Resolution

    diff = _by_kind(
        CacheComparator().compare(_intended({"orders:list": {"description": ""}}), _actual([])),
        "KEY_MISSING",
    )[0]

    executor = CacheExecutor(cache_warm_url="http://example.com/cache/warm")
    stub_resolution = Resolution(
        difference_id=diff.id, difference_signature=diff.signature, proposed_change=None
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    with patch("adapters.redis.executor._CLIENT") as mock_client:
        mock_client.post.return_value = mock_resp
        result = executor.execute(stub_resolution, diff)

    assert result.success is True
    call_url = mock_client.post.call_args[0][0]
    assert "orders%3Alist" in call_url or "orders:list" in call_url


def test_executor_deletes_undeclared_key() -> None:
    """CacheExecutor must call DELETE for UNDECLARED_KEY (not POST warm)."""
    from unittest.mock import MagicMock, patch

    from adapters.redis.executor import CacheExecutor
    from core.models import Resolution

    diff = _by_kind(
        CacheComparator().compare(_intended({}), _actual(["debug:dump"])),
        "UNDECLARED_KEY",
    )[0]

    executor = CacheExecutor(cache_warm_url="http://example.com/cache/warm")
    stub_resolution = Resolution(
        difference_id=diff.id, difference_signature=diff.signature, proposed_change=None
    )

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None

    with patch("adapters.redis.executor._CLIENT") as mock_client:
        mock_client.delete.return_value = mock_resp
        result = executor.execute(stub_resolution, diff)
        mock_client.post.assert_not_called()
        mock_client.delete.assert_called_once()

    assert result.success is True
    call_url = mock_client.delete.call_args[0][0]
    assert "debug" in call_url
