"""Which local images a publish would upload, and everything it refuses to guess.

`![](diagram.png)` in a file appearing on the page is the thing people expect a
Markdown workflow to do, and `page create` / `page update` had no notion of a
local file at all -- the reference published as a broken link and nothing said so.

The tests are ordered by what they cost when wrong. Reading a file from outside
the base directory is worst and comes first. Publishing a broken image is second.
Uploading the same picture twice, or merging two different ones, is third.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlassian_skills.confluence.local_assets import (
    LocalAsset,
    file_sha256,
    plan_uploads,
    resolve_local_assets,
    rewrite_references,
)
from atlassian_skills.core.errors import ValidationError

# A one-pixel PNG. Real bytes, because the resolver reads and hashes the file and
# a text stand-in would not exercise that.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
OTHER_PNG = PNG[:-4] + b"\x00\x00\x00\x00"


def _write(directory: Path, name: str, data: bytes = PNG) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


def test_a_reference_above_the_base_directory_is_refused(tmp_path: Path) -> None:
    """The obvious escape. Without this, publishing a document reads whatever the
    author's relative path points at."""

    base = tmp_path / "docs"
    base.mkdir()
    _write(tmp_path, "secret.png")
    with pytest.raises(ValidationError) as refused:
        resolve_local_assets("![](../secret.png)", base_dir=base)
    assert refused.value.context["reason"] == "asset_outside_base"


def test_a_symlink_out_of_the_base_directory_is_refused(tmp_path: Path) -> None:
    """The quiet escape, and the reason the check runs on the resolved path
    rather than on the text of the reference. A reference reading `logo.png`
    looks entirely local."""

    base = tmp_path / "docs"
    base.mkdir()
    outside = _write(tmp_path, "outside.png")
    (base / "logo.png").symlink_to(outside)

    with pytest.raises(ValidationError) as refused:
        resolve_local_assets("![](logo.png)", base_dir=base)
    assert refused.value.context["reason"] == "asset_outside_base"


def test_a_symlink_inside_the_base_directory_is_fine(tmp_path: Path) -> None:
    """The check is about where the bytes are, not about symlinks being
    suspicious. Refusing these would break ordinary layouts."""

    base = tmp_path / "docs"
    _write(base / "img", "real.png")
    (base / "logo.png").symlink_to(base / "img" / "real.png")

    (asset,) = resolve_local_assets("![](logo.png)", base_dir=base)
    assert asset.sha256 == file_sha256(base / "img" / "real.png")


# --------------------------------------------------------------------------
# Refusing rather than publishing something broken
# --------------------------------------------------------------------------


def test_a_missing_file_is_refused_not_skipped(tmp_path: Path) -> None:
    """Skipping would publish a broken image and say nothing, and nobody finds
    that until they read the page."""

    with pytest.raises(ValidationError) as refused:
        resolve_local_assets("![](nope.png)", base_dir=tmp_path)
    assert refused.value.context["reason"] == "asset_missing"


def test_a_file_that_is_not_an_image_is_refused(tmp_path: Path) -> None:
    """A Markdown image reference pointing at something else is a mistake worth
    surfacing here, rather than guessing a type for the server to reject later."""

    _write(tmp_path, "notes.txt", b"hello")
    with pytest.raises(ValidationError) as refused:
        resolve_local_assets("![](notes.txt)", base_dir=tmp_path)
    assert refused.value.context["reason"] == "asset_media_type"


def test_remote_and_confluence_references_are_left_alone(tmp_path: Path) -> None:
    """Those already point at something that exists. Treating them as local files
    would refuse every document that links to an image on the web."""

    markdown = "![](https://example.invalid/a.png)\n![](cfx:existing.png)\n"
    assert resolve_local_assets(markdown, base_dir=tmp_path) == ()


# --------------------------------------------------------------------------
# Identity is content, not name
# --------------------------------------------------------------------------


def test_the_same_picture_referenced_twice_uploads_once(tmp_path: Path) -> None:
    _write(tmp_path, "a.png")
    _write(tmp_path, "b.png")
    assets = resolve_local_assets("![](a.png) and ![](b.png)", base_dir=tmp_path)
    plan = plan_uploads(assets, remote_hashes={})
    assert len(assets) == 2
    assert len(plan.upload) == 1


