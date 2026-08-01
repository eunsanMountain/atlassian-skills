"""Where the merge base comes from, in the order §5.4 fixes.

The base of a three-way merge is the projection of the storage the local file was
last bound to. Until now the only place to get it was a sidecar written at pull time
— a full copy of the Markdown, kept beside every managed document forever, which
every repository then had to decide how to ignore.

It was never the only place. The manifest names the remote version, and Confluence
still has that version's storage. So the order is:

    1  Confluence historical storage      the server already has it
    2  a verified base cache              a sidecar, if one is lying around
    3  a base file the caller names       from Git, from a backup, from anywhere
    4  no base                            two-way compare, and say so

## Availability and integrity are not the same failure

This is the distinction the module is built around, and it decides whether a step
falls through to the next one or stops the command.

**Availability**: the server will not hand the version over — no permission, or the
space no longer retains it. Nothing about the local file is in doubt, so the next
source is worth trying, and each cause gets its own reason so the user knows whether
to ask an administrator or reach for a cache.

**Integrity**: the server handed something over and it is not what the manifest
says it should be. The recorded binding is wrong, and a cache is not more
trustworthy than the server — it was written by the same code from the same source.
So these stop, and they stop with a name:

    historical_storage_hash_mismatch     the version is there and its bytes differ
    historical_converter_mismatch        recorded under a converter we are not running
    historical_profile_mismatch          recorded under a different profile
    manifest_base_projection_mismatch    everything matches and the projection does
                                         not reproduce base_md
    converter_nondeterministic           the same input projected twice, differently

The last two are §5.4's closing paragraph and they are deliberately separated. A
projection that does not reproduce `base_md` while the storage and converter agree
means the converter's output moved under a fixed name; falling back to a two-way
compare would hide that behind "could not find the base". Only `rebaseline`, with an
explicit approval fingerprint, recovers from it. `converter_nondeterministic` has no
escape at all: a converter that disagrees with itself cannot be reasoned about, and
opening a route around it would be inviting someone to publish through it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import cfxmark

from atlassian_skills.core.errors import AtlasError, ValidationError
from atlassian_skills.core.managed_manifest import (
    ManagedManifest,
    canonical_content_sha256,
    extract_asset_records,
    parse_managed_document,
)

#: The profile the manifest records for a managed Markdown document, and the one this
#: build projects with. A manifest naming anything else was written by a build whose
#: output we cannot reproduce.
MANAGED_PROFILE = "markdown-first"

#: The profile cfxmark is actually asked for. Distinct from the manifest's `profile`
#: on purpose -- one records the workflow, the other selects converter behaviour --
#: and conflating them is why the mismatch check has to name which one it means.
PROJECTION_PROFILE: Literal["legacy", "editable", "readable"] = "editable"

#: Reasons that stop the command. Availability reasons are not here; they fall
#: through to the next source.
FAIL_CLOSED_REASONS = frozenset(
    {
        "historical_storage_hash_mismatch",
        "historical_converter_mismatch",
        "historical_profile_mismatch",
        "manifest_base_projection_mismatch",
        "converter_nondeterministic",
    }
)


@dataclass(frozen=True)
class BaseResolution:
    """The base, or a named account of why there is none."""

    markdown: str | None
    #: `history`, `cache`, `user_file`, or `unavailable`.
    source: str
    #: Why the sources ahead of this one did not answer, in the order they were
    #: tried. Present even on success, because "the cache was stale and history
    #: worked" is worth reporting.
    attempts: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.markdown is not None


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def project(storage: str, manifest: ManagedManifest) -> str:
    """Storage to Markdown, with the passthrough prefixes the document records.

    One function so the base projection and the remote projection cannot drift
    apart. A base produced by different options is not a base.
    """

    return cfxmark.strip_header_notice(
        cfxmark.to_md_artifact(
            storage,
            options=cfxmark.ConversionOptions(
                profile=PROJECTION_PROFILE,
                passthrough_html_comment_prefixes=manifest.passthrough,
            ),
        ).markdown
    )


def _classify_history_error(error: Exception) -> tuple[str, str]:
    """Which availability failure this is, keyed on the exit code atls documents.

    §15.4 fixes 2 as not-found and 3 as permission, so the code is the interface and
    the message is not. Collapsing them would lose the only thing the user can act
    on: a retention policy means reach for a cache, a permission refusal means ask an
    administrator, and a page that was deleted means the binding is dead.
    """

    exit_code = getattr(error, "exit_code", None)
    if exit_code == 3:
        return "history_permission_denied", "the account may not read historical versions"
    if exit_code == 2:
        return "history_version_missing", "the space no longer retains that version"
    return "history_unavailable", f"{type(error).__name__}: {error}"


def _fail(reason: str, message: str, **context: Any) -> ValidationError:
    return ValidationError(message, context={"reason": reason, **context})


def resolve_from_history(client: Any, page_id: str, manifest: ManagedManifest) -> BaseResolution:
    """§5.4 step 1. Ask the server for the version the manifest names."""

    fetch = getattr(client, "get_page_history", None)
    if fetch is None:
        # A client that cannot read history at all -- an older client object, or a
        # caller that built its own. Named rather than raised, because it is an
        # availability fact like any other and the next source may still answer. It is
        # not silent: the reason travels in the resolution and into the attempts list,
        # so a genuinely mis-wired client shows up as this reason rather than as a
        # base that quietly came from somewhere else.
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="history_unsupported",
            detail={"client": type(client).__name__},
        )
    try:
        historical = fetch(page_id, manifest.remote_version)
    except AtlasError as error:
        reason, detail = _classify_history_error(error)
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason=reason,
            detail={"version": manifest.remote_version, "detail": detail},
        )

    storage = getattr(historical, "body_storage", None) or ""
    observed = _sha256(storage)
    if observed != manifest.remote_storage:
        # The version number matched and the bytes did not. A page can be restored,
        # moved, or have its history rewritten, and then version 7 is no longer the
        # version this file was bound to. Merging against it produces a document that
        # reads perfectly and is wrong.
        raise _fail(
            "historical_storage_hash_mismatch",
            "The stored version this file is bound to no longer holds the bytes it was bound to.",
            page_id=page_id,
            version=manifest.remote_version,
            recorded=manifest.remote_storage,
            observed=observed,
        )

    running = f"cfxmark/{cfxmark.__version__}"
    if manifest.converter != running:
        # §16 forbids installing an old converter to try. Without the converter that
        # produced it, the projection is not reproducible, and a base that cannot be
        # reproduced is not a base.
        raise _fail(
            "historical_converter_mismatch",
            "This file was bound under a converter that is not the one running.",
            recorded=manifest.converter,
            running=running,
        )
    if manifest.profile != MANAGED_PROFILE:
        raise _fail(
            "historical_profile_mismatch",
            "This file was bound under a different profile.",
            recorded=manifest.profile,
            running=MANAGED_PROFILE,
        )

    markdown = project(storage, manifest)
    if canonical_content_sha256(markdown) == manifest.base_md:
        return BaseResolution(markdown=markdown, source="history")

    # Everything checked out and the projection still does not reproduce `base_md`.
    # Before blaming the manifest, ask whether the converter is deterministic: the
    # same input, the same options, the same process, twice.
    again = project(storage, manifest)
    if canonical_content_sha256(again) != canonical_content_sha256(markdown):
        raise _fail(
            "converter_nondeterministic",
            "The converter produced two different projections of the same storage.",
            page_id=page_id,
            version=manifest.remote_version,
        )
    raise _fail(
        "manifest_base_projection_mismatch",
        "The recorded base hash is not what this converter produces from the stored version.",
        page_id=page_id,
        version=manifest.remote_version,
        recorded=manifest.base_md,
        observed=canonical_content_sha256(markdown),
    )


def resolve_from_cache(cache_path: Path, manifest: ManagedManifest, *, page_id: str) -> BaseResolution:
    """§5.4 step 2. A sidecar, checked against every field before it is believed.

    Found automatically and with no flag, deliberately. §5.4's own reasoning: a user
    upgrading with a sidecar already beside their document should not have a valid
    base silently ignored, and the sidecar is checked on every field anyway, so there
    is nothing a flag would add.
    """

    from atlassian_skills.confluence.sidecar import SidecarUnusable, read_sidecar

    try:
        sidecar = read_sidecar(cache_path, page_id=page_id)
    except SidecarUnusable as unusable:
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason=f"cache_{unusable.reason}",
            detail={"path": str(cache_path)},
        )

    # Typed as strings so one dict can hold a version pair and three hash pairs.
    # The values are only ever reported, never compared again.
    mismatches: dict[str, tuple[str, str]] = {}
    if sidecar.remote_version != manifest.remote_version:
        mismatches["remote_version"] = (str(sidecar.remote_version), str(manifest.remote_version))
    if sidecar.remote_storage_sha256 != manifest.remote_storage:
        mismatches["remote_storage"] = (sidecar.remote_storage_sha256, manifest.remote_storage)
    if sidecar.site != manifest.site:
        mismatches["site"] = (sidecar.site, manifest.site)
    # §5.4 step 2 lists seven fields and these two were missing, which review R2
    # reproduced: a sidecar naming a different converter was accepted as a verified
    # base. It matters for the same reason the history path checks them -- a base
    # produced by a converter we are not running is not reproducible, so it is not a
    # base, however well its hashes line up.
    #
    # The sidecar spells them differently from the manifest ("cfxmark 0.6.0" against
    # "cfxmark/0.6.0", "editable" against "markdown-first") because they record
    # different things: the sidecar records the conversion options it used, the
    # manifest records the workflow. So the comparison is against what a sidecar
    # written by THIS build would say, not against the manifest's spelling.
    expected_converter = f"cfxmark {cfxmark.__version__}"
    if sidecar.converter != expected_converter:
        mismatches["converter"] = (sidecar.converter, expected_converter)
    if sidecar.profile != PROJECTION_PROFILE:
        mismatches["profile"] = (sidecar.profile, PROJECTION_PROFILE)
    base = cfxmark.strip_header_notice(sidecar.base_markdown)
    if canonical_content_sha256(base) != manifest.base_md:
        mismatches["base_md"] = (canonical_content_sha256(base), manifest.base_md)

    if mismatches:
        # Reported and not used. A stale sidecar is not a defect -- it is what an
        # upgraded document looks like -- but it is also not a base.
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="cache_disagrees_with_manifest",
            detail={"path": str(cache_path), "fields": sorted(mismatches)},
        )
    return BaseResolution(markdown=base, source="cache", detail={"path": str(cache_path)})


def resolve_from_file(base_path: Path, manifest: ManagedManifest, *, page_id: str) -> BaseResolution:
    """§5.4 step 3. A managed document the caller found, wherever they found it.

    The CLI takes a path and nothing else. Searching Git for a previous revision is
    the Skill's job: it knows what the user is working in, and a CLI guessing at
    history would be choosing a base on the user's behalf.
    """

    try:
        text = base_path.read_text(encoding="utf-8")
    except OSError as error:
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="base_file_unreadable",
            detail={"path": str(base_path), "detail": str(error)},
        )
    try:
        document = parse_managed_document(
            text, assets=extract_asset_records(text), verify_content=False, verify_assets=False
        )
    except Exception as error:  # noqa: BLE001 - any parse failure means the same thing
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="base_file_not_managed",
            detail={"path": str(base_path), "detail": type(error).__name__},
        )

    other = document.manifest
    if (other.page, other.site, other.remote_version, other.remote_storage) != (
        manifest.page,
        manifest.site,
        manifest.remote_version,
        manifest.remote_storage,
    ):
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="base_file_binds_elsewhere",
            detail={"path": str(base_path), "page_id": page_id},
        )
    body = cfxmark.strip_header_notice(document.content)
    if canonical_content_sha256(body) != other.base_md:
        # A managed file whose own body no longer matches its own base hash has been
        # edited since it was pulled. That is a legitimate state for a working file
        # and disqualifying as a base.
        return BaseResolution(
            markdown=None,
            source="unavailable",
            reason="base_file_edited_since_pull",
            detail={"path": str(base_path)},
        )
    return BaseResolution(markdown=body, source="user_file", detail={"path": str(base_path)})


def resolve_base(
    client: Any,
    page_id: str,
    manifest: ManagedManifest,
    *,
    cache_path: Path | None = None,
    base_file: Path | None = None,
) -> BaseResolution:
    """§5.4, in order, accumulating why each source did not answer.

    Integrity failures propagate out of here as `ValidationError`; availability
    failures are recorded and the next source is tried. The caller gets either a base
    or a resolution that says, source by source, why there is none.
    """

    attempts: list[dict[str, Any]] = []

    resolution = resolve_from_history(client, page_id, manifest)
    if resolution.available:
        return BaseResolution(
            markdown=resolution.markdown, source="history", attempts=tuple(attempts), detail=resolution.detail
        )
    attempts.append({"source": "history", "reason": resolution.reason, **resolution.detail})

    if cache_path is not None and cache_path.exists():
        resolution = resolve_from_cache(cache_path, manifest, page_id=page_id)
        if resolution.available:
            return BaseResolution(
                markdown=resolution.markdown, source="cache", attempts=tuple(attempts), detail=resolution.detail
            )
        attempts.append({"source": "cache", "reason": resolution.reason, **resolution.detail})

    if base_file is not None:
        resolution = resolve_from_file(base_file, manifest, page_id=page_id)
        if resolution.available:
            return BaseResolution(
                markdown=resolution.markdown,
                source="user_file",
                attempts=tuple(attempts),
                detail=resolution.detail,
            )
        attempts.append({"source": "user_file", "reason": resolution.reason, **resolution.detail})

    # §5.4 step 4. Not an error: the caller gets `L` and `R` and an explicit
    # statement that there is no base, and automatic merge and automatic record are
    # both closed off. What it must never be is a base nobody verified.
    return BaseResolution(
        markdown=None,
        source="unavailable",
        attempts=tuple(attempts),
        reason=attempts[0]["reason"] if attempts else "base_unavailable",
        detail={"tried": [item["source"] for item in attempts]},
    )


__all__ = [
    "FAIL_CLOSED_REASONS",
    "MANAGED_PROFILE",
    "PROJECTION_PROFILE",
    "BaseResolution",
    "project",
    "resolve_base",
    "resolve_from_cache",
    "resolve_from_file",
    "resolve_from_history",
]
