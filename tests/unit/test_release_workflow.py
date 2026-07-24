"""Release-workflow contracts that must fail closed before publication."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

_RELEASE_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"
_VERSION_GUARD = Path(__file__).parents[2] / ".github" / "scripts" / "check_pypi_version_absent.py"


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        return False


def _load_version_guard() -> object:
    spec = importlib.util.spec_from_file_location("check_pypi_version_absent", _VERSION_GUARD)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _http_error(status: int) -> HTTPError:
    return HTTPError("https://pypi.org/", status, "test", hdrs=None, fp=None)


def test_release_workflow_checks_pypi_before_publish() -> None:
    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert 'python .github/scripts/check_pypi_version_absent.py atlassian-skills "${GITHUB_REF_NAME#v}"' in workflow
    assert workflow.index("Verify PyPI version is absent") < workflow.index("run: uv publish")
    assert "requests.get" not in workflow


@pytest.mark.parametrize(
    ("opener", "expected_exit"),
    [
        (lambda *_args, **_kwargs: _Response(404), 0),
        (lambda *_args, **_kwargs: _Response(200), 1),
        (lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(500)), 1),
        (lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")), 1),
    ],
)
def test_pypi_version_guard_allows_only_404(
    opener: object, expected_exit: int, capsys: pytest.CaptureFixture[str]
) -> None:
    guard = _load_version_guard()

    assert guard.main(["atlassian-skills", "0.3.0"], opener=opener) == expected_exit
    if expected_exit:
        assert "refusing to publish" in capsys.readouterr().err


def test_pypi_version_guard_uses_a_bounded_timeout() -> None:
    guard = _load_version_guard()
    received_timeout: float | None = None

    def opener(_url: str, *, timeout: float) -> _Response:
        nonlocal received_timeout
        received_timeout = timeout
        return _Response(404)

    guard.ensure_version_absent("atlassian-skills", "0.3.0", opener=opener)
    assert received_timeout == 10.0
