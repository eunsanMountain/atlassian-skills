"""Edit a Jira description as the exact markup Jira stores.

The escape hatch, and the reason it comes first. Every other way of editing a
description converts it, and conversion is where a description can be refused
or quietly altered. This path converts nothing: what Jira returned is what goes
in the file, and what is in the file is what goes back. A description that
Markdown cannot hold is still editable here, which is what makes it honest to
refuse the Markdown path for those.

Byte-preserving, so there is no candidate to prove and no loss to consent to.
What remains is the part that is the same for every representation, and the part
this workflow exists to get right:

    pull      record what was read, tightly enough to notice a change
    validate  offline: is this file still bound to the issue it claims?
    diff      what would change, without changing anything
    push      re-read, refuse if it moved, write, then check what landed

`updated` alone is not the stale check. It moves for reasons unrelated to the
description -- an attachment upload does it -- and two writes inside its
resolution look identical. So `push` compares the description hash too, and a
description that changed while `updated` did not is still refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlassian_skills.core.errors import ConflictError, NotFoundError, ValidationError
from atlassian_skills.jira.description_binding import (
    DescriptionBinding,
    binding_path,
    read_binding,
    source_sha256,
    write_binding,
)
from atlassian_skills.jira.description_io import attachment_identity, read_exact, read_issue
from atlassian_skills.jira.description_push import CONCURRENCY_DISCLOSURE


def pull_wiki(client: Any, key: str, *, output_path: Path, site: str = "") -> dict[str, Any]:
    """Write the description exactly as Jira stores it, plus its binding."""

    fields, description = read_issue(client, key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, not text. A description holding CRLF must come back byte-identical:
    # letting Python translate line endings would make the file differ from what
    # the hash was taken over, and every later stale check a false alarm.
    # (`read_text(newline=...)` would say this too, but it is 3.13-only.)
    output_path.write_bytes(description.encode("utf-8"))

    binding = DescriptionBinding(
        issue_id=fields["id"],
        issue_key=fields["key"],
        site=site,
        remote_updated=str(fields.get("updated") or ""),
        source_sha256=source_sha256(description),
        authority="wiki",
        base_wiki=description,
        attachments=attachment_identity(fields),
    )
    write_binding(output_path, binding)
    return {
        "status": "pulled",
        "key": binding.issue_key,
        "issue_id": binding.issue_id,
        "path": str(output_path),
        "binding_path": str(binding_path(output_path)),
        "updated": binding.remote_updated,
        "bytes": len(description.encode("utf-8")),
    }


def validate_wiki(path: Path) -> dict[str, Any]:
    """Offline: what can this file still do, and what can it not?

    Reports rather than refuses. A file with no binding is still publishable as
    an unconditional write -- what it has lost is the stale check and the merge,
    and saying which is more useful than declining to answer.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    body = read_exact(path)
    binding = read_binding(path)
    if binding is None:
        return {
            "status": "unbound",
            "path": str(path),
            "bytes": len(body.encode("utf-8")),
            "can_push": True,
            "can_push_with_stale_guard": False,
            "can_merge": False,
            "detail": "no binding beside this file; push would be unconditional",
        }
    return {
        "status": "bound",
        "path": str(path),
        "key": binding.issue_key,
        "issue_id": binding.issue_id,
        "authority": binding.authority,
        "bytes": len(body.encode("utf-8")),
        "edited": source_sha256(body) != binding.source_sha256,
        "can_push": True,
        "can_push_with_stale_guard": True,
        "can_merge": bool(binding.base_wiki),
    }


