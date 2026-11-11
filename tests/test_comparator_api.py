"""
tests/test_comparator_api.py — Req 11

ContractComparator unit tests. No network calls — all States built directly.
"""

from core.models import GhostState, State
from adapters.openapi.comparator import ContractComparator, DOMAIN


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
    intended = _state({
        "/users": {"get": {"operationId": "listUsers", "responses": {
            "200": {"schema": _list_schema({"id": {"type": "integer"}, "email": {"type": "string"}})}
        }}}
    })
    actual = _state({
        "/users": {"get": {"responses": {
            "200": {"schema": _list_schema({
                "id": {"type": "integer"}, "email": {"type": "string"},
                "account_status": {"type": "string"}  # extra
            })}
        }}}
    })
    diffs = ContractComparator().compare(intended, actual)
    assert len(diffs) == 1
    d = diffs[0]
    assert d.kind == "FIELD_EXTRA"
    assert d.domain == DOMAIN
    assert "account_status" in d.target
    assert d.state == GhostState.BLOCKED
    assert d.context["safe_fix"] is False


def test_field_extra_multiple():
    intended = _state({
        "/products": {"get": {"responses": {
            "200": {"schema": _list_schema({"id": {"type": "integer"}, "name": {"type": "string"}})}
        }}}
    })
    actual = _state({
        "/products": {"get": {"responses": {
            "200": {"schema": _list_schema({
                "id": {"type": "integer"}, "name": {"type": "string"},
                "sku": {"type": "string"}, "internal_code": {"type": "string"}
            })}
        }}}
    })
    diffs = ContractComparator().compare(intended, actual)
    kinds = [d.kind for d in diffs]
    assert kinds.count("FIELD_EXTRA") == 2
    fields = {d.context["field"] for d in diffs}
    assert "sku" in fields
    assert "internal_code" in fields


# ----- FIELD_MISSING -----------------------------------------------------

def test_field_missing_detected():
    intended = _state({
        "/users": {"get": {"responses": {
            "200": {"schema": _user_schema()}
        }}}
    })
    # actual returns only id, not email
    actual = _state({
        "/users": {"get": {"responses": {
            "200": {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}}
        }}
    })
    diffs = ContractComparator().compare(intended, actual)
    missing = [d for d in diffs if d.kind == "FIELD_MISSING"]
    assert len(missing) == 1
    assert missing[0].context["field"] == "email"
    assert missing[0].state == GhostState.REVIEW_REQUIRED


# ----- PATH_MISSING / PATH_EXTRA -----------------------------------------

def test_path_missing_detected():
    intended = _state({
        "/users": {"get": {"responses": {"200": {"schema": {}}}}},
        "/orders": {"get": {"responses": {"200": {"schema": {}}}}},
    })
    actual = _state({
        "/users": {"get": {"responses": {"200": {"schema": {}}}}}
    })
    diffs = ContractComparator().compare(intended, actual)
    path_missing = [d for d in diffs if d.kind == "PATH_MISSING"]
    assert len(path_missing) == 1
    assert path_missing[0].target == "/orders"


def test_path_extra_detected():
    intended = _state({
        "/users": {"get": {"responses": {"200": {"schema": {}}}}}
    })
    actual = _state({
        "/users": {"get": {"responses": {"200": {"schema": {}}}}},
        "/admin": {"get": {"responses": {"200": {"schema": {}}}}},
    })
    diffs = ContractComparator().compare(intended, actual)
    extra = [d for d in diffs if d.kind == "PATH_EXTRA"]
    assert len(extra) == 1
    assert extra[0].target == "/admin"


# ----- clean (no drift) --------------------------------------------------

def test_no_drift_returns_empty():
    schema = _list_schema({"id": {"type": "integer"}, "email": {"type": "string"}})
    intended = _state({
        "/users": {"get": {"responses": {"200": {"schema": schema}}}}
    })
    actual = _state({
        "/users": {"get": {"responses": {"200": {"schema": schema}}}}
    })
    diffs = ContractComparator().compare(intended, actual)
    assert diffs == []


# ----- signature stability -----------------------------------------------

def test_signatures_are_stable():
    intended = _state({
        "/users": {"get": {"responses": {
            "200": {"schema": _list_schema({"id": {"type": "integer"}})}
        }}}
    })
    actual = _state({
        "/users": {"get": {"responses": {
            "200": {"schema": _list_schema({
                "id": {"type": "integer"}, "account_status": {"type": "string"}
            })}
        }}}
    })
    diffs1 = ContractComparator().compare(intended, actual)
    diffs2 = ContractComparator().compare(intended, actual)
    assert diffs1[0].signature == diffs2[0].signature


def test_safe_fix_always_false():
    intended = _state({
        "/x": {"get": {"responses": {"200": {"schema": {"type": "object", "properties": {}}}}}}
    })
    actual = _state({
        "/x": {"get": {"responses": {"200": {"schema": {"type": "object", "properties": {
            "leaked": {"type": "string"}
        }}}}}}
    })
    diffs = ContractComparator().compare(intended, actual)
    assert all(d.context["safe_fix"] is False for d in diffs)
