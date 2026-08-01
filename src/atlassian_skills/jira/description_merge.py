"""When the description moved under your edit, and which representation owns it.

Two things that look unrelated and are the same problem: deciding what the
document should say when two people changed it, and deciding which file is
allowed to say it.

**Merge.** `prepare` writes base, local and remote as three plain files and
stops. It does not merge them. A three-way merge of prose has to be done by
somebody who knows what the words mean, and a tool that guesses produces a
document that reads fine and says something nobody wrote.

`finalize` takes the merged text and re-binds it to the remote as it is NOW,
then refuses if the issue moved again while the merge was being read. That
refusal is the point of splitting the two commands: without it, a merge that
took ten minutes publishes over whatever arrived in minute three.

**Authority.** One representation publishes from a directory. `set-authority`
moves it deliberately and re-reads the issue while doing so, because the file
being handed authority has to be bound to something current -- handing it over
on the strength of a stale binding is how the next push overwrites an edit
nobody saw.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlassian_skills.core.errors import ConflictError, NotFoundError, ValidationError
from atlassian_skills.jira.description_binding import (
    DescriptionBinding,
    read_binding,
    source_sha256,
    write_binding,
)
from atlassian_skills.jira.description_io import (
    attachment_identity,
    baseline_markdown,
    read_exact,
    read_issue,
)

#: Written beside the managed file rather than in a temp directory. A merge is
#: something a person reads, edits and comes back to, possibly tomorrow.
MERGE_DIR_SUFFIX = ".merge"


def _merge_dir(path: Path) -> Path:
    return path.with_name(path.name + MERGE_DIR_SUFFIX)


def prepare_merge(client: Any, key: str, path: Path) -> dict[str, Any]:
    """Lay out the three sides and stop.

    Deliberately stops. Merging prose by rule produces a document that reads
    fine and says something nobody wrote, and the reader cannot tell which
    sentences were chosen by a person.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    binding = read_binding(path)
    if binding is None:
        raise ValidationError(
            "This file has no binding, so there is no base to merge against.",
            hint="A merge needs the text as it was pulled; without it only a two-way overwrite is possible.",
            context={"reason": "description_binding_missing", "path": str(path), "key": key},
        )

    local = read_exact(path)
    fields, remote = read_issue(client, key)

    directory = _merge_dir(path)
    directory.mkdir(parents=True, exist_ok=True)
    base_text = binding.base_markdown if binding.authority == "md" else binding.base_wiki
    written = {
        "base": directory / "base.txt",
        "local": directory / "local.txt",
        "remote": directory / "remote.txt",
    }
    written["base"].write_bytes(base_text.encode("utf-8"))
    written["local"].write_bytes(local.encode("utf-8"))
    written["remote"].write_bytes(remote.encode("utf-8"))

    # The remote as it was when prepare ran, recorded so finalize can tell
    # whether it moved while the merge was being read.
    (directory / "prepared_from.txt").write_bytes(source_sha256(remote).encode("utf-8"))

    return {
        "status": "prepared",
        "key": fields["key"],
        "authority": binding.authority,
        "base": str(written["base"]),
        "local": str(written["local"]),
        "remote": str(written["remote"]),
        "prepared_from_sha256": source_sha256(remote),
        "next_actions": [
            {
                "label": "read base against local and base against remote, merge by meaning, then finalize",
                # The visible spelling. `finalize-merge` is the same command under a
                # hidden alias, and an action a caller is told to run has to be one
                # `--help` admits exists.
                "argv": [
                    "jira",
                    "issue",
                    "description",
                    "record-reconciled-against",
                    fields["key"],
                    "--file",
                    str(path),
                    "--merged",
                    str(directory / "merged.txt"),
                ],
                "requires_user_approval": False,
            }
        ],
    }


