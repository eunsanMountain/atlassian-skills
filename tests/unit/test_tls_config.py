"""CA bundle handling for corporate TLS interception (GitHub #16).

Two problems this closes. First, `ca_bundle` was passed to httpx as a plain
string, which httpx 0.28 deprecates ("Use verify=ssl.create_default_context(...)")
— and `pyproject` pins `httpx>=0.27` with no upper bound, so the day httpx drops
the string form only corporate users with a `ca_bundle` would break. Second, a
bad bundle surfaced as a raw `ssl.SSLError` from deep inside httpx instead of an
`AtlasError` with an exit code.
"""

from __future__ import annotations

import ssl
import warnings
from pathlib import Path

import pytest

from atlassian_skills.core.errors import ExitCode, ValidationError
from atlassian_skills.core.tls import build_ssl_context, describe_verify_source

# A throwaway self-signed CA is overkill here; any syntactically valid PEM
# certificate is enough to prove the file is loaded rather than rejected.
VALID_PEM = ssl.get_default_verify_paths().cafile


class TestBuildSslContext:
    def test_no_bundle_uses_the_system_trust_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The OS trust store already holds the corporate root CA (that is why the
        browser works on the same machine), so it is the default — the reasoning
        pip followed when it adopted truststore (GitHub #16)."""
        import truststore

        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        assert isinstance(build_ssl_context(None), truststore.SSLContext)
        assert isinstance(build_ssl_context(""), truststore.SSLContext)

    def test_env_cert_file_keeps_trust_env_behaviour(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`True` (not a context) is deliberate here: it keeps httpx's trust_env
        behaviour, which is what honours SSL_CERT_FILE / SSL_CERT_DIR."""
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
        assert build_ssl_context(None) is True

    @pytest.mark.skipif(not VALID_PEM, reason="no system CA bundle to load")
    def test_valid_file_returns_context(self) -> None:
        context = build_ssl_context(VALID_PEM)
        assert isinstance(context, ssl.SSLContext)

    @pytest.mark.skipif(not VALID_PEM, reason="no system CA bundle to load")
    def test_no_deprecation_warning_on_the_httpx_boundary(self) -> None:
        """Scoped assertion rather than a suite-wide `-W error::DeprecationWarning`
        gate: a global gate breaks on any third-party warning and stops being
        about our code."""
        import httpx

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            httpx.Client(verify=build_ssl_context(VALID_PEM)).close()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert not deprecations, [str(w.message) for w in deprecations]

    def test_missing_path_fails_before_httpx(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as exc:
            build_ssl_context(str(tmp_path / "absent.pem"))
        assert (exc.value.context or {}).get("reason") == "invalid_ca_bundle"
        assert exc.value.exit_code == ExitCode.VALIDATION

    def test_non_pem_file_is_normalized(self, tmp_path: Path) -> None:
        """A DER export or a stray text file raises ssl.SSLError deep in OpenSSL;
        it must reach the user as an AtlasError with a hint, not a traceback."""
        bad = tmp_path / "bad.pem"
        bad.write_text("this is not a certificate\n")
        with pytest.raises(ValidationError) as exc:
            build_ssl_context(str(bad))
        assert (exc.value.context or {}).get("reason") == "invalid_ca_bundle"
        assert exc.value.hint is not None and "certutil -encode" in exc.value.hint

    def test_empty_file_is_normalized(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.pem"
        empty.write_text("")
        with pytest.raises(ValidationError):
            build_ssl_context(str(empty))

    def test_directory_is_accepted_as_capath(self, tmp_path: Path) -> None:
        """OpenSSL does not validate capath at construction time. Nothing can be
        checked here, so the contract is "accepted" — `doctor` carries the warning."""
        assert isinstance(build_ssl_context(str(tmp_path)), ssl.SSLContext)


class TestDescribeVerifySource:
    def test_default_is_the_system_trust_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        source, warning = describe_verify_source(None)
        assert "system trust store" in source
        assert warning is None

    @pytest.mark.skipif(not VALID_PEM, reason="no system CA bundle to load")
    def test_valid_ssl_cert_file_is_reported_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSL_CERT_FILE", str(VALID_PEM))
        source, warning = describe_verify_source(None)
        assert str(VALID_PEM) in source
        assert warning is None

    def test_missing_ssl_cert_file_warns(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """0.3.1's answer to this was `Unexpected internal error` on the first
        request; doctor now names the problem before anything has to fail."""
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent.pem"))
        _source, warning = describe_verify_source(None)
        assert warning is not None and "does not exist" in warning

    def test_unloadable_ssl_cert_file_warns(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        bad = tmp_path / "bad.pem"
        bad.write_text("not a certificate\n")
        monkeypatch.setenv("SSL_CERT_FILE", str(bad))
        _source, warning = describe_verify_source(None)
        assert warning is not None and "could not be loaded" in warning

    def test_ssl_cert_dir_warns_about_hashed_layout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("SSL_CERT_DIR", "/etc/corp/certs")
        source, warning = describe_verify_source(None)
        assert "/etc/corp/certs" in source
        assert warning is not None and "c_rehash" in warning

    def test_ca_bundle_wins_over_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/corp/ca.pem")
        bundle = tmp_path / "profile-ca.pem"
        bundle.write_text("")
        source, _warning = describe_verify_source(str(bundle))
        assert "profile-ca.pem" in source
        assert "/etc/corp/ca.pem" not in source

    def test_ca_bundle_directory_warns(self, tmp_path: Path) -> None:
        source, warning = describe_verify_source(str(tmp_path))
        assert "directory" in source
        assert warning is not None and "c_rehash" in warning


class TestClientTrustStoreFailure:
    def test_client_construction_failure_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A broken SSL_CERT_FILE raises ssl.SSLError while httpx.Client is being
        *built* — before any request. In 0.3.1 that escaped every AtlasError
        boundary and every command died as a redacted `Unexpected internal error`
        (GitHub #16 follow-up)."""
        import httpx

        from atlassian_skills.core.auth import Credential
        from atlassian_skills.core.client import BaseClient

        def boom(*args: object, **kwargs: object) -> None:
            raise ssl.SSLError("unknown error (_ssl.c:4035)")

        monkeypatch.setattr(httpx, "Client", boom)
        monkeypatch.setenv("SSL_CERT_FILE", "/broken/corp.pem")
        with pytest.raises(ValidationError) as exc:
            BaseClient("https://jira.example.com", Credential(method="pat", token="t"))
        assert (exc.value.context or {}).get("reason") == "invalid_trust_store"
        assert "SSLError" in exc.value.message
        assert "SSL_CERT_FILE" in (exc.value.context or {}).get("env", "")
