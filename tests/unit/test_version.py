from __future__ import annotations

from importlib.metadata import version as installed_version

import httpx
import respx
from typer.testing import CliRunner

from atlassian_skills import __version__
from atlassian_skills.cli.main import app
from atlassian_skills.cli.setup import ASSETS_DIR
from atlassian_skills.cli.version import PYPI_URL, _parse_version

runner = CliRunner()


def test_module_version_matches_distribution_metadata() -> None:
    assert __version__ == installed_version("atlassian-skills")


def test_bundled_skill_marker_matches_package_version() -> None:
    skill = ASSETS_DIR / "skills" / "atls" / "SKILL.md"

    assert f"<!-- installed-by: atls {__version__} -->" in skill.read_text(encoding="utf-8")


class TestVersionParse:
    def test_plain_semver(self) -> None:
        assert _parse_version("0.2.1") == (0, 2, 1)

    def test_prerelease_tail_stripped(self) -> None:
        assert _parse_version("1.0.0rc1") == (1, 0, 0)

    def test_ordering(self) -> None:
        assert _parse_version("0.3.0") > _parse_version("0.2.99")
        assert _parse_version("1.0.0") > _parse_version("0.9.9")


class TestVersionCommand:
    def test_plain_version_prints_installed_and_skips_network(self) -> None:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(PYPI_URL)
            result = runner.invoke(app, ["version"])

        assert result.exit_code == 0
        assert __version__ in result.output
        assert not route.called

    def test_check_when_latest_prints_latest_marker(self) -> None:
        with respx.mock() as router:
            router.get(PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": __version__}}))
            result = runner.invoke(app, ["version", "--check"])

        assert result.exit_code == 0
        assert "(latest)" in result.output

    def test_check_when_outdated_exits_nonzero_and_suggests_upgrade(self) -> None:
        with respx.mock() as router:
            router.get(PYPI_URL).mock(return_value=httpx.Response(200, json={"info": {"version": "99.99.99"}}))
            result = runner.invoke(app, ["version", "--check"])

        assert result.exit_code == 1
        assert "99.99.99" in result.output
        assert "atls upgrade" in result.output

    def test_check_network_failure_is_non_fatal(self) -> None:
        with respx.mock() as router:
            router.get(PYPI_URL).mock(side_effect=httpx.ConnectError("boom"))
            result = runner.invoke(app, ["version", "--check"])

        assert result.exit_code == 0
        assert "update check failed" in result.output
        assert __version__ in result.output

    def test_check_malformed_response_is_non_fatal(self) -> None:
        with respx.mock() as router:
            router.get(PYPI_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
            result = runner.invoke(app, ["version", "--check"])

        assert result.exit_code == 0
        assert "update check failed" in result.output


def test_latest_pypi_version_never_raises(monkeypatch) -> None:
    """A broken SSL_CERT_FILE raises ssl.SSLError while the client is being built.

    The docstring always promised "never raises"; 0.3.1 only caught httpx errors,
    so this exact case took all of `atls doctor` down as `Unexpected internal
    error` (GitHub #16 follow-up).
    """
    import ssl

    from atlassian_skills.cli.version import latest_pypi_version

    def boom(*args: object, **kwargs: object) -> None:
        raise ssl.SSLError("unknown error (_ssl.c:4035)")

    monkeypatch.setattr("atlassian_skills.cli.version.httpx.get", boom)
    assert latest_pypi_version() is None
