"""G6: every managed JSON error/consent envelope is value-free.

Each surface in the inventory is forced onto its error/consent path, the resulting
``AtlasError`` is serialized exactly as the CLI would emit it
(``json.dumps(err.to_dict())``), and the envelope is asserted to be free of every
forbidden marker (raw body, credential-like URL, attachment filename, generic leaf
fingerprint, arbitrary diagnostic string) while still carrying the allowed detail
(reason/code/category/severity, canonical identity, counts, resolution/policy).

Markers are planted so their absence is meaningful: several are proven to reach the
underlying cfxmark exception string (row 2b/9) or the raw storage body, so a
regression that reintroduces ``detail: str(error)`` / ``asdict`` leaf dumps / raw
``source`` would fail these tests.

The inventory started as nine rows covering the Confluence managed workflow. It is
seventeen now because the tree had grown content-bearing subsystems nothing here had
ever looked at -- the Jira description commands, the storage workflow -- and nothing
said so. The three tests at the bottom are what changed that: they count which modules
raise, which of those handle content, and which the surfaces above actually reach.
Adding a module that puts a body in an envelope is now a test failure until somebody
decides which it is.

That gate is only as good as the classification behind it, which is the other lesson
here. `local_assets` sat in the not-content-bearing list on the grounds that it carried
"paths the caller passed in", and a reviewer produced its envelope holding an image
filename read out of the body and the absolute local path it resolved to. Rows 15-17
are that module now, and the claim in a classification line is worth exactly as much as
the last person who tried to disprove it.
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
    ownership_error_context,
    to_error_context,
)
from atlassian_skills.confluence.page_inspect import inspect_page
from atlassian_skills.confluence.push_md import push_md
from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    build_source_conversion,
    create_page_stateless,
    publish_page_update,
)
from atlassian_skills.core.errors import AtlasError
from tests.unit.managed_seam import pull_managed_suspending_the_write_policy
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


def _surface_full_replacement_consent(_tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """The replacement manifest names a dropped identity without exposing its UUID."""

    storage = (
        '<p>A</p><p>B</p><ac:structured-macro ac:name="info" ac:macro-id="BODYLEAKMARKER">'
        f"<ac:rich-text-body><p>{BODY}</p></ac:rich-text-body></ac:structured-macro>"
    )
    client = StatelessClient(storage)
    preflight = build_page_update_preflight(client, "123", "B changed\n\nA\n", body_format="md", if_version=7)
    with pytest.raises(AtlasError) as info:
        publish_page_update(
            client,
            preflight,
            accept_migration=None,
            accept_full_replacement=None,
            accept_discarded_identities=None,
            reason=None,
            minor_edit=False,
            next_action_argv=("atls", "confluence", "page", "update", "123"),
        )
    return info.value, [BODY, "BODYLEAKMARKER"], ["full_replacement_consent_required", "discarded_identity_count"]


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
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("2. b", "2. changed"), encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        build_managed_preflight(client, "123", path)
    return info.value, [BODY], ["ownership_proof_invalid", "fatal_class", "counts", "identities", "next_actions"]


def _surface_managed_consent(tmp: Path, mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    client = ManagedClient('<p><ac:emoticon ac:name="smile"/></p><p>Base</p>')
    path = tmp / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
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
    pull_managed_suspending_the_write_policy(client, "123", path)
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
    pull_managed_suspending_the_write_policy(client, "123", path)
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
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
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


# --------------------------------------------------------------------------
# The Jira description writers, and the storage workflow.
#
# The nine rows above were written when the managed workflow was Confluence only. 0.4.0
# makes the Jira description commands public and adds the storage workflow, and neither
# was in the inventory -- so a body reaching an envelope from either would not have been
# caught here. `test_every_content_bearing_module_is_in_this_inventory` is what keeps
# that from happening again; these are the surfaces it requires.
# --------------------------------------------------------------------------

#: A marker shaped like a legal XML entity name, so a parser that quotes the offending
#: entity back would reproduce it.
ATTACHMENT_ENTITY = "ENTITYLEAKMARKER"


def _jira_issue(description: str) -> Any:
    from tests.unit.test_jira_description_acceptance import FakeIssue

    return FakeIssue(description)


def _surface_description_grade_refusal(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """A description Markdown cannot hold, refused with its grade attached.

    The one path here that carries description text by design: `first_difference` shows
    the line that would change, because "your description has a mention in it" does not
    tell anybody which one. Bounded to a single line and asserted below to stay that way
    -- what is forbidden is the *rest* of the description arriving with it.
    """

    from atlassian_skills.jira import description_md

    mentions = "\n\n".join(f"[~user{n}] {BODY} paragraph {n}" for n in range(40))
    issue = _jira_issue(f"h2. Title\n\n[~alice] first line\n\n{mentions}\n")
    with pytest.raises(AtlasError) as info:
        description_md.pull_md(issue, "DEMO600-1", output_path=tmp / "d.md", site=issue.base_url)
    # `first_difference` is the deliberate exception and quotes the FIRST line only, so
    # the marker planted in every later paragraph must be absent.
    return info.value, [f"{BODY} paragraph 39", CRED_HOST], ["description_not_markdown_manageable", "wiki_required"]


def _surface_description_md_stale(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    from atlassian_skills.jira import description_md, description_push

    issue = _jira_issue("h2. Title\n\nprose here\n")
    path = tmp / "md.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("prose here", "my edit"), encoding="utf-8")
    issue.description = f"h2. Title\n\nsomebody else wrote {BODY}\n"
    issue.updated = "2026-07-30T00:00:00.000+0900"
    with pytest.raises(AtlasError) as info:
        description_push.push_md(issue, "DEMO600-1", path)
    return info.value, [BODY, "my edit"], ["description_remote_changed"]


def _surface_description_wiki_stale(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    from atlassian_skills.jira import description_wiki

    issue = _jira_issue("h2. Title\n\nprose here\n")
    path = tmp / "wiki.wiki"
    description_wiki.pull_wiki(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text("my edit\n", encoding="utf-8")
    issue.description = f"other author wrote {BODY}\n"
    issue.updated = "2026-07-30T00:00:00.000+0900"
    with pytest.raises(AtlasError) as info:
        description_wiki.push_wiki(issue, "DEMO600-1", path)
    return info.value, [BODY, "my edit"], ["description_remote_changed"]


def _surface_description_readback(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """The server stored something other than what was sent."""

    from atlassian_skills.jira import description_md, description_push

    issue = _jira_issue("h2. Title\n\nprose here\n")
    path = tmp / "rb.md"
    description_md.pull_md(issue, "DEMO600-1", output_path=path, site=issue.base_url)
    path.write_text(path.read_text(encoding="utf-8").replace("prose here", "my edit"), encoding="utf-8")
    issue.on_write = lambda _written: f"the server rewrote it as {BODY}\n"
    with pytest.raises(AtlasError) as info:
        description_push.push_md(issue, "DEMO600-1", path)
    return info.value, [BODY], ["description_readback_mismatch"]


def _surface_xhtml_not_well_formed(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """The one surviving `detail: str(error)` in the tree.

    `ElementTree.ParseError` renders as `<what>: line N, column M` and carries no
    document text -- measured, not assumed, which is the whole reason this row exists.
    Nothing enforces that from inside the stdlib, so the claim is pinned here: markers
    are planted in the tag name, in an entity name and in ordinary text, all three of
    which some XML parsers do quote back.
    """

    from atlassian_skills.confluence.xhtml_workflow import validate_xhtml

    path = tmp / "bad.xhtml"
    path.write_text(f"<p>{BODY} &{ATTACHMENT_ENTITY}; <b>unclosed</p>", encoding="utf-8")
    with pytest.raises(AtlasError) as info:
        validate_xhtml(path)
    return info.value, [BODY, ATTACHMENT_ENTITY], ["xhtml_not_well_formed"]


#: A directory name distinctive enough that finding it in an envelope means an absolute
#: local path got out, not that the test matched its own filename.
LOCAL_DIR = "PRIVATEWORKDIRMARKER"


def _surface_local_asset_missing(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """A body naming a picture that is not on disk.

    The module was classified as carrying "local paths the caller passed in" and left out
    of this inventory on that basis, and the classification was wrong twice over. The
    filename comes out of the *body* -- `![](x.png)` is the author's text, not an argv --
    and the envelope also carried `resolved`, the absolute path, which is this machine's
    directory layout and account name in a transcript.

    What stays is `reference`, as written. It is the only thing that locates the line to
    fix, and dropping it would leave "one of your images is missing" as the whole report.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    base = tmp / LOCAL_DIR
    base.mkdir()
    with pytest.raises(AtlasError) as info:
        resolve_local_assets(f"![shot]({ASSET_FILE})\n", base_dir=base)
    return info.value, [LOCAL_DIR, str(base)], ["asset_missing", ASSET_FILE]


