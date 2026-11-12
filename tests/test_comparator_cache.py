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
    intended = _intended(
        {"products:list": {"description": "Cached list of all products"}}
    )
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
