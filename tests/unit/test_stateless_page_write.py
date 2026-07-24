from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import cfxmark
import pytest

from atlassian_skills.confluence.models import Page, PageVersion, Space
from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    build_source_conversion,
    create_page_stateless,
    publish_page_update,
)
from atlassian_skills.core.errors import (
    ConflictError,
    ConversionConsentRequiredError,
    MigrationConsentRequiredError,
    StaleError,
    ValidationError,
)


class FakeClient:
    base_url = "https://confluence.example.com"

    def __init__(self, storage: str = "<p>old</p>") -> None:
        self.pages: dict[str, Page] = {
            "123": self._page("123", "Source", storage, version=7, space="SPACE", parent_id=None)
        }
        self.update_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.raise_before_update = False
        self.raise_after_update = False
        self.raise_before_create = False
        self.raise_after_create = False
        self.mutate_on_get: int | None = None
        self.get_count = 0
        self.search_results: list[list[Page]] = []
        # Simulate Confluence re-serializing storage on save (e.g. text quotes
        # become &quot; entities). Applied to the stored body so the read-back
        # differs byte-for-byte from the candidate while staying semantically equal.
        self.readback_reserialize: Callable[[str], str] | None = None

    @staticmethod
    def _page(
        page_id: str,
        title: str,
        storage: str,
        *,
        version: int,
        space: str,
        parent_id: str | None,
    ) -> Page:
        ancestors = [] if parent_id is None else [Page(id=parent_id, title="Parent")]
        return Page(
            id=page_id,
            title=title,
            status="current",
            space=Space(key=space, name=space),
            version=PageVersion(number=version),
            body_storage=storage,
            ancestors=ancestors,
        )

    def get_page(self, page_id: str, **_kwargs: Any) -> Page:
        self.get_count += 1
        if self.mutate_on_get == self.get_count:
            current = self.pages[page_id]
            self.pages[page_id] = self._page(
                page_id,
                current.title,
                "<p>concurrent</p>",
                version=current.version.number + 1 if isinstance(current.version, PageVersion) else 8,
                space=current.space.key if current.space else "SPACE",
                parent_id=None,
            )
        return self.pages[page_id].model_copy(deep=True)

    def update_page(
        self,
        page_id: str,
        title: str,
        body: str,
        version_number: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.raise_before_update:
            raise RuntimeError("PUT response failed before commit")
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
        stored_body = self.readback_reserialize(body) if self.readback_reserialize is not None else body
        self.pages[page_id] = self._page(
            page_id,
            title,
            stored_body,
            version=version_number,
            space=current.space.key if current.space else "SPACE",
            parent_id=None,
        )
        if self.raise_after_update:
            raise RuntimeError("PUT response lost after commit")
        return {"id": page_id}

    def search(self, _cql: str, limit: int = 25) -> Any:
        if self.search_results:
            return SimpleNamespace(results=self.search_results.pop(0)[:limit])
        matches = [page for page in self.pages.values() if page.id != "123"][:limit]
        return SimpleNamespace(results=matches)

    def create_page(
        self,
        space_key: str,
        title: str,
        body: str,
        ancestor_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if self.raise_before_create:
            raise RuntimeError("POST failed before commit")
        page_id = str(900 + len(self.create_calls))
        self.create_calls.append({"id": page_id, "body": body})
        stored_body = self.readback_reserialize(body) if self.readback_reserialize is not None else body
        self.pages[page_id] = self._page(
            page_id,
            title,
            stored_body,
            version=1,
            space=space_key,
            parent_id=ancestor_id,
        )
        if self.raise_after_create:
            raise RuntimeError("POST response lost after commit")
        return {"id": page_id}


def _create(client: FakeClient, markdown: str, *, accept: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    return create_page_stateless(
        client,
        space="SPACE",
        title="New page",
        parent_id="10",
        body=markdown,
        body_format="md",
        dry_run=dry_run,
        accept_conversion=accept,
        next_action_argv=("atls", "confluence", "page", "create"),
    )


def test_create_clean_markdown_requires_no_consent() -> None:
    result = _create(FakeClient(), "# Heading\n\nBody\n", dry_run=True)

    assert result["status"] == "ready_to_create"
    assert result["consent_required"] is False
    assert result["conversion_fingerprint"] is None
    assert result["method"] == "POST"


def test_create_loss_report_is_source_bound_and_requires_exact_consent() -> None:
    client = FakeClient()
    conversion = build_source_conversion("x <span>raw</span> y\n")

    assert conversion.consent_required is True
    assert conversion.conversion_fingerprint is not None
    dry_run = _create(client, "x <span>raw</span> y\n", dry_run=True)
    action = dry_run["next_actions"][0]
    assert action == {
        "id": "retry_with_consent",
        "requires_user_approval": True,
        "description_code": "REVIEW_CONVERSION_AND_RETRY",
        "argv": [
            "atls",
            "confluence",
            "page",
            "create",
            "--accept-conversion",
            conversion.conversion_fingerprint,
        ],
    }
    with pytest.raises(ConversionConsentRequiredError) as exc_info:
        _create(client, "x <span>raw</span> y\n")
    assert exc_info.value.context["reason"] == "conversion_consent_required"
    assert exc_info.value.code == "CONVERSION_CONSENT_REQUIRED"
    assert exc_info.value.context["next_actions"] == [action]
    assert client.create_calls == []

    result = _create(client, "x <span>raw</span> y\n", accept=conversion.conversion_fingerprint)
    assert result["status"] == "created"
    assert len(client.create_calls) == 1

    changed = build_source_conversion("changed <span>raw</span> y\n")
    assert changed.conversion_fingerprint != conversion.conversion_fingerprint


@pytest.mark.parametrize(
    "source",
    ["asset.png", "file:///tmp/asset.png", "data:image/png;base64,AAAA", "https://user:secret@example.com/a.png"],
)
def test_create_rejects_non_external_or_credentialed_images(source: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        build_source_conversion(f"![asset]({source})\n")

    assert exc_info.value.context["reason"] == "stateless_image_source_unsupported"


def test_create_response_loss_adopts_only_one_exact_candidate() -> None:
    client = FakeClient()
    client.raise_after_create = True

    result = _create(client, "Body\n")

    assert result["status"] == "created"
    assert result["recovery"] == "lost_response_adopted"
    assert len(client.create_calls) == 1


def test_create_response_loss_never_retries_ambiguous_candidate() -> None:
    client = FakeClient()
    client.raise_before_create = True
    candidates = [
        client._page("901", "New page", "<p>Body</p>", version=1, space="SPACE", parent_id="10"),
        client._page("902", "New page", "<p>Body</p>", version=1, space="SPACE", parent_id="10"),
    ]
    client.search_results = [[], candidates]

    with pytest.raises(ConflictError) as exc_info:
        _create(client, "Body\n")

    assert exc_info.value.context["reason"] == "page_create_outcome_ambiguous"
    assert client.create_calls == []


def test_markdown_update_uses_source_bound_ownership_and_readback() -> None:
    client = FakeClient()
    preflight = build_page_update_preflight(client, "123", "new\n", body_format="md", if_version=7)

    assert preflight.ownership["intended_operation_ids"]
    assert preflight.ownership["unclassified"] == []
    result = publish_page_update(
        client,
        preflight,
        accept_migration=None,
        reason="Caller supplied",
        minor_edit=False,
        next_action_argv=("atls", "confluence", "page", "update", "123"),
    )

    assert result["status"] == "updated"
    assert result["version"] == 8
    assert preflight.migration_fingerprint is not None
    digest = preflight.migration_fingerprint.split(":", maxsplit=1)[1][:12]
    assert client.update_calls[0]["reason"] == f"atls markdown migration {digest}: reported=0; Caller supplied"


def test_markdown_update_html_comment_is_consentable_not_unclassified() -> None:
    """A user HTML comment is a classified, consentable source loss
    (``html-comment-dropped``), not the ``source_conversion_unclassified`` wall.

    Regression guard for the gap a live push caught: the diagnostic must stay
    ``blocking`` + ``content_loss`` so ``_validate_source_diagnostics`` admits it and
    consent applies. An informational-severity attempt let cfxmark's push_safe stay
    True but produced ``source_conversion_unclassified`` at the managed layer.
    """
    client = FakeClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "old\n\n<!-- a user note -->\n",
        body_format="md",
        if_version=7,
    )

    assert preflight.consent_required is True
    codes = [occurrence["code"] for occurrence in preflight.source_conversion_report["occurrences"]]
    assert "html-comment-dropped" in codes
    with pytest.raises(MigrationConsentRequiredError) as exc_info:
        publish_page_update(
            client,
            preflight,
            accept_migration=None,
            reason=None,
            minor_edit=False,
            next_action_argv=("atls", "confluence", "page", "update", "123"),
        )
    assert exc_info.value.context["reason"] == "migration_consent_required"


def test_markdown_update_source_loss_requires_exact_source_bound_fingerprint() -> None:
    client = FakeClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "x <span>raw</span> y\n",
        body_format="md",
        if_version=7,
    )

    assert preflight.consent_required is True
    assert preflight.source_conversion_report["occurrences"]
    with pytest.raises(MigrationConsentRequiredError) as exc_info:
        publish_page_update(
            client,
            preflight,
            accept_migration=None,
            reason=None,
            minor_edit=False,
            next_action_argv=("atls", "confluence", "page", "update", "123"),
        )
    assert exc_info.value.context["reason"] == "migration_consent_required"
    assert exc_info.value.code == "MIGRATION_CONSENT_REQUIRED"
    assert exc_info.value.context["next_actions"] == [
        {
            "id": "retry_with_consent",
            "requires_user_approval": True,
            "description_code": "REVIEW_MIGRATION_AND_RETRY",
            "argv": [
                "atls",
                "confluence",
                "page",
                "update",
                "123",
                "--accept-migration",
                preflight.migration_fingerprint,
            ],
        }
    ]
    assert client.update_calls == []


def test_markdown_update_unknown_remote_replacement_fails_closed() -> None:
    client = FakeClient(
        '<ac:structured-macro ac:name="future"><ac:plain-text-body><![CDATA[x]]></ac:plain-text-body>'
        "</ac:structured-macro>"
    )

    with pytest.raises(ValidationError) as exc_info:
        build_page_update_preflight(client, "123", "replacement\n", body_format="md", if_version=7)

    assert exc_info.value.context["reason"] == "ownership_proof_invalid"


def test_page_update_second_read_blocks_remote_drift() -> None:
    client = FakeClient()
    preflight = build_page_update_preflight(client, "123", "new\n", body_format="md", if_version=7)
    client.mutate_on_get = 2

    with pytest.raises(StaleError) as exc_info:
        publish_page_update(
            client,
            preflight,
            accept_migration=None,
            reason=None,
            minor_edit=False,
            next_action_argv=(),
        )

    assert exc_info.value.context["reason"] == "prewrite_remote_drift"
    assert client.update_calls == []


def test_page_update_response_loss_is_adopted_by_exact_readback() -> None:
    client = FakeClient()
    preflight = build_page_update_preflight(client, "123", "<p>new</p>", body_format="storage", if_version=7)
    client.raise_after_update = True

    result = publish_page_update(
        client,
        preflight,
        accept_migration=None,
        reason=None,
        minor_edit=False,
        next_action_argv=(),
    )

    assert result["status"] == "updated"
    assert result["recovery"] == "lost_response_adopted"
    assert len(client.update_calls) == 1


def test_page_update_can_change_only_title_after_fresh_revalidation() -> None:
    client = FakeClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "<p>old</p>",
        body_format="storage",
        title="Renamed",
        if_version=7,
    )

    assert preflight.would_update is True
    result = publish_page_update(
        client,
        preflight,
        accept_migration=None,
        reason=None,
        minor_edit=False,
        next_action_argv=(),
    )

    assert result["status"] == "updated"
    assert client.pages["123"].title == "Renamed"


def test_markdown_update_readback_accepts_server_reserialization() -> None:
    # Confluence re-serializes storage on save: text quotes come back as &quot;
    # entities and <hr/> gains a space. The read-back then differs byte-for-byte
    # from the candidate even though nothing changed. The update must still be
    # reported as succeeded via the source-bound semantic comparison.
    client = FakeClient()
    client.readback_reserialize = lambda storage: storage.replace('"', "&quot;").replace("<hr/>", "<hr />")
    preflight = build_page_update_preflight(
        client,
        "123",
        'say "hi" now\n\n---\n',
        body_format="md",
        if_version=7,
    )

    result = publish_page_update(
        client,
        preflight,
        accept_migration=None,
        reason="Caller supplied",
        minor_edit=False,
        next_action_argv=("atls", "confluence", "page", "update", "123"),
    )

    # The byte hash of the read-back differs from the candidate, proving the
    # semantic comparison is what accepts it.
    readback = client.get_page("123")
    assert readback.body_storage != preflight.candidate_storage
    assert result["status"] == "updated"
    assert result["version"] == 8


# ---------------------------------------------------------------------------
# Read-back tolerance for the server-stamped ac:macro-id. Confluence assigns a
# macro-id to every macro on save; the managed Markdown side never carries one,
# so the read-back must accept a macro-id-only difference while still rejecting
# any real content, structure, parameter, or other attribute change.
# ---------------------------------------------------------------------------

_MACRO_BODY = (
    '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>hi</p></ac:rich-text-body></ac:structured-macro>'
)


def _stamp_macro_id(storage: str) -> str:
    return storage.replace(
        '<ac:structured-macro ac:name="info">',
        '<ac:structured-macro ac:name="info" ac:macro-id="11111111-2222-3333-4444-555555555555">',
    )


def test_markdown_update_readback_ignores_server_macro_id() -> None:
    client = FakeClient()
    client.readback_reserialize = _stamp_macro_id
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    result = publish_page_update(
        client, preflight, accept_migration=None, reason="r", minor_edit=False, next_action_argv=()
    )

    readback = client.get_page("123")
    assert "ac:macro-id" in (readback.body_storage or "")
    assert readback.body_storage != preflight.candidate_storage
    assert result["status"] == "updated"
    assert result["version"] == 8


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda s: s.replace('ac:name="info"', 'ac:name="note"'),  # macro name
        lambda s: s.replace("<p>hi</p>", "<p>HACKED</p>"),  # macro body
        lambda s: _stamp_macro_id(s).replace('ac:name="info"', 'ac:name="warning"'),  # macro-id + real change
    ],
)
def test_markdown_update_readback_rejects_real_server_change(corrupt: Callable[[str], str]) -> None:
    client = FakeClient()
    client.readback_reserialize = corrupt
    preflight = build_page_update_preflight(client, "123", _MACRO_BODY, body_format="storage", if_version=7)

    with pytest.raises(ValidationError) as exc_info:
        publish_page_update(client, preflight, accept_migration=None, reason="r", minor_edit=False, next_action_argv=())

    assert exc_info.value.context["reason"] == "page_update_readback_mismatch"


