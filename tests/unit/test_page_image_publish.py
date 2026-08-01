"""Publishing a Markdown file that points at a local image.

Writing `![](diagram.png)` next to a file and having the picture appear on the
page is the thing people expect a Markdown workflow to do. `page create` and
`page update` had no notion of a local file at all: the reference published as a
broken link and nothing said so.

The ordering differences between the two paths are the interesting part, and each
one is a real constraint rather than a preference.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlassian_skills.confluence.stateless_write import (
    build_page_update_preflight,
    create_page_stateless,
    publish_page_update,
)
from atlassian_skills.core.errors import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.unit.test_state_free_body_write import BodyClient  # noqa: E402

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


class ImageClient(BodyClient):
    """The project's own fake, plus a record of the order writes happen in.

    Built on `BodyClient` rather than written fresh: a fake that does not model
    the readback and version protocol tests a path that does not ship, and a
    first draft of this file did exactly that.
    """

    def __init__(self, storage: str = "<p>alpha paragraph text here</p>") -> None:
        super().__init__()
        self.storage = storage
        self.events: list[str] = []
        self.uploads: list[str] = []
        self.fail_upload_at: str | None = None
        self.created_title: str | None = None

    # `publish_page_update` calls positionally and `push_md` calls by keyword.
    # Accepting both keeps this fake usable from either path rather than quietly
    # modelling only the one it was first written against.
    def update_page(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.events.append("body")
        if args:
            page_id, title, body, version_number = args[:4]
            kwargs.update(page_id=page_id, title=title, body=body, version_number=version_number)
        return super().update_page(**kwargs)  # type: ignore[arg-type]

    def create_page(self, space, title, body, *, ancestor_id=None, body_format="storage"):
        self.events.append("body")
        self.storage = body
        self.created_title = title
        return {"id": "123"}

    def upload_attachment(self, page_id, path, *, filename=None, **kwargs):
        if filename == self.fail_upload_at:
            raise RuntimeError("network went away")
        self.events.append(f"upload:{filename}")
        self.uploads.append(str(filename))
        return {"title": filename}

    def list_attachments(self, page_id: str, limit: int | None = None) -> list:
        return []

    # The create path reads back with `expand=`; the update path does not pass it.
    def get_page(self, page_id: str, *args: object, **kwargs: object):
        page = super().get_page(page_id)
        return SimpleNamespace(
            id=page_id,
            title=self.created_title or page.title,
            body_storage=page.body_storage,
            version=page.version,
            ancestors=[],
            space=SimpleNamespace(key="FIX"),
        )

    def search(self, *args: object, **kwargs: object) -> SimpleNamespace:
        # `create` checks the title is free before posting, via a CQL search.
        return SimpleNamespace(results=[])


def _write_png(directory: Path, name: str = "diagram.png") -> Path:
    path = directory / name
    path.write_bytes(PNG)
    return path


# --------------------------------------------------------------------------
# Order of writes
#
# The two paths write in opposite orders, and neither is a preference.
# --------------------------------------------------------------------------


def test_an_update_uploads_before_it_writes_the_body(tmp_path: Path) -> None:
    """The page must never reference an attachment that is not there yet. The
    other order leaves a window where every image on the page is broken."""

    _write_png(tmp_path)
    client = ImageClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        asset_dir=tmp_path,
    )
    publish_page_update(client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=())
    assert client.events == ["upload:diagram.png", "body"]


def test_a_create_uploads_after_the_page_exists(tmp_path: Path) -> None:
    """The opposite order, and not a choice: an attachment needs a page to hang
    off. So a new page shows broken images for the moment it takes."""

    _write_png(tmp_path)
    client = ImageClient()
    result = create_page_stateless(
        client,
        space="FIX",
        title="New",
        parent_id=None,
        body="alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        dry_run=False,
        accept_conversion=None,
        next_action_argv=(),
        asset_dir=tmp_path,
    )
    assert client.events == ["body", "upload:diagram.png"]
    assert result["assets"]["uploaded"] == ["diagram.png"]


# --------------------------------------------------------------------------
# When the upload does not finish
# --------------------------------------------------------------------------


def test_an_update_whose_upload_fails_leaves_the_body_alone(tmp_path: Path) -> None:
    """Refusing here leaves the page exactly as it was. Writing the body anyway
    would publish a document pointing at an attachment that never arrived."""

    _write_png(tmp_path)
    client = ImageClient()
    client.fail_upload_at = "diagram.png"
    preflight = build_page_update_preflight(
        client,
        "123",
        "alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        asset_dir=tmp_path,
    )
    with pytest.raises(ValidationError) as refused:
        publish_page_update(
            client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=()
        )
    assert refused.value.context["reason"] == "asset_upload_interrupted"
    assert "body" not in client.events


def test_a_create_whose_upload_fails_reports_the_page_it_already_made(tmp_path: Path) -> None:
    """The page exists by then. Deleting it because a picture failed would throw
    away what the user asked for, so the outcome is reported instead -- under a
    status that does not read as plain success."""

    _write_png(tmp_path)
    client = ImageClient()
    client.fail_upload_at = "diagram.png"
    result = create_page_stateless(
        client,
        space="FIX",
        title="New",
        parent_id=None,
        body="alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        dry_run=False,
        accept_conversion=None,
        next_action_argv=(),
        asset_dir=tmp_path,
    )
    assert result["status"] == "created_with_missing_images"
    assert result["assets_incomplete"]["failed"] == "diagram.png"


# --------------------------------------------------------------------------
# What the boundary still refuses
# --------------------------------------------------------------------------


def test_an_image_outside_the_base_directory_is_still_refused(tmp_path: Path) -> None:
    """Relaxed for files we resolved, not for arbitrary paths. The reason for
    refusing was never that local files are suspicious -- it was that we could not
    deliver them -- and a file outside the base is one we will not."""

    base = tmp_path / "docs"
    base.mkdir()
    _write_png(tmp_path, "secret.png")
    with pytest.raises(ValidationError) as refused:
        build_page_update_preflight(
            ImageClient(),
            "123",
            "alpha paragraph text here\n\n![](../secret.png)\n",
            body_format="md",
            asset_dir=base,
        )
    assert refused.value.context["reason"] == "asset_outside_base"


def test_a_managed_asset_marker_still_belongs_to_the_managed_path(tmp_path: Path) -> None:
    """Untouched by this change: a document already carrying managed markers has
    a workflow of its own, and the state-free path is not it."""

    client = ImageClient()
    with pytest.raises(ValidationError) as refused:
        build_page_update_preflight(
            client,
            "123",
            'alpha paragraph text here\n\n![](x.png)<!-- cfxmark:asset src="x.png" -->\n',
            body_format="md",
            asset_dir=tmp_path,
        )
    assert refused.value.context["reason"] == "managed_asset_requires_push_md"


def test_a_document_from_stdin_naming_a_local_file_says_which_flag_is_missing() -> None:
    """Standard input has no directory, and resolving against the shell's working
    directory would make the same command mean different things in different
    terminals."""

    with pytest.raises(ValidationError) as refused:
        build_page_update_preflight(
            ImageClient(),
            "123",
            "alpha paragraph text here\n\n![](diagram.png)\n",
            body_format="md",
            asset_dir=None,
        )
    assert refused.value.context["reason"] == "asset_dir_required"


# --------------------------------------------------------------------------
# What gets published, and what a dry run does not do
# --------------------------------------------------------------------------


def test_the_published_body_points_at_the_attachment(tmp_path: Path) -> None:
    """The reference is rewritten before the candidate is built, so what gets
    proved and published is the document naming the attachment."""

    (tmp_path / "img").mkdir()
    _write_png(tmp_path / "img")
    client = ImageClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "alpha paragraph text here\n\n![](img/diagram.png)\n",
        body_format="md",
        asset_dir=tmp_path,
    )
    publish_page_update(client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=())
    assert 'ri:filename="diagram.png"' in client.storage
    assert "img/diagram.png" not in client.storage


def test_a_dry_run_shows_the_plan_and_uploads_nothing(tmp_path: Path) -> None:
    _write_png(tmp_path)
    client = ImageClient()
    result = create_page_stateless(
        client,
        space="FIX",
        title="New",
        parent_id=None,
        body="alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        dry_run=True,
        accept_conversion=None,
        next_action_argv=(),
        asset_dir=tmp_path,
    )
    assert result["assets"]["upload"][0]["filename"] == "diagram.png"
    assert client.events == []


def test_an_https_image_still_publishes_untouched(tmp_path: Path) -> None:
    client = ImageClient()
    preflight = build_page_update_preflight(
        client,
        "123",
        "alpha paragraph text here\n\n![](https://example.invalid/x.png)\n",
        body_format="md",
        asset_dir=None,
    )
    publish_page_update(client, preflight, accept_migration=None, reason=None, minor_edit=False, next_action_argv=())
    assert client.uploads == []


# --------------------------------------------------------------------------
# Recovering a create whose pictures did not all land
# --------------------------------------------------------------------------


class FaithfulImageClient(ImageClient):
    """Records uploads as attachments, the way the server does.

    `ImageClient` reports an empty attachment list whatever it has been sent,
    which is fine for testing ordering and useless for testing recovery: a
    recovery that consults the page cannot be measured against a page that never
    remembers anything.
    """

    def __init__(self) -> None:
        super().__init__()
        self.attached: list[SimpleNamespace] = []

    def upload_attachment(self, page_id, path, *, filename=None, **kwargs):
        outcome = super().upload_attachment(page_id, path, filename=filename, **kwargs)
        self.attached.append(SimpleNamespace(title=filename))
        return outcome

    def list_attachments(self, page_id: str, limit: int | None = None) -> list:
        return list(self.attached)


def test_the_recovery_command_is_runnable_as_returned(tmp_path: Path) -> None:
    """The skill's central rule is to run `next_actions[].argv` exactly and never
    invent a command. An argv carrying `<the same file>` breaks that rule in the
    one place a caller is already in trouble."""

    _write_png(tmp_path)
    body = tmp_path / "body.md"
    body.write_text("alpha paragraph text here\n\n![](diagram.png)\n", encoding="utf-8")
    client = ImageClient()
    client.fail_upload_at = "diagram.png"

    result = create_page_stateless(
        client,
        space="FIX",
        title="New",
        parent_id=None,
        body=body.read_text(encoding="utf-8"),
        body_format="md",
        dry_run=False,
        accept_conversion=None,
        next_action_argv=(),
        asset_dir=tmp_path,
        body_source=str(body),
    )

    (action,) = result["next_actions"]
    argv = action["argv"]
    assert argv[:3] == ["confluence", "page", "recover-assets"]
    assert not [item for item in argv if item.startswith("<")]
    assert argv[argv.index("--body-file") + 1] == str(body)


def test_a_body_from_stdin_offers_no_recovery_it_cannot_name(tmp_path: Path) -> None:
    """No file to point at, so no action. A next action nobody can run is what
    this replaced."""

    _write_png(tmp_path)
    client = ImageClient()
    client.fail_upload_at = "diagram.png"
    result = create_page_stateless(
        client,
        space="FIX",
        title="New",
        parent_id=None,
        body="alpha paragraph text here\n\n![](diagram.png)\n",
        body_format="md",
        dry_run=False,
        accept_conversion=None,
        next_action_argv=(),
        asset_dir=tmp_path,
    )
    assert result["status"] == "created_with_missing_images"
    assert "next_actions" not in result


def test_recovery_uploads_what_is_missing_and_writes_no_body(tmp_path: Path) -> None:
    """Why it cannot be a rerun of the write: after a create the page body already
    *is* the candidate, so the update finds nothing to change, returns
    `no_change`, and never reaches the uploads. Measured -- the advice to rerun
    the write recovered nothing at all."""

    from atlassian_skills.confluence.local_assets import recover_assets

    _write_png(tmp_path)
    body = tmp_path / "body.md"
    body.write_text("alpha paragraph text here\n\n![](diagram.png)\n", encoding="utf-8")
    client = FaithfulImageClient()

    outcome = recover_assets(client, "123", body.read_text(encoding="utf-8"), base_dir=tmp_path)
    assert outcome["status"] == "recovered"
    assert outcome["uploaded"] == ["diagram.png"]
    assert "body" not in client.events


def test_running_recovery_twice_uploads_nothing_the_second_time(tmp_path: Path) -> None:
    """Idempotent by consequence rather than by a flag: what is already on the
    page is not sent again."""

    from atlassian_skills.confluence.local_assets import recover_assets

    _write_png(tmp_path)
    body = tmp_path / "body.md"
    body.write_text("alpha paragraph text here\n\n![](diagram.png)\n", encoding="utf-8")
    client = FaithfulImageClient()

    recover_assets(client, "123", body.read_text(encoding="utf-8"), base_dir=tmp_path)
    again = recover_assets(client, "123", body.read_text(encoding="utf-8"), base_dir=tmp_path)

    assert again["status"] == "already_complete"
    assert again["uploaded"] == []
    assert client.uploads == ["diagram.png"]