def diff_wiki(client: Any, key: str, path: Path) -> dict[str, Any]:
    """What a push would change, computed against a FRESH read.

    Against the remote as it is now, not against the base the file was pulled
    from. A diff that shows only local edits hides the case that matters most --
    somebody else changed the description while this file sat on disk.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    local = read_exact(path)
    fields, remote = read_issue(client, key)
    binding = read_binding(path)

    remote_moved = binding is not None and source_sha256(remote) != binding.source_sha256
    return {
        "key": fields["key"],
        "path": str(path),
        "local_sha256": source_sha256(local),
        "remote_sha256": source_sha256(remote),
        "identical": local == remote,
        "remote_updated": str(fields.get("updated") or ""),
        # Two different questions, and a caller that sees only the first will
        # publish over somebody else's edit believing it changed nothing.
        "local_edited": binding is not None and source_sha256(local) != binding.source_sha256,
        "remote_changed_since_pull": remote_moved,
        "first_difference": _first_difference(remote, local),
    }


def _first_difference(before: str, after: str) -> dict[str, Any] | None:
    """The first line that differs, as (line number, stored, would-write).

    A line rather than a character offset: an offset is precise and unusable,
    and the caller's next move is to look at the line.
    """

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    for index in range(max(len(before_lines), len(after_lines))):
        stored = before_lines[index] if index < len(before_lines) else None
        would = after_lines[index] if index < len(after_lines) else None
        if stored != would:
            return {"line": index + 1, "stored": stored, "would_write": would}
    return None


def push_wiki(
    client: Any,
    key: str,
    path: Path,
    *,
    dry_run: bool = False,
    allow_unbound: bool = False,
) -> dict[str, Any]:
    """Publish the file as the description, refusing if the issue moved.

    The order is the point. Re-read first, compare against what the file was
    pulled from, and only then write -- so the window between deciding and
    writing is as small as this can make it. Then read back, because a server
    that normalised what it stored has changed the document out from under the
    next edit, and nothing else would notice.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    body = read_exact(path)
    binding = read_binding(path)

    if binding is None and not allow_unbound:
        raise ValidationError(
            "This file has no binding, so a push cannot tell whether the issue changed since it was pulled.",
            hint="Pull it with 'atls jira issue description wiki pull', or pass --allow-unbound to write anyway.",
            context={"reason": "description_binding_missing", "path": str(path), "key": key},
        )

    if binding is not None and binding.authority != "wiki":
        # The reverse of the check the Markdown path makes, and it was missing.
        # This path writes the file's bytes to Jira verbatim, which is right for
        # wiki and catastrophic for anything else: a Markdown file publishes
        # `# Title` and `- bullet` as literal text, because Jira wiki spells
        # those `h1.` and `*`. The description renders as the source of a
        # document rather than the document.
        raise ValidationError(
            "This file is managed as Markdown, so publishing it as exact wiki would write Markdown source to Jira.",
            hint=(
                "Publish it through the Markdown workflow, which converts first. "
                "To edit this description as exact wiki instead, move authority deliberately."
            ),
            context={
                "reason": "markdown_is_authoritative",
                "path": str(path),
                "key": key,
                "authority": binding.authority,
            },
        )

    fields, remote = read_issue(client, key)
    if binding is not None:
        _assert_still_bound(binding, fields, remote, path=path, key=key)

    if body == remote:
        return {"status": "no_change", "key": fields["key"], "updated": str(fields.get("updated") or "")}

    if dry_run:
        return {
            "status": "dry_run",
            "method": "PUT",
            "key": fields["key"],
            "would_write_sha256": source_sha256(body),
            "remote_sha256": source_sha256(remote),
            "first_difference": _first_difference(remote, body),
        }

    # Once more, immediately before the write. The read at the top of this function
    # happens before the no-change comparison and the dry-run branch, so an edit landing
    # in between would be overwritten by a body prepared against the older text. The
    # Markdown writer added this second read for exactly that reason and this one did
    # not have it, which left the two public writers with different windows.
    #
    # It does not close the window -- nothing a client can do closes the interval between
    # this response and the server applying the PUT, which is what `CONCURRENCY_DISCLOSURE`
    # is for. It makes the window as small as this side can make it.
    _immediate_fields, immediate = read_issue(client, fields["key"])
    if source_sha256(immediate) != source_sha256(remote):
        raise ConflictError(
            "The description changed on the server while this push was being prepared.",
            hint=(
                "Read the current description, merge the two by meaning, then push. "
                "Re-pulling would discard the local edit."
            ),
            context={
                "reason": "description_remote_changed",
                "path": str(path),
                "key": fields["key"],
                "pulled_sha256": source_sha256(remote),
                "remote_sha256": source_sha256(immediate),
                "detected": "immediately_before_write",
            },
        )

    client.update_issue(fields["key"], fields={"description": body})

    after_fields, after = read_issue(client, fields["key"])
    matched = after == body
    if binding is not None:
        write_binding(
            path,
            DescriptionBinding(
                issue_id=after_fields["id"],
                issue_key=after_fields["key"],
                site=binding.site,
                remote_updated=str(after_fields.get("updated") or ""),
                source_sha256=source_sha256(after),
                authority=binding.authority,
                base_wiki=after,
                attachments=attachment_identity(after_fields),
            ),
        )
    return {
        "status": "updated",
        "key": after_fields["key"],
        "updated": str(after_fields.get("updated") or ""),
        # Not an assertion. Jira stores description bytes verbatim in every
        # measurement taken so far, but "so far" is not "always", and a caller
        # republishing a body the server rewrote would keep undoing the rewrite.
        "description_matches_sent": matched,
        # The same disclosure the Markdown writer carries. Jira has no conditional
        # update, so this write is best effort on both paths and saying so on only one
        # of them told half the callers less than the other half.
        "concurrency": dict(CONCURRENCY_DISCLOSURE),
        "first_difference": None if matched else _first_difference(after, body),
    }


def _assert_still_bound(
    binding: DescriptionBinding,
    fields: dict[str, Any],
    remote: str,
    *,
    path: Path,
    key: str,
) -> None:
    """Refuse when the issue this file claims is not the issue in front of us."""

    if binding.issue_id and fields["id"] and binding.issue_id != fields["id"]:
        raise ValidationError(
            "This file was pulled from a different issue than the one being pushed to.",
            hint="Check the key, or pull the intended issue again.",
            context={
                "reason": "description_binding_issue_mismatch",
                "path": str(path),
                "bound_issue_id": binding.issue_id,
                "target_issue_id": fields["id"],
                "requested_key": key,
            },
        )

    if source_sha256(remote) != binding.source_sha256:
        raise ConflictError(
            "The description changed on the server after this file was pulled.",
            hint=(
                "Read the current description, merge the two by meaning, then push. "
                "Re-pulling would discard the local edit."
            ),
            context={
                "reason": "description_remote_changed",
                "path": str(path),
                "key": fields["key"],
                "pulled_updated": binding.remote_updated,
                "remote_updated": str(fields.get("updated") or ""),
                # Both hashes, so a caller can tell "somebody edited it" from
                # "the binding is for another issue" without a second request.
                "pulled_sha256": binding.source_sha256,
                "remote_sha256": source_sha256(remote),
            },
        )


__all__ = ["diff_wiki", "pull_wiki", "push_wiki", "validate_wiki"]