def _surface_local_asset_outside_base(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """A reference climbing out of the asset directory -- the traversal refusal."""

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    base = tmp / LOCAL_DIR
    base.mkdir()
    with pytest.raises(AtlasError) as info:
        resolve_local_assets(f"![shot](../{ASSET_FILE})\n", base_dir=base)
    return info.value, [LOCAL_DIR, str(base)], ["asset_outside_base"]


def _surface_local_asset_dir_required(tmp: Path, _mp: pytest.MonkeyPatch) -> tuple[AtlasError, list[str], list[str]]:
    """Several local references and nowhere to resolve them from.

    The multi-reference path, because it reports a list rather than one name and a list
    is where an unbounded dump hides. It is capped at eight in the source; asserted here.
    """

    from atlassian_skills.confluence.local_assets import prepare_assets

    body = "".join(f"![shot{n}](image{n}-{ASSET_FILE})\n" for n in range(40))
    with pytest.raises(AtlasError) as info:
        prepare_assets(body, base_dir=None)
    assert len(info.value.context["references"]) <= 8, "the stray-reference list is no longer bounded"
    return info.value, [LOCAL_DIR], ["asset_dir_required"]


_SURFACES = {
    "row1_stateless_image": _surface_image,
    "row2a_source_conversion_invalid": _surface_source_conversion_invalid,
    "row2b_storage_candidate_invalid": _surface_storage_candidate_invalid,
    "row3_stateless_ownership": _surface_stateless_ownership,
    "row3b_full_replacement_consent": _surface_full_replacement_consent,
    "row4_update_readback": _surface_update_readback,
    "row5_create_readback": _surface_create_readback,
    "row6_managed_ownership": _surface_managed_ownership,
    "row7_managed_consent": _surface_managed_consent,
    "row7_managed_consent_with_assets": _surface_managed_consent_with_assets,
    "row7b_managed_recovery_consent_with_assets": _surface_managed_recovery_consent_with_assets,
    "row8_managed_asset_reference": _surface_managed_asset_reference,
    "row9_page_inspect": _surface_page_inspect,
    "row10_description_grade_refusal": _surface_description_grade_refusal,
    "row11_description_md_stale": _surface_description_md_stale,
    "row12_description_wiki_stale": _surface_description_wiki_stale,
    "row13_description_readback": _surface_description_readback,
    "row14_xhtml_not_well_formed": _surface_xhtml_not_well_formed,
    "row15_local_asset_missing": _surface_local_asset_missing,
    "row16_local_asset_outside_base": _surface_local_asset_outside_base,
    "row17_local_asset_dir_required": _surface_local_asset_dir_required,
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


# A page with far more fatal leaves than the error envelope will show. A table nested
# in a table cannot be spliced back, so an edit anywhere on the page leaves every leaf
# of every table unattributed -- three of them is a comfortable few dozen.
#
# A list of fourteen items was the first attempt and it does not refuse at all: C3 made
# mixed and bare lists round-trip, so those edits became provable. That is the fix
# working, and it is why this fixture is a table.
_NESTED_TABLE = (
    "<table><tbody><tr><td><table><tbody><tr><td>inner cell</td></tr></tbody></table></td></tr></tbody></table>"
)
_MANY_LEAVES = "".join(f"<p>paragraph number {n} here</p>{_NESTED_TABLE}" for n in range(3))


def _many_leaf_refusal(tmp_path: Path) -> AtlasError:
    client = ManagedClient(_MANY_LEAVES)
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("paragraph number", "edited number"), encoding="utf-8")
    with pytest.raises(AtlasError) as refused:
        build_managed_preflight(client, "123", path)
    assert refused.value.context["reason"] == "ownership_proof_invalid"
    return refused.value


def test_a_diagnosis_running_in_process_gets_every_leaf_not_the_displayed_ten(tmp_path: Path) -> None:
    """The uncapped list has to actually arrive, and it did not.

    `ownership_error_context` read `source.unclassified` off a cfxmark
    `OwnershipProofSummary`, which has no such attribute -- so `getattr(..., None)`
    returned nothing and `all_identities` was silently always empty. The harness that
    depended on it fell back to the capped ten and went on reporting shared root causes
    that were only shared among the first ten of a thirty-three leaf page. Nothing
    failed; the field was simply never populated, and a `getattr` default is what let
    that be quiet.

    So this asserts the count, not just the presence: an empty list and a truncated one
    both satisfy "the key exists".
    """

    context = _many_leaf_refusal(tmp_path).context or {}

    # The envelope's curated sample stays exactly as it was, and the page has more
    # leaves than it -- otherwise the cap is not being exercised and this test would
    # pass just as happily against the empty list it is here to rule out.
    assert len(context["identities"]) == 10
    assert context["counts"]["unclassified"] > 10
    # The in-process list is the whole set, and it agrees with the count beside it.
    # Asserted as the invariant rather than a literal: the number of leaves a nested
    # table sheds is the proof's business and may legitimately change.
    assert len(context["all_identities"]) == context["counts"]["unclassified"]
    assert context["all_identities"][:10] == context["identities"]


def test_the_uncapped_leaf_list_never_reaches_a_cli_envelope(tmp_path: Path) -> None:
    """The other half, and the reason the field is allowed to exist at all.

    It is value-free either way -- paths, fields, attributes and ordinals, no
    fingerprints -- so this is not a leak guard. It is a budget guard: shipping
    thirty-three leaves to a terminal is how an error message stops being read. If a
    future change makes it public, that should be a decision, not a redaction that
    quietly stopped being applied.

    Written expecting to pass, and it did not. `to_error_context` stripped the field
    and the proof-refusal path never calls it, so the whole uncapped list went out
    through `to_dict()`. That is why the rule now lives at the serialization boundary
    rather than in one helper each raise site has to remember.
    """

    error = _many_leaf_refusal(tmp_path)
    context = error.context or {}

    assert "all_identities" in context, "the in-process context must still carry it"
    assert "all_identities" not in to_error_context(context)
    envelope = json.dumps(error.to_dict())
    assert "all_identities" not in envelope
    # Still ten in what the user sees, so the redaction did not take the sample with it.
    assert len(json.loads(envelope)["error"]["context"]["identities"]) == 10


def test_the_other_ownership_path_does_not_pretend_to_offer_a_full_leaf_set() -> None:
    """ "No full set here" and "the full set is empty" must not look the same.

    `ownership_error_context` has two branches. The cfxmark-summary branch can supply
    every leaf; the defensive payload branch works from `asdict` dumps and caps at ten
    like the envelope does. If that branch emitted `all_identities: []`, a consumer
    would read a complete answer where there is only a truncated one -- which is the
    original defect exactly, just relocated. It omits the key instead, so the absence
    is checkable.
    """

    payload = {"unclassified": [{"identity": (("p[0]",), "text", None, n)} for n in range(14)]}

    context = ownership_error_context(payload, reason="ownership_proof_fatal")

    assert context["counts"]["unclassified"] == 14
    assert len(context["identities"]) == 10
    assert "all_identities" not in context


def test_normal_report_display_is_not_touched_by_g6(tmp_path: Path) -> None:
    """A successful (non-error) preflight keeps its rich display report untouched.

    Proves G6 narrows only error/consent envelopes: the dry-run success payload
    still carries the descriptive display_label the consent envelope redacts.
    """

    client = ManagedClient('<p><ac:emoticon ac:name="smile"/></p><p>Base</p>')
    path = tmp_path / "page.md"
    pull_managed_suspending_the_write_policy(client, "123", path, no_assets=True)
    path.write_text(path.read_text(encoding="utf-8").replace("Base", "Edited"), encoding="utf-8")

    dry_run = push_md(client, "123", path.read_text(encoding="utf-8"), managed_path=path, dry_run=True)

    assert dry_run["status"] == "migration_consent_required"
    occurrences = dry_run["migration_report"]["occurrences"]
    assert occurrences, "expected at least one migration occurrence"
    display_labels = [occ.get("display_label") for occ in occurrences]
    assert any(label and "Confluence emoticon" in label for label in display_labels)


def test_the_json_envelope_scrubs_a_credential_out_of_the_request_url() -> None:
    """The one display path that did not share the scrubber.

    `safe_display_url` lives in this same module as `AtlasError` and every
    human-readable path calls it -- the request-context line, the three verbose-log
    sites, the redirect `location`. Its docstring says why they share one function:
    error output and verbose logs both land in agent transcripts and in bug reports
    users paste. `to_dict()` is the path an agent actually reads, and it emitted the
    raw URL.

    Both carriers are checked. A Personal Access Token can arrive as userinfo in a
    configured profile URL, and as a query parameter a proxy or a redirect appended.
    """

    from atlassian_skills.core.errors import safe_display_url

    token = "PAT-9f3bDEADBEEF"
    url = f"https://svc:{token}@confluence.example.com/rest/api/content/123?token={token}"

    envelope = json.dumps(AtlasError("boom", http_url=url, http_status=401).to_dict())

    assert token not in envelope
    assert "svc:" not in envelope
    # Still useful: the scrubbed origin and path survive, so the error still says where.
    assert "confluence.example.com/rest/api/content/123" in envelope
    assert json.loads(envelope)["error"]["http_url"] == safe_display_url(url)


def test_an_in_process_only_key_is_stripped_wherever_it_is_nested() -> None:
    """The guard's own comment claims it covers every raise site. It covered depth 0.

    That is not a hypothetical shape here: `migration_preflight._redact_ownership`
    exists specifically to pop `all_identities` out of a nested `context["ownership"]`,
    so a sibling helper already defends the case the boundary guard missed. Today every
    producer spreads the key flat and nothing leaks; the point is that the boundary is
    supposed to be the thing that does not depend on remembering.
    """

    error = AtlasError(
        "boom",
        context={
            "all_identities": ["flat"],
            "ownership": {"all_identities": ["nested"], "counts": {"unclassified": 3}},
            "items": [{"all_identities": ["in a list"]}],
        },
    )

    envelope = json.dumps(error.to_dict())

    assert "all_identities" not in envelope
    for marker in ("flat", "nested", "in a list"):
        assert marker not in envelope
    # Everything else survives, so this is a scoped strip and not a blanket one.
    assert json.loads(envelope)["error"]["context"]["ownership"]["counts"]["unclassified"] == 3


# --------------------------------------------------------------------------
# Coverage of the inventory itself
# --------------------------------------------------------------------------

#: Modules that raise with a `context=` and handle page or description content. Each one
#: must be reached by a surface above, measured from the traceback rather than declared.
#:
#: Being a *list* is the point. The nine rows were written for the Confluence managed
#: workflow, and by 0.4.0 the tree had grown two more content-bearing subsystems that the
#: inventory had never seen -- not because anyone decided they were safe, but because
#: nothing counted. Adding a module that puts a body in an envelope now fails here until
#: it is either exercised or classified.
_MUST_BE_EXERCISED = frozenset(
    {
        "confluence/body_write.py",
        "confluence/local_assets.py",
        "confluence/migration_preflight.py",
        "confluence/page_inspect.py",
        "confluence/push_md.py",
        "confluence/stateless_write.py",
        "confluence/xhtml_workflow.py",
        "jira/description_md.py",
        "jira/description_push.py",
        "jira/description_wiki.py",
    }
)

#: Raising modules that handle no page or description content, with the reason. Reviewed
#: individually: a wrong entry here is how a body would get out, so "it looked like
#: plumbing" is not sufficient and each says what it actually carries.
_NOT_CONTENT_BEARING = {
    "bitbucket/client.py": "HTTP status and request identity; the URL is scrubbed at to_dict",
    "cli/confluence.py": "argument validation, before any body is read",
    "cli/jira.py": "argument validation, before any body is read",
    "confluence/asset_sync.py": "attachment ids and counts; filenames go through the asset allowlist",
    "confluence/client.py": "HTTP status and page id",
    "confluence/identity_gate.py": "identity kinds and counts, never the values",
    "confluence/managed_pull.py": "manifest fields and page identity",
    "confluence/page_copy.py": "page ids, titles and hashes between two pages the caller named",
    "confluence/patch_text.py": "match counts and offsets; the anchor is the caller's own argument",
    "confluence/prepare_merge.py": "paths to the three files it wrote, not their contents",
    "confluence/pull_md.py": "grade and manifest fields",
    "confluence/reconcile.py": "fingerprints and paths; bodies go to files, never to an envelope",
    "confluence/validate_local.py": "manifest fields read offline",
    "confluence/version_reason.py": "the caller's own version comment",
    "core/attachment_io.py": "byte counts and local paths",
    "core/client.py": "HTTP status; the URL is scrubbed at to_dict",
    "core/directory_capability.py": "filesystem capability probes",
    "core/file_identity.py": "size, mtime and inode",
    "core/managed_file.py": "path and encoding failure position",
    "core/pagination.py": "page counts and limits",
    "core/publication.py": "publication state names",
    "core/tls.py": "trust store configuration",
    "jira/description_merge.py": "paths to the three files it wrote, not their contents",
}


def _modules_raising_with_context() -> set[str]:
    """Every shipped module that raises an AtlasError carrying a context dict."""

    import ast

    src = Path(__file__).resolve().parents[2] / "src" / "atlassian_skills"
    found: set[str] = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            name = getattr(node.exc.func, "id", getattr(node.exc.func, "attr", ""))
            if not name.endswith("Error"):
                continue
            if any(kw.arg == "context" and isinstance(kw.value, ast.Dict) for kw in node.exc.keywords):
                found.add(path.relative_to(src).as_posix())
    return found


def _modules_the_inventory_reaches() -> set[str]:
    """Which modules the surfaces above actually raise from, read off the traceback."""

    import tempfile
    import traceback

    reached: set[str] = set()
    for build in _SURFACES.values():
        with tempfile.TemporaryDirectory() as directory:
            monkeypatch = pytest.MonkeyPatch()
            try:
                error, _forbidden, _required = build(Path(directory), monkeypatch)
            finally:
                monkeypatch.undo()
        for frame in traceback.extract_tb(error.__traceback__):
            parts = Path(frame.filename).parts
            if "atlassian_skills" in parts:
                reached.add("/".join(parts[parts.index("atlassian_skills") + 1 :]))
    return reached


def test_every_raising_module_is_classified() -> None:
    """A new module that raises must be called content-bearing or not, deliberately.

    The failure this prevents is silence. A module added to the tree raised into CLI
    envelopes with nobody having decided whether what it puts there is safe, and the
    inventory could not tell the difference between "reviewed and fine" and "never
    looked at".
    """

    raising = _modules_raising_with_context()
    classified = _MUST_BE_EXERCISED | _NOT_CONTENT_BEARING.keys()

    unclassified = sorted(raising - classified)
    assert not unclassified, (
        "these modules raise with a context and are in neither list; decide which, and if "
        "content-bearing add a surface to _SURFACES:\n  " + "\n  ".join(unclassified)
    )

    stale = sorted(classified - raising)
    assert not stale, (
        "these are classified but no longer raise with a context; drop them so the lists "
        "keep meaning something:\n  " + "\n  ".join(stale)
    )


def test_every_content_bearing_module_is_in_this_inventory() -> None:
    """Declaring a module content-bearing has to cost a surface, or it declares nothing."""

    missing = sorted(_MUST_BE_EXERCISED - _modules_the_inventory_reaches())
    assert not missing, "declared content-bearing but no surface in _SURFACES raises from them:\n  " + "\n  ".join(
        missing
    )


def test_a_refusal_does_not_reproduce_the_description_it_refused() -> None:
    """The loss list was one entry per loss, so a long description became its own error.

    Measured before the cap: a description with 200 mentions produced 200 loss entries
    and a 7.8KB envelope for a 12.8KB description -- 62% of the text back out of a
    command that had just refused to write anything. The Confluence side had already
    settled this shape for leaf identities: show a bounded page, say how many there
    really were, keep the full set for the object in hand.
    """

    from atlassian_skills.jira import description_md
    from atlassian_skills.jira.description_grade import MAX_REPORTED_LOSSES

    body = "h2. T\n\n" + "\n\n".join(f"[~user{n}] paragraph number {n}" for n in range(200))
    issue = _jira_issue(body)

    with pytest.raises(AtlasError) as info:
        description_md.pull_md(issue, "DEMO600-1", output_path=Path("/dev/null"), site=issue.base_url)

    grade = info.value.context["grade"]
    assert len(grade["losses"]) == MAX_REPORTED_LOSSES
    # The count is what stops the page being mistaken for the whole.
    assert grade["losses_total"] == 200
    envelope = json.dumps(info.value.to_dict())
    assert len(envelope) < len(body) // 4, f"envelope is {len(envelope)} bytes against a {len(body)}-byte description"
