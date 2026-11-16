"""
test_remote_source.py — Req 5, tech.md outbound HTTP rules

adapters/remote_source.py downloads models.py, which Schema Drift then executes.
These tests check the download follows the same rules as every other outbound
request: SSRF check first, no redirects followed, no proxy env vars, and a
failed download leaves nothing behind.

HTTP is served by httpx.MockTransport — no network.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from adapters import remote_source
from adapters.ssrf import SSRFError

URL = "https://raw.githubusercontent.com/demo/repo/main/models.py"
MODELS = b"class User(Base):\n    __tablename__ = 'users'\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Empty cache before and after; delete temp files the tests create."""
    remote_source.invalidate()
    monkeypatch.setattr("adapters.remote_source.ssrf_validate", lambda url: url)
    yield
    for path in list(remote_source._cache.values()):
        path.unlink(missing_ok=True)
    remote_source.invalidate()


def _serve(monkeypatch, handler) -> list[str]:
    """Route the module's HTTP client to *handler*; return the list of requested URLs."""
    requests: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        "adapters.remote_source._make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(recording), follow_redirects=False),
    )
    return requests


# ---------------------------------------------------------------------------
# Download and cache
# ---------------------------------------------------------------------------


def test_downloads_content_to_a_temp_file(monkeypatch):
    _serve(monkeypatch, lambda req: httpx.Response(200, content=MODELS))
    path = remote_source.fetch(URL)
    assert path.suffix == ".py"
    assert path.read_bytes() == MODELS


def test_second_fetch_is_served_from_cache(monkeypatch):
    requests = _serve(monkeypatch, lambda req: httpx.Response(200, content=MODELS))
    first = remote_source.fetch(URL)
    second = remote_source.fetch(URL)
    assert first == second
    assert len(requests) == 1


def test_invalidate_forces_a_fresh_download(monkeypatch):
    requests = _serve(monkeypatch, lambda req: httpx.Response(200, content=MODELS))
    old = remote_source.fetch(URL)
    remote_source.invalidate(URL)
    new = remote_source.fetch(URL)
    old.unlink(missing_ok=True)
    assert new != old
    assert len(requests) == 2


# ---------------------------------------------------------------------------
# Outbound HTTP rules (tech.md)
# ---------------------------------------------------------------------------


def test_blocked_destination_refused_before_any_request(monkeypatch):
    """Uses the real SSRF guard: 127.0.0.1 is loopback."""
    from adapters.ssrf import validate

    monkeypatch.setattr("adapters.remote_source.ssrf_validate", validate)
    requests = _serve(monkeypatch, lambda req: httpx.Response(200, content=MODELS))
    with pytest.raises(SSRFError):
        remote_source.fetch("http://127.0.0.1/models.py")
    assert requests == []
    assert remote_source._cache == {}


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_not_followed(monkeypatch, status):
    requests = _serve(
        monkeypatch,
        lambda req: httpx.Response(status, headers={"location": "https://evil.example/models.py"}),
    )
    with pytest.raises(httpx.HTTPStatusError):
        remote_source.fetch(URL)
    assert requests == [URL]  # the redirect target was never requested
    assert remote_source._cache == {}


@pytest.mark.parametrize("status", [404, 500])
def test_error_status_raises_and_caches_nothing(monkeypatch, status):
    _serve(monkeypatch, lambda req: httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        remote_source.fetch(URL)
    assert remote_source._cache == {}


def test_failed_download_leaves_no_temp_file(monkeypatch, tmp_path):
    """The temp file is written only after a successful response."""
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    _serve(monkeypatch, lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        remote_source.fetch(URL)
    assert list(Path(tmp_path).iterdir()) == []


def test_http_client_disables_proxies_and_redirects(monkeypatch):
    """trust_env=False, follow_redirects=False, OS trust store, short timeout."""
    seen = {}
    monkeypatch.setattr("adapters.remote_source.httpx.Client", lambda **kw: seen.update(kw))
    remote_source._make_client()
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
    assert seen["timeout"] == remote_source.TIMEOUT_S
    assert isinstance(seen["verify"], ssl.SSLContext)
