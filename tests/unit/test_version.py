from __future__ import annotations

import httpx
import respx
from typer.testing import CliRunner

from atlassian_skills import __version__
from atlassian_skills.cli.main import app
from atlassian_skills.cli.version import PYPI_URL, _parse_version

runner = CliRunner()


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