# A managed Markdown round-trip that reuses an existing macro carries that
# macro's ac:macro-id (opaque-macro instance reuse preserves its comments,
# permissions, and attachments). The read-back tolerance is one-directional: the
# server may STAMP an id the candidate lacked, but an id the candidate already
# carries must survive unchanged. A changed or removed id is a real loss of macro
# identity, not serialization trivia, so the read-back must reject it.
# ---------------------------------------------------------------------------


def _jira_page_storage(macro_id: str) -> str:
    return (
        f'<ac:structured-macro ac:name="jira" ac:macro-id="{macro_id}">'
        '<ac:parameter ac:name="key">DOC-1</ac:parameter>'
        "</ac:structured-macro>"
        "<p>After</p>"
    )


def _edited_markdown_for(storage: str) -> str:
    base = cfxmark.to_md_artifact(storage, options=cfxmark.ConversionOptions(profile="editable"))
    return base.markdown.replace("After", "Edited")


def test_markdown_update_readback_preserves_existing_macro_id() -> None:
    existing = "existing0-1111-2222-3333-444444444444"
    storage = _jira_page_storage(existing)
    client = FakeClient(storage=storage)
    preflight = build_page_update_preflight(
        client, "123", _edited_markdown_for(storage), body_format="md", if_version=7
    )
    # Precondition: the managed candidate really carries the pre-existing id.
    assert f'ac:macro-id="{existing}"' in preflight.candidate_storage

    result = publish_page_update(
        client, preflight, accept_migration=None, reason="r", minor_edit=False, next_action_argv=()
    )

    assert result["status"] == "updated"
    assert result["version"] == 8


