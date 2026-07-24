from __future__ import annotations

import hashlib

import pytest

from atlassian_skills.core.managed_manifest import ManagedManifestError, parse_managed_manifest
from atlassian_skills.core.site_identity import normalize_site_url, site_fingerprint


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://EXAMPLE.COM:443/confluence/", "https://example.com/confluence"),
        ("http://example.com:8080/a/./b/../c", "http://example.com:8080/a/c"),
        ("https://[2001:0db8::1]:443/wiki", "https://[2001:db8::1]/wiki"),
        ("https://example.com/%7Eteam", "https://example.com/~team"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),
        ("https://example.com:8443/wiki/", "https://example.com:8443/wiki"),
        ("https://example.com/", "https://example.com"),
    ],
)
def test_site_url_normalization_vectors(raw: str, expected: str) -> None:
    assert normalize_site_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://user@example.com/wiki",
        "https://example.com/wiki?x=1",
        "https://example.com/wiki#frag",
        "https://example.com/%ZZ",
        "https://example.com\\wiki",
        "https://example.com/\x01wiki",
        "https://예시.example/wiki",
    ],
)
def test_site_url_rejects_nonportable_or_ambiguous_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_site_url(raw)


def test_site_fingerprint_is_domain_separated_and_portable() -> None:
    normalized = "https://example.com/confluence"
    expected = hashlib.sha256(b"atls-site-v1\0" + normalized.encode()).hexdigest()

    assert site_fingerprint("HTTPS://EXAMPLE.COM:443/confluence/") == f"sha256:{expected}"
    assert site_fingerprint(normalized) == f"sha256:{expected}"


def test_legacy_binding_marker_is_diagnostic_only() -> None:
    markdown = '<!-- atls:binding {"v":1,"id":"bnd_dead"} -->\n\n# Draft\n'

    with pytest.raises(ManagedManifestError) as exc_info:
        parse_managed_manifest(markdown)

    assert exc_info.value.reason == "legacy_binding_marker"
