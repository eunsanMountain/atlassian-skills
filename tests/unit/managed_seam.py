"""Test seams for managed-workflow setup, kept out of `conftest`.

`conftest` imports `BodyClient` from a test module in order to define `HistoryClient`, so
anything a *test module* needs to import cannot live there without a cycle -- and
`test_state_free_body_write`, which owns `BodyClient`, is exactly such a module.

Nothing here imports from a test module, which is the whole reason it is a separate file.
"""

from __future__ import annotations

from typing import Any


def pull_managed_suspending_the_write_policy(client: Any, page_id: str, output_path: Any, **kwargs: Any) -> Any:
    """Pull a managed file for a page whose grade forbids the canonical write.

    **A test seam, not a product path.** §8.2 forbids the write for `xhtml_required`
    without a closed preservation capability, and the registry is empty, so a caller
    cannot obtain this file. Many tests need one anyway, because their subject is
    something else entirely -- the projection, an attachment record, the shape of a
    consent refusal -- and the page they were written against happens to be
    unclassifiable.

    Only the grade's write permission is changed. The storage, the conversion, the proof
    and the push all see exactly what they saw before, which is what keeps those tests
    measuring their own subject rather than this one.

    Where a test's subject *is* the write policy, it must not use this. Those tests live
    in `test_pull_compatibility.py` and go through the real pull.
    """

    import pytest as _pytest

    import atlassian_skills.confluence.managed_pull as managed_pull_module
    from atlassian_skills.confluence.compatibility import compatibility_payload
    from atlassian_skills.confluence.pull_md import pull_md

    def pullable(*args: object, **payload_kwargs: object) -> dict:
        return {**compatibility_payload(*args, **payload_kwargs), "canonical_write_permitted": True}

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(managed_pull_module, "compatibility_payload", pullable)
        return pull_md(client, page_id, output_path=output_path, portable=True, **kwargs)


def prepare_portable_pull_suspending_the_write_policy(*args: Any, **kwargs: Any) -> Any:
    """The same seam, one layer down, for tests that call `prepare_portable_pull`.

    Their subject is what the prepared pull *says* -- the edit-preflight guidance, the
    migration report -- and a refusal has different guidance by design, so they would be
    measuring the refusal instead of the thing they are named for.
    """

    import pytest as _pytest

    import atlassian_skills.confluence.managed_pull as managed_pull_module
    from atlassian_skills.confluence.compatibility import compatibility_payload

    def pullable(*payload_args: Any, **payload_kwargs: Any) -> dict:
        return {**compatibility_payload(*payload_args, **payload_kwargs), "canonical_write_permitted": True}

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(managed_pull_module, "compatibility_payload", pullable)
        return managed_pull_module.prepare_portable_pull(*args, **kwargs)
