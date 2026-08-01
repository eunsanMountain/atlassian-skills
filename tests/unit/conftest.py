from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.core.auth import Credential
from atlassian_skills.core.client import BaseClient
from atlassian_skills.jira.client import JiraClient
from tests.unit.test_state_free_body_write import BodyClient


@pytest.fixture
def bypass_tty_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `_ensure_interactive_terminal` a no-op for wizard tests.

    `CliRunner` runs commands with a non-TTY stdin, which would otherwise hit the
    wizard's TTY guard and exit before any prompt is exercised. Tests that
    specifically validate the guard skip this fixture.
    """
    import atlassian_skills.cli.setup as setup_mod

    monkeypatch.setattr(setup_mod, "_ensure_interactive_terminal", lambda: None)


@pytest.fixture
def base_client(mock_credential: Credential, jira_base_url: str) -> BaseClient:
    return BaseClient(jira_base_url, mock_credential)


@pytest.fixture
def jira_client(mock_credential: Credential, jira_base_url: str) -> JiraClient:
    return JiraClient(jira_base_url, mock_credential)


@pytest.fixture
def confluence_client(mock_credential: Credential, confluence_base_url: str) -> ConfluenceClient:
    return ConfluenceClient(confluence_base_url, mock_credential)


#: Where each agent keeps its configuration, when it is told rather than found.
#: `setup.py` resolves each of these BEFORE falling back to `Path.home()`, so a
#: test that redirects home is still reading the developer's real machine if one
#: of them is exported.
_AGENT_HOME_VARS = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "COPILOT_HOME",
    "AGENTS_HOME",
)


@pytest.fixture(autouse=True)
def _agent_homes_come_from_the_test_not_the_shell() -> Iterator[None]:
    """Unset the agent home variables for every test in this directory.

    `test_status_installed` patches `Path.home()` to a `tmp_path` and asserts the
    Codex skill is reported as installed there. With `CODEX_HOME` exported --
    which it is inside any Codex session, and inside several editor
    integrations -- the patched home is never consulted and the assertion
    describes whatever that directory happens to hold.

    CI has none of them set, so the suite was green there and red on the machine
    of anyone running an agent. Same shape as `_client_factory_is_put_back`: the
    test was reading state it did not set.

    Saved and restored by hand rather than through `monkeypatch`. An autouse
    fixture that REQUESTS `monkeypatch` pulls its setup earlier than the guard
    below, and fixtures are torn down in reverse: the guard would then run
    before `monkeypatch` undoes anything, and report every test that patches a
    client factory -- sixteen of them -- as having leaked one.
    """

    saved = {name: os.environ.pop(name, None) for name in _AGENT_HOME_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


#: The CLI's client factories. A test that swaps one of these and does not put it
#: back changes what every later test in the session talks to.
_CLIENT_FACTORIES = (
    "atlassian_skills.cli.confluence",
    "atlassian_skills.cli.jira",
    "atlassian_skills.cli.bitbucket",
)


@pytest.fixture(autouse=True)
def _client_factory_is_put_back() -> Iterator[None]:
    """Fail the test that leaves a CLI client factory rebound.

    Found the hard way. One helper did `module._make_client = lambda ...` instead
    of patching, so from then on every command that reached Confluence through
    the CLI got a fake built for a different file -- surfacing as
    `'BodyClient' object has no attribute 'reply_to_comment'` in
    `test_bugfix_regressions.py`, under some orderings and not others.

    The failure is attributed here, to the test that did it, rather than to
    whichever unlucky test ran next. That is the whole value: the symptom and
    the cause were in different files, and nothing connected them.
    """

    import importlib

    modules = []
    for name in _CLIENT_FACTORIES:
        try:
            module = importlib.import_module(name)
        except ImportError:  # pragma: no cover - a product that dropped a CLI
            continue
        modules.append((name, module, getattr(module, "_make_client", None)))
    yield
    leaked = [name for name, module, original in modules if getattr(module, "_make_client", None) is not original]
    assert not leaked, (
        f"this test left {', '.join(leaked)}._make_client replaced. "
        "Use monkeypatch or unittest.mock.patch so it is restored."
    )


def pull_managed_accepting_named_losses(client: object, page_id: str, output_path: object, **kwargs: object) -> object:
    """Pull a managed file, approving named losses if the pull refuses without one.

    §8.2 stopped the pull from writing a canonical file for `migration_required`
    pages, which is most of the deliberately-lossy storage the suite uses as
    *setup* for something else -- a push proof, a consent prompt, a dry-run. Those
    tests want the file the way a caller would get it: pull, read the fingerprint
    off the refusal, pull again with it.

    Two calls rather than a flag that skips the policy, because a helper that wrote
    the file some other way would let the tests keep passing if the refusal broke.
    """

    from atlassian_skills.confluence.pull_md import pull_md

    result = pull_md(client, page_id, output_path=output_path, portable=True, **kwargs)
    if result.status != "not_pulled":
        return result
    assert result.migration_report_sha256, f"refused with no way forward: {result.compatibility.get('status')}"
    return pull_md(
        client,
        page_id,
        output_path=output_path,
        portable=True,
        accept_migration=result.migration_report_sha256,
        **kwargs,
    )


# --------------------------------------------------------------------------
# A history-capable fake, for the managed workflow tests only
# --------------------------------------------------------------------------
#
# Review R2's fourth option, and the reason it beat the other three: §5.4 puts page
# history first when recovering a merge base, so a fake with no `get_page_history`
# cannot reach the default path at all -- every managed test would be measuring the
# cache fallback. Widening `BodyClient` would have pushed a server-history contract
# onto the client unit tests too, which have no business knowing about it.
#
# So the capability is a subclass the managed workflow modules opt into. `BodyClient`
# is untouched.


class HistoryClient(BodyClient):  # type: ignore[misc]
    """`BodyClient` plus the endpoint the historical base resolver needs.

    Every version the fake has ever held is kept, so a test can move the page and
    then ask for the version a manifest still points at -- the mechanism §5.4 puts
    first and which nothing exercises today.

    `history_fault` reproduces the outcomes §5.4 requires to be told apart: a
    version the space no longer retains, and a permission refusal.
    """

    def __init__(self, *, history_fault: str | None = None, storage: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.history_fault = history_fault
        # `storage=` rather than assigning `client.storage` afterwards. Assigning it
        # leaves `history` holding the default body for the current version, so the
        # manifest binds one thing and history returns another -- which surfaces as
        # `historical_storage_hash_mismatch`, an integrity failure, from a test that
        # only meant to choose a page. Twenty-two tests hit exactly that.
        if storage is not None:
            self.storage = storage
        self.history: dict[int, str] = {self.version: self.storage}
        self.history_calls: list[int] = []

    def set_storage(self, storage: str) -> None:
        """Change the current body and keep history consistent with it.

        For a test that needs to pick the page's contents after construction. Plain
        assignment to `.storage` is what went wrong above; this is the spelling that
        does not.
        """

        self.storage = storage
        self.history[self.version] = storage

    def update_page(self, **kwargs: Any) -> dict[str, object]:
        result = super().update_page(**kwargs)
        self.history[self.version] = self.storage
        return result

    def move_to(self, storage: str) -> None:
        """A third party edited the page in the browser."""

        self.version += 1
        self.storage = storage
        self.history[self.version] = storage

    def get_page_history(self, page_id: str, version: int) -> SimpleNamespace:
        from atlassian_skills.core.errors import ForbiddenError, NotFoundError

        self.history_calls.append(version)
        # The two shapes the real endpoint produces, and they are what §5.4 asks to
        # be told apart: a space policy that no longer keeps the version, and an
        # account that may not read history at all.
        if self.history_fault == "permission_denied":
            raise ForbiddenError("no permission to read historical versions")
        if self.history_fault == "version_missing" or version not in self.history:
            raise NotFoundError(f"version {version} is not retained")
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.history[version],
            version=SimpleNamespace(number=version),
        )
