"""Tests for cli/doctor.py — diagnostic command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from atlassian_skills.cli.main import app


@pytest.fixture(autouse=True)
def _offline_pypi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `doctor`'s top-of-output PyPI check offline by default so the suite never hits the
    network. Tests that exercise the update banner re-patch this with a concrete value."""
    monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: None)
    monkeypatch.setattr("atlassian_skills.core.config.config_path", lambda: tmp_path / "config.toml")


class TestDoctor:
    def test_doctor_runs_with_no_install(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Clear any host env that would mask the 'NOT SET' state
        for var in (
            "JIRA_PERSONAL_TOKEN",
            "CONFLUENCE_PERSONAL_TOKEN",
            "BITBUCKET_TOKEN",
            "ATLS_DEFAULT_JIRA_TOKEN",
            "ATLS_DEFAULT_CONFLUENCE_TOKEN",
            "ATLS_DEFAULT_BITBUCKET_TOKEN",
            "ATLS_DEFAULT_JIRA_URL",
            "ATLS_DEFAULT_CONFLUENCE_URL",
            "ATLS_DEFAULT_BITBUCKET_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Platform:" in result.output
        assert "Attachment writer: native" in result.output
        assert "Paths:" in result.output
        assert "Skill installation status:" in result.output
        assert "Auth:" in result.output
        assert "not installed" in result.output  # no skill files exist
        assert "NOT SET" in result.output  # no token env vars

    def test_doctor_shows_installed_version_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Plant a Claude skill with a version marker
        skill = tmp_path / ".claude" / "skills" / "atls" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("<!-- installed-by: atls 0.2.7 -->", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "v0.2.7" in result.output

    def test_doctor_shows_url_and_token_sources(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        # Plant a config.toml with one URL + an env token
        from atlassian_skills.core.config import Config, Profile, save_config

        cfg = Config()
        cfg.profiles["default"] = Profile(jira_url="https://jira.dr-test")
        save_config(cfg)
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "ABCDEF1234")

        runner = CliRunner()
        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "https://jira.dr-test" in result.output
        assert "config" in result.output
        assert "length=10" in result.output

    def test_native_writer_does_not_probe_compatibility(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import atlassian_skills.core.config as config_mod

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        monkeypatch.setattr(
            "atlassian_skills.core.attachment_io.verify_compatible_attachment_writer",
            lambda: pytest.fail("native doctor must not start a compatibility probe"),
        )

        result = CliRunner().invoke(app, ["doctor", "--no-update-check"])

        assert result.exit_code == 0
        assert "Attachment writer: native" in result.output

    def test_compatible_writer_reports_dependency_check_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import atlassian_skills.cli.doctor as doctor_mod
        import atlassian_skills.core.config as config_mod
        from atlassian_skills.core.config import Config, save_config

        monkeypatch.setattr(config_mod, "config_path", lambda: tmp_path / "config.toml")
        save_config(Config(attachment_writer="compatible"))
        monkeypatch.setattr(doctor_mod, "_detect_platform", lambda: "windows")
        monkeypatch.setattr(
            "atlassian_skills.core.attachment_io.verify_compatible_attachment_writer",
            lambda: tmp_path / "Git" / "bin" / "bash.exe",
        )

        result = CliRunner().invoke(app, ["doctor", "--no-update-check"])

        assert result.exit_code == 0
        assert "Attachment writer: compatible" in result.output
        assert "Attachment compatibility dependencies: available" in result.output
        assert "Attachment compatibility: ready" not in result.output


class TestDoctorUpdateCheck:
    """The PyPI freshness banner shown at the top of `doctor`."""

    def test_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from atlassian_skills import __version__

        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: __version__)
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "up to date" in result.output
        assert __version__ in result.output

    def test_update_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", lambda timeout=2.0: "99.99.99")
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Update available" in result.output
        assert "99.99.99" in result.output
        assert "atls upgrade" in result.output

    def test_offline_is_non_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # autouse fixture already makes latest_pypi_version return None
        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "couldn't reach PyPI" in result.output

    def test_no_update_check_skips_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        probe = MagicMock()
        monkeypatch.setattr("atlassian_skills.cli.version.latest_pypi_version", probe)
        result = CliRunner().invoke(app, ["doctor", "--no-update-check"])

        assert result.exit_code == 0
        assert "update check skipped" in result.output
        probe.assert_not_called()


# ---------------------------------------------------------------------------
# --check-auth (GitHub #18) and the profile bug it sits next to
# ---------------------------------------------------------------------------

import ssl  # noqa: E402

import httpx  # noqa: E402
import respx  # noqa: E402

JIRA_URL = "https://jira.example.com"


@pytest.fixture
def _jira_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
    monkeypatch.setenv("ATLS_DEFAULT_JIRA_TOKEN", "probe-token")


class TestDoctorProfileSelection:
    def test_profile_flag_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`doctor` hardcoded "default", so `--profile corp` silently reported the
        wrong profile — including its URLs and token sources."""
        monkeypatch.setenv("ATLS_CORP_JIRA_URL", "https://jira.corp.example.com")
        result = CliRunner().invoke(app, ["--profile", "corp", "doctor"])
        assert result.exit_code == 0, result.output
        assert "Profile: corp" in result.output
        assert "https://jira.corp.example.com" in result.output


class TestDoctorTlsSource:
    def test_reports_certifi_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        result = CliRunner().invoke(app, ["doctor"])
        assert "TLS verify:" in result.output
        assert "certifi" in result.output

    def test_ssl_cert_dir_gets_a_hashed_layout_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.setenv("SSL_CERT_DIR", "/etc/corp/certs")
        result = CliRunner().invoke(app, ["doctor"])
        assert "SSL_CERT_DIR" in result.output
        assert "c_rehash" in result.output


class TestDoctorCheckAuth:
    @respx.mock
    def test_default_run_makes_no_atlassian_calls(self, _jira_env: None) -> None:
        """`doctor` without the flag must stay safe to run repeatedly: no network
        to the instance, no credential prompt."""
        route = respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={}))
        result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert route.call_count == 0
        assert "Auth probe" not in result.output

    @respx.mock
    def test_success_reports_the_user(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(
            return_value=httpx.Response(200, json={"displayName": "Jane Roe"})
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "✓ jira: authenticated as Jane Roe" in result.output

    @respx.mock
    def test_html_200_is_flagged_as_interception(self, _jira_env: None) -> None:
        """The high-level `get_myself()` would have raised a JSON parse error here.

        Probing through the low-level client is what makes "a proxy answered"
        distinguishable from "the server is broken".
        """
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(
            return_value=httpx.Response(
                200, text="<html>corporate sign-in</html>", headers={"content-type": "text/html"}
            )
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "content-type is text/html" in result.output
        assert "proxy or SSO portal" in result.output

    @respx.mock
    def test_401_is_reported_as_invalid_token(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(return_value=httpx.Response(401))
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "401 — token invalid or expired" in result.output

    @respx.mock
    def test_403_is_reported_as_permission(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(return_value=httpx.Response(403))
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert "403 — token is valid but lacks permission" in result.output

    @respx.mock
    def test_login_redirect_is_reported(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(
            return_value=httpx.Response(302, headers={"location": f"{JIRA_URL}/login.action"})
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert "redirected to a login page" in result.output

    @respx.mock
    def test_offorigin_redirect_names_the_proxy_host(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(
            return_value=httpx.Response(302, headers={"location": "https://proxy.corp.example.net/auth"})
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert "proxy.corp.example.net" in result.output
        assert "intercepting" in result.output

    @respx.mock
    def test_tls_failure_points_at_the_ca_options(self, _jira_env: None) -> None:
        error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain")
        error.__cause__ = ssl.SSLCertVerificationError("self-signed certificate in certificate chain")
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(side_effect=error)
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "TLS verification failed" in result.output
        # The OpenSSL detail must survive: "TLS verification failed" alone gives
        # the user nothing to act on (GitHub #16 follow-up).
        assert "self-signed certificate" in result.output
        assert "SSL_CERT_FILE" in result.output

    @respx.mock
    def test_connection_failure_is_non_fatal(self, _jira_env: None) -> None:
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(side_effect=httpx.ConnectError("refused"))
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "connection failed" in result.output

    def test_unconfigured_product_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_DEFAULT_JIRA_URL", raising=False)
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "jira:" not in result.output.split("Auth probe")[-1]

    @respx.mock
    def test_missing_credential_reports_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No token anywhere → the probe explains what is missing and makes no call.

        0.3.1 instead demanded `--resolve-credentials` on top of `--check-auth`,
        which made every keyring user type two flags for one action (GitHub #16
        follow-up). `--check-auth` itself is the opt-in now.
        """
        monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
        monkeypatch.delenv("ATLS_DEFAULT_JIRA_TOKEN", raising=False)
        monkeypatch.delenv("JIRA_PERSONAL_TOKEN", raising=False)
        route = respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(return_value=httpx.Response(200, json={}))
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert route.call_count == 0
        assert "credential unavailable" in result.output

    @respx.mock
    def test_provider_backed_credential_is_resolved_without_extra_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from atlassian_skills.core.auth import Credential

        monkeypatch.setenv("ATLS_DEFAULT_JIRA_URL", JIRA_URL)
        monkeypatch.delenv("ATLS_DEFAULT_JIRA_TOKEN", raising=False)
        monkeypatch.setattr(
            "atlassian_skills.core.auth.resolve_credential",
            lambda *args, **kwargs: Credential(method="pat", token="keyring-token"),
        )
        respx.get(f"{JIRA_URL}/rest/api/2/myself").mock(
            return_value=httpx.Response(200, json={"displayName": "Jane Roe"})
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "authenticated as Jane Roe" in result.output


def test_tls_detection_walks_the_real_exception_chain() -> None:
    """respx replaces `__cause__` with its own wrapper, so the chain path needs a
    test that builds the production shape directly (httpx from httpcore from ssl)."""
    from atlassian_skills.cli.doctor import _looks_like_tls_failure
    from atlassian_skills.core.errors import NetworkError

    try:
        try:
            raise ssl.SSLCertVerificationError("self-signed certificate in certificate chain")
        except ssl.SSLError as ssl_error:
            raise httpx.ConnectError("transport failed") from ssl_error
    except httpx.ConnectError as transport_error:
        wrapped = NetworkError("Connection error: transport failed")
        wrapped.__cause__ = transport_error

    assert _looks_like_tls_failure(wrapped) is True


def test_tls_detection_does_not_fire_on_plain_connection_refused() -> None:
    from atlassian_skills.cli.doctor import _looks_like_tls_failure
    from atlassian_skills.core.errors import NetworkError

    assert _looks_like_tls_failure(NetworkError("Connection error: [Errno 111] Connection refused")) is False


class TestBitbucketProbeIdentity:
    """`/rest/api/1.0/users` is the user *directory*, not a whoami.

    Reading a name out of `values[0]` reports whoever sorts first — a diagnostic
    tool confidently naming the wrong account is worse than naming none.
    """

    @pytest.fixture(autouse=True)
    def _bitbucket_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_DEFAULT_BITBUCKET_URL", "https://bitbucket.example.com")
        monkeypatch.setenv("ATLS_DEFAULT_BITBUCKET_TOKEN", "probe-token")

    @respx.mock
    def test_identity_comes_from_the_header_not_the_directory(self) -> None:
        respx.get("https://bitbucket.example.com/rest/api/1.0/users").mock(
            return_value=httpx.Response(
                200,
                json={"size": 1, "values": [{"name": "admin", "displayName": "Site Administrator"}]},
                headers={"X-AUSERNAME": "real.caller"},
            )
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "✓ bitbucket: authenticated as real.caller" in result.output
        assert "Site Administrator" not in result.output
        assert "admin" not in result.output.split("Auth probe")[-1]

    @respx.mock
    def test_missing_header_reports_no_name_rather_than_a_stranger(self) -> None:
        respx.get("https://bitbucket.example.com/rest/api/1.0/users").mock(
            return_value=httpx.Response(
                200, json={"size": 1, "values": [{"name": "admin", "displayName": "Site Administrator"}]}
            )
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert result.exit_code == 0, result.output
        assert "✓ bitbucket: authenticated" in result.output
        assert "Site Administrator" not in result.output

    @respx.mock
    def test_header_value_is_sanitized(self) -> None:
        respx.get("https://bitbucket.example.com/rest/api/1.0/users").mock(
            return_value=httpx.Response(200, json={"size": 0, "values": []}, headers={"X-AUSERNAME": "ok.user"})
        )
        result = CliRunner().invoke(app, ["doctor", "--check-auth"])
        assert "authenticated as ok.user" in result.output


def test_probe_name_does_not_descend_into_collections() -> None:
    from atlassian_skills.cli.doctor import _probe_name

    assert _probe_name({"values": [{"displayName": "Someone Else"}]}) is None
    assert _probe_name({"displayName": "Jane Roe"}) == "Jane Roe"
