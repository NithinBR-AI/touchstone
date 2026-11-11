"""
tests/test_ssrf.py — Req 42

SSRF guard tests. All cases validated without network calls.
"""

import pytest

from adapters.openapi.ssrf import SSRFError, validate


# ----- valid URLs --------------------------------------------------------

def test_https_public_url_allowed():
    # github.com is a public host — should not raise
    validate("https://raw.githubusercontent.com/owner/repo/main/openapi.yaml")


def test_http_allowed_for_scheme():
    # scheme check: http is allowed (ssrf guard checks IP, not scheme alone)
    # local resolution would catch loopback; for external hosts it passes
    validate("http://example.com/spec.yaml")


# ----- bad schemes -------------------------------------------------------

def test_ftp_scheme_blocked():
    with pytest.raises(SSRFError, match="scheme"):
        validate("ftp://example.com/file")


def test_file_scheme_blocked():
    with pytest.raises(SSRFError, match="scheme"):
        validate("file:///etc/passwd")


# ----- metadata endpoints ------------------------------------------------

def test_aws_metadata_blocked():
    with pytest.raises(SSRFError, match="blocked"):
        validate("http://169.254.169.254/latest/meta-data/")


def test_gcp_metadata_blocked():
    with pytest.raises(SSRFError, match="blocked"):
        validate("http://metadata.google.internal/computeMetadata/v1/")


# ----- loopback / private ------------------------------------------------

def test_localhost_blocked():
    with pytest.raises(SSRFError):
        validate("http://localhost/secret")


def test_127_0_0_1_blocked():
    with pytest.raises(SSRFError):
        validate("http://127.0.0.1/secret")


def test_private_10_range_blocked():
    with pytest.raises(SSRFError):
        validate("http://10.0.0.1/internal")


def test_private_192_168_range_blocked():
    with pytest.raises(SSRFError):
        validate("http://192.168.1.1/router")


def test_private_172_16_range_blocked():
    with pytest.raises(SSRFError):
        validate("http://172.16.0.1/api")


# ----- no host -----------------------------------------------------------

def test_missing_host_blocked():
    with pytest.raises(SSRFError, match="no host"):
        validate("https:///path")


# ----- returns url on success --------------------------------------------

def test_returns_url_unchanged():
    url = "https://raw.githubusercontent.com/owner/repo/main/openapi.yaml"
    assert validate(url) == url
