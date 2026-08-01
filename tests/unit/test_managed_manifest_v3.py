"""Manifest v3, and the two directions of the version boundary.

§6.3 asks for one fixture in particular: a v3 reader reading v2, and a v2 reader
refusing v3. The second half is the awkward one, because this build has no v2 reader
any more — so it is reconstructed from the field set v2 actually had, which is what a
v2 reader would have compared against. That makes the test about the format rather
than about a class that no longer exists.

The version is read before anything else, and that ordering is the substance. Before
it, a document from a newer writer was reported as carrying an unknown field
`authority` — the same answer a corrupt document gets, and the opposite of the useful
one. A user seeing "unknown field" edits the file; a user seeing "newer version"
upgrades.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlassian_skills.core.managed_manifest import (
    CURRENT_MANAGED_MANIFEST_VERSION,
    SUPPORTED_MANAGED_MANIFEST_VERSIONS,
    ManagedManifestError,
    parse_managed_manifest,
    serialize_managed_manifest,
)

#: The field set v2 shipped with, spelled out rather than imported, because the point
#: is what a v2 READER would have required and that reader is gone.
V2_FIELDS = (
    "v",
    "page",
    "site",
    "remote_version",
    "remote_storage",
    "base_md",
    "assets",
    "converter",
    "profile",
    "passthrough",
)

HASH = "sha256:" + "a" * 64


def _manifest(version: int, *, authority: str | None = "md", profile: str = "markdown-first") -> str:
    fields = [
        ("v", str(version)),
        ("page", "123"),
        ("site", HASH),
        ("remote_version", "7"),
        ("remote_storage", HASH),
        ("base_md", HASH),
    ]
    if authority is not None:
        fields.append(("authority", authority))
    fields += [
        ("assets", HASH),
        ("converter", "cfxmark/0.5.2"),
        ("profile", profile),
        ("passthrough", "-"),
    ]
    payload = " ".join(f"{name}={value}" for name, value in fields)
    return f"<!-- atls:managed {payload} -->\n\nbody text\n"


def test_a_v3_reader_reads_a_v2_document() -> None:
    """The whole point of a version field. A v2 document keeps working, and the
    authority it never carried defaults to what it always meant."""

    manifest = parse_managed_manifest(_manifest(2, authority=None))
    assert manifest.v == 2
    assert manifest.authority == "md"


def test_a_v2_reader_would_refuse_a_v3_document() -> None:
    """§6.3's other direction, reconstructed from the format.

    A v2 reader compared the field names against its own tuple and refused anything
    else. v3 adds `authority`, so that comparison fails — which is the contract:
    an old build does not silently ignore a field it does not understand.
    """

    line = _manifest(CURRENT_MANAGED_MANIFEST_VERSION).splitlines()[0]
    names = tuple(item.split("=", 1)[0] for item in line[len("<!-- atls:managed ") : -len(" -->")].split(" "))

    assert names != V2_FIELDS
    assert set(names) - set(V2_FIELDS) == {"authority"}


def test_a_newer_version_is_refused_by_name_and_says_what_is_supported() -> None:
    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION + 1))
    assert refused.value.reason == "managed_manifest_newer_version"
    assert refused.value.context["supported"] == list(SUPPORTED_MANAGED_MANIFEST_VERSIONS)


def test_a_newer_version_refusal_does_not_echo_the_document() -> None:
    """§6.3's fourth bullet. A refusal that quotes what it could not read puts page
    content into a log, and the caller cannot act on it either way."""

    document = _manifest(CURRENT_MANAGED_MANIFEST_VERSION + 1).replace("body text", "confidential prose")
    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(document)
    assert "confidential prose" not in str(refused.value)
    assert "confidential prose" not in str(refused.value.context)


def test_a_version_below_the_supported_floor_is_a_different_refusal() -> None:
    """Older and newer are not the same problem and do not get the same answer.
    One means repair or re-pull; the other means upgrade."""

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(_manifest(1, authority=None))
    assert refused.value.reason != "managed_manifest_newer_version"


def test_the_version_is_read_before_the_field_set_is_judged() -> None:
    """The ordering, asserted directly.

    A v4 document also has a field set this build does not know. If the field check
    came first it would win, and the reason would be `unknown_manifest_field` — which
    is what it used to be.
    """

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION + 1) + "")
    assert refused.value.reason == "managed_manifest_newer_version"

    # And a v3 document with a genuinely unknown field still reports that, so the
    # version check has not swallowed the field check.
    line, _, rest = _manifest(CURRENT_MANAGED_MANIFEST_VERSION).partition("\n")
    tampered = line.replace(" passthrough=-", " passthrough=- surprise=1") + "\n" + rest
    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(tampered)
    assert refused.value.reason == "unknown_manifest_field"
    assert refused.value.context["fields"] == ["surprise"]


def test_a_profile_that_declares_a_different_authority_is_refused() -> None:
    """§6.2's contradictory-field rule.

    `xhtml-exact` says the exact XHTML publishes; `authority=md` says the Markdown
    does. Whichever field the code reads decides what gets sent, so the document is
    refused rather than ranked.
    """

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION, profile="xhtml-exact"))
    assert refused.value.reason == "managed_manifest_self_contradictory"
    assert refused.value.context["profile_implies"] == "xhtml"


def test_a_profile_nobody_has_registered_is_unknown_rather_than_contradictory() -> None:
    """An unknown profile is not evidence of a contradiction, and refusing it as one
    would reject a document a newer writer produced legitimately. The version check is
    what guards that case."""

    manifest = parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION, profile="some-future-profile"))
    assert manifest.profile == "some-future-profile"


def test_an_authority_managed_markdown_does_not_admit_is_refused() -> None:
    """§6.1: managed Markdown allows `md` and nothing else. The exact-XHTML workflow
    keeps its authority in its own sidecar, because inline metadata inside XHTML would
    change the bytes that get published."""

    with pytest.raises(ManagedManifestError) as refused:
        parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION, authority="xhtml"))
    assert refused.value.reason == "invalid_managed_authority"


def test_a_v3_manifest_round_trips_through_the_serializer() -> None:
    parsed = parse_managed_manifest(_manifest(CURRENT_MANAGED_MANIFEST_VERSION))
    assert serialize_managed_manifest(parsed) == _manifest(CURRENT_MANAGED_MANIFEST_VERSION).splitlines()[0]


def test_a_v2_manifest_serializes_back_as_v2() -> None:
    """Reading a v2 document does not silently promote it.

    The version moves on a successful pull, push or record -- steps that have re-read
    the remote and can prove the new baseline. A serializer called in passing has
    proved nothing, so it writes back what it was given.
    """

    parsed = parse_managed_manifest(_manifest(2, authority=None))
    written = serialize_managed_manifest(parsed)
    assert " v=2 " in written
    assert "authority=" not in written


def test_the_writers_that_may_promote_a_document_are_the_ones_the_plan_names() -> None:
    """A source scan, because "which code may move a document to v3" is a policy and
    policies drift.

    §6.3 gives the promotion to a successful pull, push or record. If a fifth site
    starts writing the current version, this names it and somebody has to say why.

    The four, and which of the three each one is:

        managed_pull.py    the pull
        body_write.py      the push, after the readback agreed
        reconcile.py       the record -- and `rebaseline`, which moves a baseline and
                           is the same act of rebinding under a different name
        prepare_merge.py   `finalize-merge`, the record's hidden predecessor. It stays
                           for one release and is expected to leave this list with it.
    """

    root = Path(__file__).resolve().parents[2] / "src" / "atlassian_skills"
    pattern = re.compile(r"v=CURRENT_MANAGED_MANIFEST_VERSION")
    writers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    )
    assert writers == [
        "confluence/body_write.py",
        "confluence/managed_pull.py",
        "confluence/prepare_merge.py",
        "confluence/reconcile.py",
    ], writers
