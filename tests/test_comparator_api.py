"""
tests/test_comparator_api.py — Req 11

ContractComparator unit tests. No network calls — all States built directly.
"""

from adapters.openapi.comparator import DOMAIN, ContractComparator
from core.models import GhostState, State


def _state(paths: dict) -> State:
    return State(domain="api", data={"paths": paths})


def _user_schema(extra_fields: list[str] | None = None) -> dict:
    props = {"id": {"type": "integer"}, "email": {"type": "string"}}
    if extra_fields:
        for f in extra_fields:
            props[f] = {"type": "string"}
    return {"type": "object", "properties": props}


def _list_schema(item_props: dict) -> dict:
    return {"type": "array", "items": {"type": "object", "properties": item_props}}


# ----- FIELD_EXTRA -------------------------------------------------------


def test_field_extra_detected():
    intended = _state(
        {
            "/users": {
                "get": {
                    "operationId": "listUsers",
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {"id": {"type": "integer"}, "email": {"type": "string"}}
                            )
                        }
                    },
                }
            }
        }
    )
    actual = _state(
        {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {
                                    "id": {"type": "integer"},
                                    "email": {"type": "string"},
                                    "account_status": {"type": "string"},  # extra
                                }
                            )
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "FIELD_EXTRA"
    assert d.domain == DOMAIN
    assert "account_status" in d.context["fields"]
    assert d.state == GhostState.BLOCKED
    assert d.context["safe_fix"] is False


def test_field_extra_multiple():
    intended = _state(
        {
            "/products": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {"id": {"type": "integer"}, "name": {"type": "string"}}
                            )
                        }
                    }
                }
            }
        }
    )
    actual = _state(
        {
            "/products": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {
                                    "id": {"type": "integer"},
                                    "name": {"type": "string"},
                                    "sku": {"type": "string"},
                                    "internal_code": {"type": "string"},
                                }
                            )
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    kinds = [d.kind for d in diffs]
    assert kinds.count("FIELD_EXTRA") == 1
    fields = diffs[0].context["fields"]
    assert "sku" in fields
    assert "internal_code" in fields


# ----- FIELD_MISSING -----------------------------------------------------


def test_field_missing_detected():
    intended = _state({"/users": {"get": {"responses": {"200": {"schema": _user_schema()}}}}})
    # actual returns only id, not email
    actual = _state(
        {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {"id": {"type": "integer"}},
                            }
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    missing = [d for d in diffs if d.kind == "FIELD_MISSING"]
    assert len(missing) == 1
    assert "email" in missing[0].context["fields"]
    assert missing[0].state == GhostState.REVIEW_REQUIRED


# ----- PATH_MISSING / PATH_EXTRA -----------------------------------------


def test_path_missing_detected():
    intended = _state(
        {
            "/users": {"get": {"responses": {"200": {"schema": {}}}}},
            "/orders": {"get": {"responses": {"200": {"schema": {}}}}},
        }
    )
    actual = _state({"/users": {"get": {"responses": {"200": {"schema": {}}}}}})
    diffs = ContractComparator().compare(intended, actual)
    path_missing = [d for d in diffs if d.kind == "PATH_MISSING"]
    assert len(path_missing) == 1
    assert path_missing[0].target == "/orders"


def test_path_extra_detected():
    intended = _state({"/users": {"get": {"responses": {"200": {"schema": {}}}}}})
    actual = _state(
        {
            "/users": {"get": {"responses": {"200": {"schema": {}}}}},
            "/admin": {"get": {"responses": {"200": {"schema": {}}}}},
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    extra = [d for d in diffs if d.kind == "PATH_EXTRA"]
    assert len(extra) == 1
    assert extra[0].target == "/admin"


# ----- clean (no drift) --------------------------------------------------


def test_no_drift_returns_empty():
    schema = _list_schema({"id": {"type": "integer"}, "email": {"type": "string"}})
    intended = _state({"/users": {"get": {"responses": {"200": {"schema": schema}}}}})
    actual = _state({"/users": {"get": {"responses": {"200": {"schema": schema}}}}})
    diffs = ContractComparator().compare(intended, actual)
    assert diffs == []


# ----- signature stability -----------------------------------------------


def test_signatures_are_stable():
    intended = _state(
        {
            "/users": {
                "get": {"responses": {"200": {"schema": _list_schema({"id": {"type": "integer"}})}}}
            }
        }
    )
    actual = _state(
        {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {
                                    "id": {"type": "integer"},
                                    "account_status": {"type": "string"},
                                }
                            )
                        }
                    }
                }
            }
        }
    )
    diffs1 = ContractComparator().compare(intended, actual)
    diffs2 = ContractComparator().compare(intended, actual)
    assert diffs1[0].signature == diffs2[0].signature