@pytest.mark.parametrize(
    "corrupt_existing_id",
    [
        pytest.param(lambda s, e: s.replace(e, "server00-9999-8888-7777-666666666666"), id="changed"),
        pytest.param(lambda s, e: s.replace(f' ac:macro-id="{e}"', ""), id="removed"),
    ],
)
def test_markdown_update_readback_rejects_existing_macro_id_change(
    corrupt_existing_id: Callable[[str, str], str],
) -> None:
    existing = "existing0-1111-2222-3333-444444444444"
    storage = _jira_page_storage(existing)
    client = FakeClient(storage=storage)
    client.readback_reserialize = lambda body: corrupt_existing_id(body, existing)
    preflight = build_page_update_preflight(
        client, "123", _edited_markdown_for(storage), body_format="md", if_version=7
    )
    assert f'ac:macro-id="{existing}"' in preflight.candidate_storage

    with pytest.raises(ValidationError) as exc_info:
        publish_page_update(client, preflight, accept_migration=None, reason="r", minor_edit=False, next_action_argv=())

    assert exc_info.value.context["reason"] == "page_update_readback_mismatch"


def _create_storage(client: FakeClient, body: str) -> dict[str, Any]:
    return create_page_stateless(
        client,
        space="SPACE",
        title="New page",
        parent_id="10",
        body=body,
        body_format="storage",
        dry_run=False,
        accept_conversion=None,
        next_action_argv=("atls", "confluence", "page", "create"),
    )


def test_create_readback_ignores_server_macro_id() -> None:
    client = FakeClient()
    client.readback_reserialize = _stamp_macro_id

    result = _create_storage(client, _MACRO_BODY)

    assert result["status"] == "created"
    created = client.pages[str(result["id"])]
    assert "ac:macro-id" in (created.body_storage or "")


def test_create_response_loss_reconciles_across_server_macro_id() -> None:
    # The POST commit lands but its response is lost; reconciliation finds the
    # created page by title and must accept it despite the server macro-id stamp.
    client = FakeClient()
    client.readback_reserialize = _stamp_macro_id
    client.raise_after_create = True

    result = _create_storage(client, _MACRO_BODY)

    assert result["status"] == "created"
    assert result.get("recovery") == "lost_response_adopted"


def test_create_readback_rejects_real_server_change() -> None:
    client = FakeClient()
    client.readback_reserialize = lambda s: _stamp_macro_id(s).replace("<p>hi</p>", "<p>HACKED</p>")

    with pytest.raises(ValidationError) as exc_info:
        _create_storage(client, _MACRO_BODY)

    assert exc_info.value.context["reason"] == "page_create_readback_mismatch"
