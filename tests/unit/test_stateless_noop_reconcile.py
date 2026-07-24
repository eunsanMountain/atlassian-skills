"""W1a: stateless no-op skip-PUT and post-PUT server-no-op reconciliation.

Covers the server-envelope-aware no_change decision (macro-id / schema-version="1"
the server adds are a no-op, but a candidate-owned id/schema change or any real body
change still writes), that a proven no-op never demands migration consent, and that a
server that accepts a PUT without bumping the version is reported as the distinct
``reconciled_no_change`` outcome — never confused with ``no_change``, a lost response,
or a genuine read-back mutation.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlassian_skills.confluence.models import PageVersion
from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    publish_page_update,
)
from atlassian_skills.core.errors import ValidationError
from tests.unit.test_stateless_page_write import _MACRO_BODY, FakeClient, _stamp_macro_id

_MACRO_WITH_ID = _stamp_macro_id(_MACRO_BODY)
_CODE_NO_SCHEMA = (
    '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[x]]></ac:plain-text-body></ac:structured-macro>'
)
_CODE_SCHEMA_1 = (
    '<ac:structured-macro ac:name="code" ac:schema-version="1">'
    "<ac:plain-text-body><![CDATA[x]]></ac:plain-text-body></ac:structured-macro>"
)


class NoVersionBumpClient(FakeClient):
    """Server accepts the PUT and stores the body but does not bump the version."""

    def update_page(self, page_id: str, title: str, body: str, version_number: int, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(
            {
                "page_id": page_id,
                "title": title,
                "body": body,
                "version": version_number,
                "reason": kwargs.get("reason"),
            }
        )
        current = self.pages[page_id]
        stored = self.readback_reserialize(body) if self.readback_reserialize is not None else body
        self.pages[page_id] = self._page(
            page_id,
            title,
            stored,
            version=current.version.number if isinstance(current.version, PageVersion) else 1,
            space=current.space.key if current.space else "SPACE",
            parent_id=None,
        )
        return {"id": page_id}


def _publish(client: FakeClient, preflight: Any) -> dict[str, Any]:
    return publish_page_update(
        client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=()
    )


# ---------------------------------------------------------------------------
# Skip-PUT no-op: server macro envelope is tolerated.
# ---------------------------------------------------------------------------


def test_macro_id_only_difference_is_no_change_without_put_or_consent() -> None:
    client = FakeClient(_MACRO_WITH_ID)  # remote carries the server-stamped macro-id
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    assert preflight.would_update is False
    result = _publish(client, preflight)

    assert result["status"] == "no_change"
    assert result["put_count"] == 0
    assert result["version"] == 7
    assert client.update_calls == []


def test_code_macro_schema_version_1_difference_is_no_change_without_put() -> None:
    client = FakeClient(_CODE_SCHEMA_1)  # remote carries ac:schema-version="1"
    preflight = build_page_update_preflight(client, "123", _CODE_NO_SCHEMA, body_format="storage", if_version=7)

    assert preflight.would_update is False
    result = _publish(client, preflight)

    assert result["status"] == "no_change"
    assert result["put_count"] == 0
    assert client.update_calls == []


# ---------------------------------------------------------------------------
# Real changes still write / fail.
# ---------------------------------------------------------------------------


def test_title_change_still_writes() -> None:
    client = FakeClient("<p>old</p>")
    preflight = build_page_update_preflight(
        client, "123", "<p>old</p>", body_format="storage", title="Renamed", if_version=7
    )

    assert preflight.would_update is True
    result = _publish(client, preflight)

    assert result["status"] == "updated"
    assert len(client.update_calls) == 1


def test_body_change_still_writes() -> None:
    client = FakeClient("<p>old</p>")
    preflight = build_page_update_preflight(client, "123", "<p>brand new body</p>", body_format="storage", if_version=7)

    assert preflight.would_update is True
    result = _publish(client, preflight)

    assert result["status"] == "updated"
    assert len(client.update_calls) == 1


def test_candidate_owned_macro_id_change_is_not_a_no_op() -> None:
    client = FakeClient(_MACRO_WITH_ID)
    other_id = _MACRO_WITH_ID.replace("11111111-2222-3333-4444-555555555555", "99999999-8888-7777-6666-555555555555")
    preflight = build_page_update_preflight(client, "123", other_id, body_format="storage", if_version=7)

    # The candidate carries a different id than the remote: a real change, not a
    # server-added envelope, so it must not be treated as a no-op.
    assert preflight.would_update is True


# ---------------------------------------------------------------------------
# Post-PUT reconciliation: server no-version-bump vs the failure modes.
# ---------------------------------------------------------------------------


def test_server_no_version_bump_is_reconciled_no_change_not_no_change() -> None:
    client = NoVersionBumpClient("<p>old</p>")
    # The server accepts the PUT, stamps a macro-id on the stored macro, and keeps
    # the version — a real PUT that turned out to be a semantic no-op server-side.
    client.readback_reserialize = _stamp_macro_id
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    assert preflight.would_update is True  # macro vs <p>old</p> is a real preflight change
    result = _publish(client, preflight)

    assert result["status"] == "reconciled_no_change"
    assert result["put_count"] == 1
    assert result["version"] == 7
    assert result["recovery"] == "server_noop_no_version_bump"
    assert len(client.update_calls) == 1


def test_response_loss_with_unchanged_version_is_put_failed_not_reconciled() -> None:
    client = FakeClient("<p>old</p>")
    client.raise_before_update = True  # PUT raises before commit; version and body stay put
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    with pytest.raises(ValidationError) as info:
        _publish(client, preflight)

    assert info.value.context["reason"] == "page_update_put_failed"


def test_genuine_readback_mutation_is_not_reconciled_no_change() -> None:
    client = NoVersionBumpClient("<p>old</p>")
    # Server keeps the version but returns a genuinely different body: a real remote
    # mutation, never a no-op.
    client.readback_reserialize = lambda _body: "<p>server mutated this</p>"
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    with pytest.raises(ValidationError) as info:
        _publish(client, preflight)

    assert info.value.context["reason"] == "page_update_readback_mismatch"