def test_two_different_pictures_with_one_name_are_kept_apart(tmp_path: Path) -> None:
    """Confluence keeps one flat attachment namespace per page, so publishing both
    under `diagram.png` would silently make one of them win."""

    _write(tmp_path / "one", "diagram.png", PNG)
    _write(tmp_path / "two", "diagram.png", OTHER_PNG)
    assets = resolve_local_assets("![](one/diagram.png) ![](two/diagram.png)", base_dir=tmp_path)
    assert len({asset.filename for asset in assets}) == 2


def test_an_unchanged_picture_already_on_the_page_is_reused(tmp_path: Path) -> None:
    """Re-uploading would create a new version of an unchanged file on every
    publish, and the page history would fill with edits nobody made."""

    path = _write(tmp_path, "a.png")
    assets = resolve_local_assets("![](a.png)", base_dir=tmp_path)
    plan = plan_uploads(assets, remote_hashes={"a.png": file_sha256(path)})
    assert plan.upload == ()
    assert len(plan.reuse) == 1


def test_a_changed_picture_under_the_same_name_is_uploaded(tmp_path: Path) -> None:
    _write(tmp_path, "a.png")
    assets = resolve_local_assets("![](a.png)", base_dir=tmp_path)
    plan = plan_uploads(assets, remote_hashes={"a.png": "0" * 64})
    assert len(plan.upload) == 1
    assert plan.reuse == ()


# --------------------------------------------------------------------------
# What the plan says, and what it never does
# --------------------------------------------------------------------------


def test_a_plan_never_deletes_anything(tmp_path: Path) -> None:
    """A reference disappearing means the document stopped pointing at a picture,
    not that the picture should go. Something else may link to it, and an
    attachment nobody can restore is worse than one nobody references.

    Stated as an explicit empty list rather than an absent key, so a reader does
    not have to wonder whether deletes are hidden elsewhere."""

    _write(tmp_path, "a.png")
    plan = plan_uploads(resolve_local_assets("![](a.png)", base_dir=tmp_path), remote_hashes={})
    assert plan.to_dict()["delete"] == []


def test_references_are_rewritten_to_the_names_they_will_carry(tmp_path: Path) -> None:
    _write(tmp_path / "img", "a.png")
    markdown = "before ![alt text](img/a.png) after"
    assets = resolve_local_assets(markdown, base_dir=tmp_path)
    assert rewrite_references(markdown, assets) == "before ![alt text](a.png) after"


def test_rewriting_leaves_everything_it_did_not_resolve(tmp_path: Path) -> None:
    """A document mixing local files, web images and Confluence markers must come
    out with only its local files touched."""

    _write(tmp_path, "a.png")
    markdown = "![](a.png) ![](https://example.invalid/b.png) ![](cfx:c.png)"
    rewritten = rewrite_references(markdown, resolve_local_assets(markdown, base_dir=tmp_path))
    assert "https://example.invalid/b.png" in rewritten
    assert "cfx:c.png" in rewritten


def test_a_url_encoded_reference_resolves_to_the_real_file(tmp_path: Path) -> None:
    """Markdown written by a tool commonly escapes spaces. Failing on those would
    refuse documents that are perfectly valid."""

    _write(tmp_path, "my diagram.png")
    (asset,) = resolve_local_assets("![](my%20diagram.png)", base_dir=tmp_path)
    assert asset.filename == "my diagram.png"


def test_the_plan_is_reportable_before_anything_is_uploaded(tmp_path: Path) -> None:
    """Planning and doing are separate so a dry run can show this, and so every
    refusal happens before a byte reaches the server."""

    path = _write(tmp_path, "a.png")
    plan = plan_uploads(resolve_local_assets("![](a.png)", base_dir=tmp_path), remote_hashes={})
    payload = plan.to_dict()
    assert payload["upload"][0]["filename"] == "a.png"
    assert payload["upload"][0]["sha256"] == file_sha256(path)
    assert payload["upload"][0]["media_type"] == "image/png"