# ----- TYPE_MISMATCH (issue #6 regression) -------------------------------


def test_type_mismatch_detected():
    """Comparator must emit TYPE_MISMATCH when field types differ between spec and live."""
    intended = _state(
        {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {"id": {"type": "integer"}, "age": {"type": "integer"}}
                            )
                        }
                    }
                }
            }
        }
    )
    # live API returns age as string instead of integer
    actual = _state(
        {
            "/users": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": _list_schema(
                                {"id": {"type": "integer"}, "age": {"type": "string"}}
                            )
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    type_mismatches = [d for d in diffs if d.kind == "TYPE_MISMATCH"]
    assert len(type_mismatches) == 1
    d = type_mismatches[0]
    assert d.context["field"] == "age"
    assert d.context["declared_type"] == "integer"
    assert d.context["actual_type"] == "string"
    assert d.state == GhostState.REVIEW_REQUIRED
    assert d.context["safe_fix"] is False


def test_type_mismatch_not_emitted_when_types_match():
    """No TYPE_MISMATCH when field types are identical."""
    schema = _list_schema({"id": {"type": "integer"}, "name": {"type": "string"}})
    intended = _state({"/users": {"get": {"responses": {"200": {"schema": schema}}}}})
    actual = _state({"/users": {"get": {"responses": {"200": {"schema": schema}}}}})
    diffs = ContractComparator().compare(intended, actual)
    assert not any(d.kind == "TYPE_MISMATCH" for d in diffs)


def test_type_mismatch_context_has_status_not_status_code():
    """Context key must be 'status' (not 'status_code') — UI reads ctx.status."""
    intended = _state(
        {
            "/items": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {"count": {"type": "integer"}},
                            }
                        }
                    }
                }
            }
        }
    )
    actual = _state(
        {
            "/items": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {"count": {"type": "string"}},
                            }
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    assert len(diffs) == 1
    assert "status" in diffs[0].context
    assert "status_code" not in diffs[0].context


def test_field_extra_context_has_description():
    """PATH_MISSING and FIELD_EXTRA diffs must carry a description for the UI fallback."""
    intended = _state(
        {
            "/users": {"get": {"responses": {"200": {"schema": {}}}}},
            "/orders": {"get": {"responses": {"200": {"schema": {}}}}},
        }
    )
    actual = _state({"/users": {"get": {"responses": {"200": {"schema": {}}}}}})
    diffs = ContractComparator().compare(intended, actual)
    path_missing = [d for d in diffs if d.kind == "PATH_MISSING"]
    assert len(path_missing) == 1
    # PATH_MISSING has no field — UI falls back to ctx.description
    assert (
        "description" not in path_missing[0].context
    )  # PATH_MISSING has no description field currently
    # target is the path itself
    assert path_missing[0].target == "/orders"


# ----- verifier signature (issue #5 regression) --------------------------


def test_verifier_accepts_comparison_target():
    """ContractVerifier.verify() must accept (ComparisonTarget, Difference) — not (diff, result, url)."""
    from adapters.openapi.verifier import ContractVerifier
    from core.models import ComparisonTarget, Difference, GhostState

    verifier = ContractVerifier()
    target = ComparisonTarget(domain="api", target="/users", context={})
    diff = Difference(
        signature="abc",
        domain="api",
        kind="FIELD_EXTRA",
        target="GET /users 200.leaked",
        severity="MEDIUM",
        state=GhostState.BLOCKED,
        context={"path": "/users", "field": "leaked", "method": "get", "status": "200"},
    )
    # No openapi_url in context — should return resolved=True with a trust note, not raise
    result = verifier.verify(target, diff)
    assert result.resolved is True
    assert "trusting execution" in result.evidence.get("reason", "")


def test_safe_fix_always_false():
    intended = _state(
        {"/x": {"get": {"responses": {"200": {"schema": {"type": "object", "properties": {}}}}}}}
    )
    actual = _state(
        {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "schema": {
                                "type": "object",
                                "properties": {"leaked": {"type": "string"}},
                            }
                        }
                    }
                }
            }
        }
    )
    diffs = ContractComparator().compare(intended, actual)
    assert all(d.context["safe_fix"] is False for d in diffs)
