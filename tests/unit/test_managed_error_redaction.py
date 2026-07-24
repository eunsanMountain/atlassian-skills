"""G6: every managed-Confluence JSON error/consent envelope is value-free.

Each surface in the §3 nine-row inventory is forced onto its error/consent path,
the resulting ``AtlasError`` is serialized exactly as the CLI would emit it
(``json.dumps(err.to_dict())``), and the envelope is asserted to be free of every
forbidden marker (raw body, credential-like URL, attachment filename, generic leaf
fingerprint, arbitrary diagnostic string) while still carrying the allowed detail
(reason/code/category/severity, canonical identity, counts, resolution/policy).

Markers are planted so their absence is meaningful: several are proven to reach the
underlying cfxmark exception string (row 2b/9) or the raw storage body, so a
regression that reintroduces ``detail: str(error)`` / ``asdict`` leaf dumps / raw
``source`` would fail these tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cfxmark
import pytest

from atlassian_skills.confluence.migration_preflight import (
    _SAFE_ASSET_ITEM_KEYS,
    _SAFE_OCCURRENCE_KEYS,
    build_managed_preflight,
)
from atlassian_skills.confluence.page_inspect import inspect_page
from atlassian_skills.confluence.pull_md import pull_md
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    build_source_conversion,
    create_page_stateless,
    publish_page_update,
)
from atlassian_skills.core.errors import AtlasError
from tests.unit.test_stateless_page_write import FakeClient as StatelessClient

# Distinctive markers. None may appear anywhere in a serialized error/consent
# envelope. Each names the class of leak it guards against.
BODY = "BODYLEAKMARKER"
CRED_USER = "SECRETUSERINFO"
CRED_PASS = "CREDPASSMARKER"
CRED_HOST = "credleakhost.example.com"
ATTACHMENT = "ATTACHMENTLEAK.png"
DIAG = "DIAGSUMMARYLEAK"
# Managed attachment filename planted in a populated asset plan (filename + the
# portable src path both embed it).
ASSET_FILE = "ATTACHMENTLEAKMARKER.png"

# Occurrence keys that must never survive redaction into an error/consent envelope.
_FORBIDDEN_OCCURRENCE_KEYS = frozenset(
    {
        "before_fingerprint",
        "after_fingerprint",
        "before_summary",
        "after_summary",
        "user_impact",
        "suggested_workflow",
        "display_label",
        "message",
    }
)


class ManagedClient:
    """Minimal managed client: only serves a fixed storage body (no PUT)."""

    base_url = "https://example.com/confluence"

    def __init__(self, storage: str) -> None:
        self.storage = storage
        self.version = 7
        self.puts = 0

    def get_page(self, page_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=page_id,
            title="Page",
            body_storage=self.storage,
            version=SimpleNamespace(number=self.version),
        )


class AssetManagedClient(ManagedClient):
    """Managed client that also serves one attachment, for a populated asset plan."""

    def __init__(self, storage: str) -> None:
        super().__init__(storage)
        self.attachment = SimpleNamespace(
            id="att-1",
            title=ASSET_FILE,
            version=SimpleNamespace(number=4),
            media_type="image/png",
            links=SimpleNamespace(download="/download/att-1"),
        )

    def list_attachments(self, page_id: str) -> list[SimpleNamespace]:
        return [self.attachment]

    def fetch_attachment_bytes(self, attachment_id: str, download_link: str | None) -> bytes:
        return b"synthetic-attachment-bytes"


def _raiser(exc: BaseException) -> Any:
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _raise


# ---------------------------------------------------------------------------
# Surface builders: each returns (AtlasError, forbidden_markers, required_substrings).
# ---------------------------------------------------------------------------


def _surface_image(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    markdown = f"![alt](https://{CRED_USER}:{CRED_PASS}@{CRED_HOST}/{ATTACHMENT})\n"
    with pytest.raises(AtlasError) as info:
        build_source_conversion(markdown)
    return (
        info.value,
        [CRED_USER, CRED_PASS, CRED_HOST, ATTACHMENT],
        ["stateless_image_source_unsupported", "credential_present", "scheme"],
    )


def _surface_source_conversion_invalid(_tmp: Path, mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    mp.setattr(cfxmark, "to_cfx_artifact", _raiser(cfxmark.ParseError(f"{BODY} <p>{ATTACHMENT}</p> {DIAG}")))
    with pytest.raises(AtlasError) as info:
        build_source_conversion("# clean heading\n\nBody paragraph.\n")
    return (
        info.value,
        [BODY, ATTACHMENT, DIAG],
        ["source_conversion_candidate_invalid", "conversion_code", "parse_error"],
    )


def _surface_storage_candidate_invalid(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    # A malformed structured-macro attribute makes cfxmark's ParseError embed the
    # marker verbatim; the redaction must replace it with a typed code.
    client = StatelessClient()
    with pytest.raises(AtlasError) as info:
        create_page_stateless(
            client,
            space="SPACE",
            title="New",
            parent_id=None,
            body=f"<ac:structured-macro {BODY}",
            body_format="storage",
            dry_run=True,
            accept_conversion=None,
            next_action_argv=("atls", "confluence", "page", "create"),
        )
    return info.value, [BODY], ["storage_candidate_invalid", "conversion_code"]


def _surface_stateless_ownership(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    storage = (
        f'<ac:structured-macro ac:name="future"><ac:plain-text-body><![CDATA[{BODY}]]>'
        "</ac:plain-text-body></ac:structured-macro>"
    )
    client = StatelessClient(storage)
    with pytest.raises(AtlasError) as info:
        build_page_update_preflight(client, "123", "replacement text\n", body_format="md", if_version=7)
    return info.value, [BODY], ["ownership_proof_invalid"]


def _surface_update_readback(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    client = StatelessClient()
    preflight = build_page_update_preflight(client, "123", "<p>new</p>", body_format="storage", if_version=7)
    # Server "returns" a genuinely different body carrying the marker.
    client.readback_reserialize = lambda body: body.replace("new", f"{BODY} changed")
    with pytest.raises(AtlasError) as info:
        publish_page_update(
            client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=()
        )
    return info.value, [BODY], ["page_update_readback_mismatch", "comparison", "storage_leaf_mismatch"]


def _surface_create_readback(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    client = StatelessClient()
    client.readback_reserialize = lambda body: body.replace("hi", f"{BODY}")
    body = '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>hi</p></ac:rich-text-body></ac:structured-macro>'
    with pytest.raises(AtlasError) as info:
        create_page_stateless(
            client,
            space="SPACE",
            title="New page",
            parent_id=None,
            body=body,
            body_format="storage",
            dry_run=False,
            accept_conversion=None,
            next_action_argv=("atls", "confluence", "page", "create"),
        )
    return info.value, [BODY], ["page_create_readback_mismatch", "comparison"]


def _surface_managed_ownership(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    # A Track-B (non-1 nested ordered list) page; an edit INSIDE the unrepresentable
    # shape makes validate_managed_cfx_artifact raise OwnershipProofError. The
    # summary is value-free; the raw body marker must not appear.
    storage = f'<ul><li>step {BODY}<ol start="2"><li>b</li></ol></li></ul>'
    client = ManagedClient(storage)
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("2. b", "2. changed"), encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        build_managed_preflight(client, "123", path)
    return info.value, [BODY], ["ownership_proof_invalid", "fatal_class", "counts", "identities"]


def _surface_managed_consent(tmp: Path, mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    client = ManagedClient('<p><ac:emoticon ac:name="smile"/></p><p>Base</p>')
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    # "Confluence emoticon ..." is cfxmark's display_label (= diagnostic.message); it
    # must be stripped. The stable code/effect stay.
    return (
        info.value,
        ["Confluence emoticon", "rendered as Unicode"],
        [
            "migration_consent_required",
            "emoticon-to-unicode",
            "converted",
        ],
    )


def _surface_managed_consent_with_assets(tmp: Path, mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    # A managed page with BOTH a consent-triggering migration (emoticon) AND a
    # populated asset plan (one attachment). The consent envelope embeds the asset
    # plan; its filename/path must be redacted, its safe hash/action/counts kept.
    storage = (
        '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
        '<p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        f'<ri:attachment ri:filename="{ASSET_FILE}"/></ac:image></p>'
    )
    client = AssetManagedClient(storage)
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path)
    return (
        info.value,
        [ASSET_FILE, "ATTACHMENTLEAKMARKER", "page.assets/"],
        ["migration_consent_required", "emoticon-to-unicode", "current_sha256"],
    )


def _surface_managed_recovery_consent_with_assets(
    tmp: Path, _mp: pytest.MonkeyPatch
) -> tuple[AtlasError, list[str], list[str]]:
    # Same asset + consent-triggering-migration scenario as row7_managed_consent_with_assets,
    # but routed through body_write._require_migration_consent (the recovery /
    # publish_managed_body raise site) instead of push_md. This raise site previously
    # embedded the raw preflight dict (assets filename/path, migration report, ownership
    # fingerprints) without to_error_context — the leak this surface guards against.
    from atlassian_skills.confluence.body_write import _require_migration_consent

    storage = (
        '<p><ac:emoticon ac:name="smile"/></p><p>Base</p>'
        '<p><ac:image xmlns:ac="http://atlassian.com/content" '
        'xmlns:ri="http://atlassian.com/resource/identifier">'
        f'<ri:attachment ri:filename="{ASSET_FILE}"/></ac:image></p>'
    )
    client = AssetManagedClient(storage)
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")
    preflight = build_managed_preflight(client, "123", path)
    assert preflight.consent_required, "scenario must trigger migration consent"
    with pytest.raises(AtlasError) as info:
        _require_migration_consent(
            preflight,
            accept_migration=None,
            next_action_argv=("atls", "confluence", "page", "push-md"),
        )
    return (
        info.value,
        [ASSET_FILE, "ATTACHMENTLEAKMARKER", "page.assets/"],
        ["migration_consent_required", "emoticon-to-unicode"],
    )


def _surface_managed_asset_reference(tmp: Path, mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    mp.setattr(
        "atlassian_skills.confluence.migration_preflight.extract_managed_asset_references",
        _raiser(ValueError(f"/secret/{ATTACHMENT} unrepresentable {BODY}")),
    )
    client = ManagedClient("<p>Base</p>")
    path = tmp / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    with pytest.raises(AtlasError) as info:
        build_managed_preflight(client, "123", path)
    return info.value, [ATTACHMENT, BODY, "/secret/"], ["managed_asset_reference_invalid", "classification"]


def _surface_page_inspect(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    client = ManagedClient(f"<ac:structured-macro {BODY}")

    def _list_attachments(_page_id: str) -> list[Any]:
        return []

    client.list_attachments = _list_attachments  # type: ignore[attr-defined]
    with pytest.raises(AtlasError) as info:
        inspect_page(client, "123", intent="read")
    return info.value, [BODY], ["page_inspect_conversion_failed", "conversion_code"]


_SURFACES = {
    "row1_stateless_image": _surface_image,
    "row2a_source_conversion_invalid": _surface_source_conversion_invalid,
    "row2b_storage_candidate_invalid": _surface_storage_candidate_invalid,
    "row3_stateless_ownership": _surface_stateless_ownership,
    "row4_update_readback": _surface_update_readback,
    "row5_create_readback": _surface_create_readback,
    "row6_managed_ownership": _surface_managed_ownership,
    "row7_managed_consent": _surface_managed_consent,
    "row7_managed_consent_with_assets": _surface_managed_consent_with_assets,
    "row7b_managed_recovery_consent_with_assets": _surface_managed_recovery_consent_with_assets,
    "row8_managed_asset_reference": _surface_managed_asset_reference,
    "row9_page_inspect": _surface_page_inspect,
}


def _iter_occurrences(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            if key in {"migration_report", "source_conversion_report"} and isinstance(node.get("occurrences"), list):
                found.extend(item for item in node["occurrences"] if isinstance(item, dict))
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            if key == "deferred_migrations":
                found.extend(item for item in node if isinstance(item, dict))
            for item in node:
                walk(item, key)

    walk(payload, None)
    return found


def _iter_asset_items(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, dict):
            if key == "assets" and isinstance(node.get("items"), list):
                found.extend(item for item in node["items"] if isinstance(item, dict))
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)

    walk(payload, None)
    return found


@pytest.mark.parametrize("surface", list(_SURFACES))
def test_managed_error_envelope_is_value_free(surface: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    error, forbidden, required = _SURFACES[surface](tmp_path, monkeypatch)

    envelope = json.dumps(error.to_dict())
    for marker in forbidden:
        assert marker not in envelope, f"{surface}: leaked forbidden marker {marker!r} in {envelope}"
    for token in required:
        assert token in envelope, f"{surface}: missing allowed detail {token!r} in {envelope}"

    payload = json.loads(envelope)
    for occurrence in _iter_occurrences(payload):
        leaked = _FORBIDDEN_OCCURRENCE_KEYS & occurrence.keys()
        assert not leaked, f"{surface}: report occurrence exposed forbidden keys {sorted(leaked)}"
        # Deny by default: a novel unsafe field name must not slip past the blocklist.
        extra = occurrence.keys() - _SAFE_OCCURRENCE_KEYS
        assert not extra, f"{surface}: report occurrence exposed non-allowlisted keys {sorted(extra)}"
    for item in _iter_asset_items(payload):
        extra = item.keys() - _SAFE_ASSET_ITEM_KEYS
        assert not extra, f"{surface}: asset item exposed non-allowlisted keys {sorted(extra)}"


def test_normal_report_display_is_not_touched_by_g6(tmp_path: Path) -> None:
    """A successful (non-error) preflight keeps its rich display report untouched.

    Proves G6 narrows only error/consent envelopes: the dry-run success payload
    still carries the descriptive display_label the consent envelope redacts.
    """

    client = ManagedClient('<p><ac:emoticon ac:name="smile"/></p><p>Base</p>')
    path = tmp_path / "page.md"
    pull_md(client, "123", output_path=path, portable=True, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")

    dry_run = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path, dry_run=True)

    assert dry_run["status"] == "migration_consent_required"
    occurrences = dry_run["migration_report"]["occurrences"]
    assert occurrences, "expected at least one migration occurrence"
    display_labels = [occ.get("display_label") for occ in occurrences]
    assert any(label and "Confluence emoticon" in label for label in display_labels)