def finalize_merge(client: Any, key: str, path: Path, *, merged: Path) -> dict[str, Any]:
    """Re-bind the merged text to the issue as it is now, or refuse.

    The refusal is why this is a second command. A merge takes as long as it
    takes to understand two edits; without re-checking, one that took ten
    minutes publishes over whatever arrived in minute three.
    """

    if not merged.exists():
        raise NotFoundError(f"No such file: {merged}")
    binding = read_binding(path)
    if binding is None:
        raise ValidationError(
            "This file has no binding, so a merge cannot be re-bound to the issue.",
            context={"reason": "description_binding_missing", "path": str(path), "key": key},
        )

    directory = _merge_dir(path)
    marker = directory / "prepared_from.txt"
    if not marker.exists():
        raise ValidationError(
            "There is no prepared merge for this file.",
            hint=(
                "Run 'atls jira issue description prepare-reconcile' first; finalizing without it "
                "would bind to a state nobody compared against."
            ),
            context={"reason": "merge_not_prepared", "path": str(path), "key": key},
        )
    prepared_from = marker.read_bytes().decode("utf-8")

    fields, remote = read_issue(client, key)
    if source_sha256(remote) != prepared_from:
        raise ConflictError(
            "The description changed again while this merge was being read.",
            hint="Prepare the merge again; finalizing now would publish over an edit nobody has seen.",
            context={
                "reason": "remote_changed_since_prepare",
                "key": fields["key"],
                "prepared_from_sha256": prepared_from,
                "remote_sha256": source_sha256(remote),
            },
        )

    text = read_exact(merged)
    path.write_bytes(text.encode("utf-8"))
    write_binding(
        path,
        DescriptionBinding(
            issue_id=fields["id"],
            issue_key=fields["key"],
            site=binding.site,
            remote_updated=str(fields.get("updated") or ""),
            # Bound to the remote the merge was resolved against, so the push
            # that follows sees no conflict it has already settled.
            source_sha256=source_sha256(remote),
            authority=binding.authority,
            # The remote the merge was resolved against, for BOTH
            # representations. Leaving the Markdown side pointing at the
            # pre-merge wiki made a binding that disagreed with itself: its hash
            # said "current with the remote" while its base said otherwise, and
            # the next push measured the merged text against the wrong baseline
            # and reported converter_drift about a converter that never changed.
            base_wiki=remote,
            # A Markdown reproducing that remote, not the merged text. The
            # merged text is the pending EDIT -- recording it as the base would
            # say the file has no unpublished change in it, one line after
            # writing the change into the file.
            base_markdown=baseline_markdown(remote, "") if binding.authority == "md" else "",
            grade=binding.grade,
            attachments=attachment_identity(fields),
        ),
    )
    return {
        "status": "finalized",
        "key": fields["key"],
        "path": str(path),
        "authority": binding.authority,
    }


def set_authority(client: Any, key: str, path: Path, *, to: str) -> dict[str, Any]:
    """Move which representation publishes from this directory.

    Re-reads the issue while doing it. The file being handed authority has to be
    bound to something current: handing it over on a stale binding is how the
    next push overwrites an edit nobody saw.
    """

    if to not in {"md", "wiki"}:
        raise ValidationError(
            f"Unknown representation: {to}.",
            context={"reason": "unknown_authority", "allowed": ["md", "wiki"], "given": to},
        )
    if to == "md":
        # This changed a field and nothing else. It wrote `authority="md"` with
        # `base_wiki` set to whatever the issue holds and `base_markdown`
        # carried over from the old binding -- or empty, when the file never had
        # one. `assess_candidate` reads an empty `base_markdown` as "no baseline
        # to check against" and returns `safe: True` without looking at
        # anything, so the next `md push` sent a body that had never been
        # graded: a numbered list came back as two H1s and a table as escaped
        # pipes, at exit 0.
        #
        # Handing authority back needs the round trip `md pull` runs -- grade
        # the stored wiki, and write the Markdown that reproduces it. Until that
        # exists here, the move is refused rather than performed unproven.
        raise ValidationError(
            "Handing authority to Markdown is not available yet.",
            hint=(
                "It would mark the file publishable without grading the stored description, "
                "and an ungraded body publishes as something else. "
                "Pull the issue with 'atls jira issue description md pull', which grades it."
            ),
            context={
                "reason": "authority_to_md_unavailable",
                "path": str(path),
                "key": key,
                "next_actions": [
                    {
                        "label": "pull the description as graded Markdown instead",
                        "argv": ["jira", "issue", "description", "md", "pull", key, "--output", str(path)],
                        "requires_user_approval": True,
                    }
                ],
            },
        )
    binding = read_binding(path)
    if binding is None:
        raise ValidationError(
            "This file has no binding, so there is no authority to move.",
            context={"reason": "description_binding_missing", "path": str(path), "key": key},
        )

    fields, remote = read_issue(client, key)
    write_binding(
        path,
        DescriptionBinding(
            issue_id=fields["id"],
            issue_key=fields["key"],
            site=binding.site,
            remote_updated=str(fields.get("updated") or ""),
            source_sha256=source_sha256(remote),
            authority=to,
            base_wiki=remote,
            base_markdown=binding.base_markdown if to == "md" else "",
            grade=binding.grade,
            attachments=attachment_identity(fields),
        ),
    )
    return {"status": "authority_set", "key": fields["key"], "authority": to, "path": str(path)}


__all__ = ["finalize_merge", "prepare_merge", "set_authority"]