def test_an_oversized_file_is_refused_with_its_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """So a mistyped path pointing at a disk image fails here, with something to
    act on, rather than after a long upload."""

    monkeypatch.setattr("atlassian_skills.confluence.local_assets.MAX_BYTES", 10)
    _write(tmp_path, "big.png")
    with pytest.raises(ValidationError) as refused:
        resolve_local_assets("![](big.png)", base_dir=tmp_path)
    assert refused.value.context["reason"] == "asset_too_large"
    assert refused.value.context["reference"] == "big.png"


def test_an_asset_carries_what_an_upload_needs(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.png")
    (asset,) = resolve_local_assets("![](a.png)", base_dir=tmp_path)
    assert isinstance(asset, LocalAsset)
    assert asset.path == path.resolve()
    assert asset.size == len(PNG)


# --------------------------------------------------------------------------
# Uploading, and what happens when it stops partway
# --------------------------------------------------------------------------


def test_nothing_is_reused_unless_a_hash_proves_it(tmp_path: Path) -> None:
    """Confluence returns a name, a media type and a byte count for an
    attachment, and none of those prove two files are the same.

    So the default reuses nothing. A redundant attachment version is visible in
    the page history; a stale image that was skipped because the name and size
    matched is not."""

    from atlassian_skills.confluence.local_assets import reusable_hashes

    class Client:
        def list_attachments(self, page_id: str, limit: int | None = None) -> list:
            raise AssertionError("must not read attachments to decide reuse")

    assert reusable_hashes(Client(), "123") == {}


def test_an_upload_that_fails_partway_says_what_already_landed(tmp_path: Path) -> None:
    """The caller has to be able to say "these three are on the page and the body
    does not reference them". Reporting only the failure would leave files
    stranded with nothing naming them."""

    from atlassian_skills.confluence.local_assets import (
        AssetUploadInterrupted,
        plan_uploads,
        upload_assets,
    )

    _write(tmp_path, "a.png", PNG)
    _write(tmp_path, "b.png", OTHER_PNG)
    plan = plan_uploads(resolve_local_assets("![](a.png) ![](b.png)", base_dir=tmp_path), remote_hashes={})

    class Client:
        def __init__(self) -> None:
            self.done: list[str] = []

        def list_attachments(self, page_id, limit=None):
            return []

        def upload_attachment(self, page_id, path, *, filename=None, **kwargs):
            if len(self.done) == 1:
                raise RuntimeError("network went away")
            self.done.append(filename)

    with pytest.raises(AssetUploadInterrupted) as interrupted:
        upload_assets(Client(), "123", plan)
    assert len(interrupted.value.uploaded) == 1
    assert interrupted.value.failed


def test_a_clean_upload_reports_what_landed_and_what_was_reused(tmp_path: Path) -> None:
    from atlassian_skills.confluence.local_assets import plan_uploads, upload_assets

    path = _write(tmp_path, "a.png")
    _write(tmp_path, "b.png", OTHER_PNG)
    plan = plan_uploads(
        resolve_local_assets("![](a.png) ![](b.png)", base_dir=tmp_path),
        remote_hashes={"a.png": file_sha256(path)},
    )

    class Client:
        def list_attachments(self, page_id, limit=None):
            return []

        def upload_attachment(self, page_id, path, *, filename=None, **kwargs):
            return {"title": filename}

    outcome = upload_assets(Client(), "123", plan)
    assert outcome.uploaded == ("b.png",)
    assert outcome.reused == ("a.png",)
    assert outcome.orphaned == ()


def test_a_stored_name_carries_its_id_and_a_new_one_does_not(tmp_path: Path) -> None:
    """Which endpoint each file goes to, decided per file.

    An id means "post a version of this stored attachment"; no id means "create". The
    page's own list is what tells them apart, and sending a stored name to the create
    endpoint is answered `400` by Server/DC -- so applying either decision to every
    file breaks one of the two cases.
    """

    from atlassian_skills.confluence.local_assets import plan_uploads, upload_assets

    _write(tmp_path, "stored.png", PNG)
    _write(tmp_path, "fresh.png", OTHER_PNG)
    plan = plan_uploads(resolve_local_assets("![](stored.png) ![](fresh.png)", base_dir=tmp_path), remote_hashes={})

    class Client:
        def __init__(self) -> None:
            self.calls: dict[str, str | None] = {}

        def list_attachments(self, page_id, limit=None):
            raise AssertionError("the caller passed `stored`; this must not be read again")

        def upload_attachment(self, page_id, path, *, filename=None, attachment_id=None, **kwargs):
            self.calls[str(filename)] = attachment_id
            return {"title": filename}

    client = Client()
    upload_assets(client, "123", plan, stored={"stored.png": "att-3"})

    assert client.calls == {"stored.png": "att-3", "fresh.png": None}


def test_nothing_to_upload_reads_nothing(tmp_path: Path) -> None:
    """The read is only owed when there is a file to place."""

    from atlassian_skills.confluence.local_assets import AssetUploadPlan, upload_assets

    class Client:
        def list_attachments(self, page_id, limit=None):
            raise AssertionError("an empty plan must not cost a round trip")

    outcome = upload_assets(Client(), "123", AssetUploadPlan(upload=(), reuse=()))
    assert outcome.uploaded == ()
    assert outcome.orphaned == ()


def test_the_state_free_path_never_reuses_an_attachment_it_already_has(tmp_path: Path) -> None:
    """Measured, because the skill now tells people this and a claim in shipped
    guidance has to be checked rather than reasoned about.

    Confluence returns a name, a media type and a byte count for an attachment,
    and none of those prove two files are the same -- so `reusable_hashes` is
    empty even when the page already holds the identical picture, and every
    publish uploads it again. That is the cost of the state-free path, and the
    reason a page edited more than once belongs on the managed one.
    """

    from atlassian_skills.confluence.local_assets import reusable_hashes

    class PageThatAlreadyHasIt:
        def list_attachments(self, page_id: str, limit: int | None = None) -> list:
            return [{"title": "a.png"}]

    _write(tmp_path, "a.png")
    assets = resolve_local_assets("![](a.png)", base_dir=tmp_path)
    plan = plan_uploads(assets, remote_hashes=reusable_hashes(PageThatAlreadyHasIt(), "123"))

    assert [item.filename for item in plan.upload] == ["a.png"]
    assert plan.reuse == ()


# --------------------------------------------------------------------------
# A fenced block is documentation about an image, not an image
# --------------------------------------------------------------------------


def test_an_image_inside_a_code_fence_is_not_an_asset_reference(tmp_path: Path) -> None:
    """Found on a live corpus page that documents the image workflow and so contains an example.

    The document shows a reader how to reference a picture:

        ```
        ![diagram description](assets/diagram.png)
        ```

    There is no such file and there was never meant to be. The extractor read the
    example as a real reference and refused the publish with `asset_missing`, so a
    document explaining how images work could not be published *because* it explained
    it. The census then reported it as corpus damage.

    cfxmark already learned this in a different place -- a manifest inside a fence is
    documentation about a manifest -- and the fix is the same shape: blank the fences
    before matching.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    markdown = (
        "Put the file under `assets/`, then reference it:\n\n"
        "```\n"
        "![diagram description](assets/diagram.png)\n"
        "```\n\n"
        "That is all.\n"
    )
    assert resolve_local_assets(markdown, base_dir=tmp_path) == ()


def test_a_real_reference_beside_a_fenced_example_still_resolves(tmp_path: Path) -> None:
    """The discriminating case, and the reason the fix cannot be "ignore any document
    containing a fence"."""

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "real.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    markdown = "![the actual picture](assets/real.png)\n\n```\n![diagram description](assets/diagram.png)\n```\n"
    resolved = resolve_local_assets(markdown, base_dir=tmp_path)
    assert [asset.reference for asset in resolved] == ["assets/real.png"]


def test_an_indented_code_block_is_also_not_a_reference(tmp_path: Path) -> None:
    """Four-space indentation is the other spelling of the same thing."""

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    markdown = "Example:\n\n    ![diagram description](assets/diagram.png)\n\nDone.\n"
    assert resolve_local_assets(markdown, base_dir=tmp_path) == ()


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        # R4-pre round 2's reproduction. A three-backtick line inside a four-backtick
        # block is code, not a close. Tracking only "am I in a fence" read it as a close:
        # the inner example became live, so it could be rejected as a missing asset or
        # rewritten inside the fence that was displaying it, and the real reference after
        # the actual close was hidden instead. Content corruption, not a bad count.
        (
            "a narrower fence inside a wider one",
            "````\n```\n![inner](assets/inner.png)\n```\n````\n\n![real](assets/real.png)\n",
        ),
        ("tilde inside backtick", "```\n~~~\n![inner](assets/inner.png)\n~~~\n```\n\n![real](assets/real.png)\n"),
        ("backtick inside tilde", "~~~\n```\n![inner](assets/inner.png)\n```\n~~~\n\n![real](assets/real.png)\n"),
        (
            "a language tag on the opener",
            "```markdown\n![inner](assets/inner.png)\n```\n\n![real](assets/real.png)\n",
        ),
        (
            "CRLF throughout",
            "````\r\n```\r\n![inner](assets/inner.png)\r\n```\r\n````\r\n\r\n![real](assets/real.png)\r\n",
        ),
        (
            "a tab-indented example after the fence closes",
            "```\n![inner](assets/inner.png)\n```\n\n\t![tabbed](assets/tabbed.png)\n\n![real](assets/real.png)\n",
        ),
    ],
)
def test_only_the_real_reference_survives_mixed_code_shapes(shape: str, body: str, tmp_path: Path) -> None:
    from atlassian_skills.confluence.local_assets import resolve_local_assets, rewrite_references

    (tmp_path / "assets").mkdir()
    for name in ("inner.png", "real.png", "tabbed.png"):
        (tmp_path / "assets" / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    resolved = resolve_local_assets(body, base_dir=tmp_path)
    assert [asset.reference for asset in resolved] == ["assets/real.png"], shape

    # And the rewrite must touch that one and nothing else, so every example comes out
    # of the document exactly as the author wrote it.
    rewritten = rewrite_references(body, resolved)
    assert "![inner](assets/inner.png)" in rewritten, shape
    assert "![real](assets/real.png)" not in rewritten, shape
    if "tabbed" in body:
        assert "![tabbed](assets/tabbed.png)" in rewritten, shape


def test_a_tab_indented_fence_line_is_indented_code_not_a_fence(tmp_path: Path) -> None:
    """R4-pre round 3's reproduction.

    A tab at the start of a line is four columns, so it is an indented code block, and an
    indented code block cannot open a fenced one. The matcher's prefix was `\\s{0,3}`,
    which accepts a tab — so this opened a fence that could never close, blanked to end of
    file, and hid every real reference after it. The same author-file corruption class as
    round 2's finding: an existing image silently drops out of discovery and can then
    neither be rewritten nor uploaded.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets, rewrite_references

    (tmp_path / "assets").mkdir()
    for name in ("example.png", "real.png"):
        (tmp_path / "assets" / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    body = "\t```\n\t![example inside indented code](assets/example.png)\n![real picture](assets/real.png)\n"

    resolved = resolve_local_assets(body, base_dir=tmp_path)
    assert [asset.reference for asset in resolved] == ["assets/real.png"]

    rewritten = rewrite_references(body, resolved)
    assert "![example inside indented code](assets/example.png)" in rewritten
    assert "![real picture](assets/real.png)" not in rewritten


def test_up_to_three_spaces_still_opens_a_fence(tmp_path: Path) -> None:
    """The discriminating half: spaces are fine, and three of them are still a fence.

    Without this the repair could have been "no indentation at all", which would expose
    every example in a nested list.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "real.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    body = "   ```\n   ![inner](assets/inner.png)\n   ```\n\n![real](assets/real.png)\n"
    assert [a.reference for a in resolve_local_assets(body, base_dir=tmp_path)] == ["assets/real.png"]


def test_a_misaligned_blanking_refuses_instead_of_asserting(monkeypatch, tmp_path: Path) -> None:
    """The invariant cannot be reached by any input, so it is forced.

    R4-pre round 3 asked for this conversion and was right about why: `python -O` removes
    assertions, so the build where a corrupting rewrite matters most is the build without
    the check. It also noted the invariant is not sufficient on its own — it held while the
    blanked view had entirely wrong semantics. It stays as defence in depth, and what this
    pins is that it is a typed, catchable refusal on a path that rewrites author files
    rather than an implementation assertion escaping to the caller.
    """

    from atlassian_skills.confluence import local_assets

    monkeypatch.setattr(local_assets.re, "sub", lambda *args, **kwargs: "")

    with pytest.raises(ValidationError) as caught:
        local_assets.resolve_local_assets("![x](assets/x.png)\n", base_dir=tmp_path)
    assert caught.value.context["reason"] == "code_blanking_offset_mismatch"
    # And it says the document was left alone, which is the fact a caller needs.
    assert "not modified" in (caught.value.hint or "")


def test_an_ordinary_top_level_fence_is_still_read_as_code(tmp_path: Path) -> None:
    """The plain case, kept alongside the container ones so a tokenizer regression that
    only broke the simple shape would still be caught."""

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "real.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    body = "```\n![example](assets/example.png)\n```\n\n![real](assets/real.png)\n"
    assert [a.reference for a in resolve_local_assets(body, base_dir=tmp_path)] == ["assets/real.png"]


@pytest.mark.parametrize(
    ("shape", "body"),
    [
        # R4-pre round 4's reproduction, and the shapes the line scanner refused because it
        # could not see them. A real tokenizer can.
        ("a block quote", "> ```\n> ![example](assets/example.png)\n> ```\n\n![real](assets/real.png)\n"),
        ("a bulleted list item", "- ```\n  ![example](assets/example.png)\n  ```\n\n![real](assets/real.png)\n"),
        ("a numbered list item", "1. ```\n   ![example](assets/example.png)\n   ```\n\n![real](assets/real.png)\n"),
        (
            "a quote inside a list item",
            "- > ```\n  > ![example](assets/example.png)\n  > ```\n\n![real](assets/real.png)\n",
        ),
    ],
)
def test_a_container_nested_fence_is_read_as_code(shape: str, body: str, tmp_path: Path) -> None:
    """The refusal becomes a correct answer.

    Rounds 2, 3 and 4 each found a different piece of Markdown the hand-written line
    scanner did not know -- fence width and delimiter, indentation columns, container
    prefixes. Rather than a fourth regular expression, the code ranges now come from the
    same tokenizer cfxmark already parses Markdown with, which knows all three by
    construction.

    So these shapes no longer refuse: the example inside the container is code, the
    reference after it is real, and the rewrite touches only the second.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets, rewrite_references

    (tmp_path / "assets").mkdir()
    for name in ("example.png", "real.png"):
        (tmp_path / "assets" / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    resolved = resolve_local_assets(body, base_dir=tmp_path)
    assert [asset.reference for asset in resolved] == ["assets/real.png"], shape

    rewritten = rewrite_references(body, resolved)
    assert "![example](assets/example.png)" in rewritten, shape
    assert "![real](assets/real.png)" not in rewritten, shape


@pytest.mark.xfail(
    reason=(
        "mistletoe reads ```md as closing an open ``` fence. CommonMark says a closing fence "
        "carries no info string, so it is content. Marked rather than patched: adding fence "
        "tracking back beside the tokenizer re-imports the container bug the tokenizer was "
        "brought in to fix -- measured, that union breaks the list-item cases."
    ),
    strict=True,
)
def test_an_equal_width_inner_opener_with_an_info_string_is_content(tmp_path: Path) -> None:
    """The one shape the tokenizer reads differently from CommonMark.

    A strict xfail so it announces itself the day mistletoe changes or is replaced, instead
    of sitting in a document nobody rereads. The consequence if it bites is the R4-2 class:
    the example inside is exposed and the real reference after it is hidden.
    """

    from atlassian_skills.confluence.local_assets import resolve_local_assets

    (tmp_path / "assets").mkdir()
    for name in ("inner.png", "real.png"):
        (tmp_path / "assets" / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    body = "```\n```md\n![inner](assets/inner.png)\n```\n\n![real](assets/real.png)\n"

    assert [a.reference for a in resolve_local_assets(body, base_dir=tmp_path)] == ["assets/real.png"]
