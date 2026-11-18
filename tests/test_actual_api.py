"""
Tests for adapters/openapi/actual.py, Req 10.6 (unreachable target).

A path that times out, fails to connect, answers 5xx or redirects was not
observed and must not become a PATH_MISSING ghost. A 4xx answer is a real
observation and still counts as missing. If nothing can be observed, probe()
raises TargetUnreachable so no ghosts are reported at all.

No network: _fetch_json, _warmup and the SSRF check are replaced.
"""

from __future__ import annotations

import httpx
import pytest

import adapters.openapi.actual as actual
from adapters.openapi.actual import TargetUnreachable, probe
from adapters.openapi.comparator import ContractComparator
from core.models import State

SPEC_PATHS = {
    "/users": {"get": {"responses": {"200": {"schema": {"type": "array"}}}}},
    "/products": {"get": {"responses": {"200": {"schema": {"type": "array"}}}}},
}


@pytest.fixture
def offline(monkeypatch):
    """Disable DNS/SSRF and warm-up; return a setter for per-path responses."""
    monkeypatch.setattr(actual, "ssrf_validate", lambda url: url)
    monkeypatch.setattr(actual, "_warmup", lambda base_url: None)
    answers: dict[str, tuple[int, object]] = {}

    def fake_fetch(url: str):
        path = "/" + url.split("/", 3)[3]
        result = answers[path]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(actual, "_fetch_json", fake_fetch)
    return answers


def _missing(state: State) -> list[str]:
    intended = State(domain="api", data={"paths": SPEC_PATHS})
    diffs = ContractComparator().compare(intended, state)
    return [d.target for d in diffs if d.kind == "PATH_MISSING"]


def test_all_paths_unreachable_raises(offline):
    offline["/users"] = (0, None)
    offline["/products"] = (0, None)
    with pytest.raises(TargetUnreachable):
        probe("https://api.example.com", SPEC_PATHS)


def test_all_paths_5xx_raises(offline):
    offline["/users"] = (502, None)
    offline["/products"] = (503, None)
    with pytest.raises(TargetUnreachable):
        probe("https://api.example.com", SPEC_PATHS)


def test_fetch_exception_counts_as_unreachable(offline):
    offline["/users"] = RuntimeError("boom")
    offline["/products"] = (200, [{"id": 1}])
    state = probe("https://api.example.com", SPEC_PATHS)
    assert state.data["not_observed"]["/users"] == {
        "status": 0,
        "reason": "UNREACHABLE",
    }
    assert _missing(state) == []


def test_partly_unreachable_is_not_a_lost_endpoint(offline):
    offline["/users"] = (0, None)
    offline["/products"] = (200, [{"id": 1, "name": "Widget"}])
    state = probe("https://api.example.com", SPEC_PATHS)
    assert "/products" in state.data["paths"]
    assert state.data["not_observed"]["/users"]["reason"] == "UNREACHABLE"
    assert _missing(state) == []


def test_5xx_is_not_a_lost_endpoint(offline):
    offline["/users"] = (500, None)
    offline["/products"] = (200, [{"id": 1}])
    state = probe("https://api.example.com", SPEC_PATHS)
    assert state.data["not_observed"]["/users"] == {
        "status": 500,
        "reason": "UNREACHABLE",
    }
    assert _missing(state) == []


def test_redirect_is_skipped_not_missing(offline):
    offline["/users"] = (302, None)
    offline["/products"] = (200, [{"id": 1}])
    state = probe("https://api.example.com", SPEC_PATHS)
    assert state.data["not_observed"]["/users"]["reason"] == "REDIRECT"
    assert _missing(state) == []


def test_404_is_still_a_lost_endpoint(offline):
    offline["/users"] = (404, None)
    offline["/products"] = (200, [{"id": 1}])
    state = probe("https://api.example.com", SPEC_PATHS)
    assert "/users" not in state.data["not_observed"]
    assert _missing(state) == ["/users"]


def test_all_404_does_not_raise(offline):
    offline["/users"] = (404, None)
    offline["/products"] = (404, None)
    state = probe("https://api.example.com", SPEC_PATHS)
    assert sorted(_missing(state)) == ["/products", "/users"]


def test_probe_request_uses_10s_timeout(monkeypatch):
    monkeypatch.setattr(actual, "ssrf_validate", lambda url: url)
    seen: dict[str, object] = {}

    def fake_get(url, headers=None, timeout=None):
        seen["timeout"] = timeout
        return httpx.Response(200, json=[{"id": 1}], request=httpx.Request("GET", url))

    monkeypatch.setattr(actual._CLIENT, "get", fake_get)
    status, _ = actual._fetch_json("https://api.example.com/users")
    assert status == 200
    assert seen["timeout"] == actual.PROBE_TIMEOUT_SECS == 10.0


def test_timeout_maps_to_status_zero(monkeypatch):
    monkeypatch.setattr(actual, "ssrf_validate", lambda url: url)

    def fake_get(url, headers=None, timeout=None):
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(actual._CLIENT, "get", fake_get)
    assert actual._fetch_json("https://api.example.com/users") == (0, None)
