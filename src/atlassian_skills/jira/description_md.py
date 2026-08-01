"""Read a Jira description as managed Markdown, when that is provable.

The read half of the Markdown workflow; `description_push` is the write half.

The rule that shapes this file: **a description that cannot be published as
Markdown is not pulled as Markdown.** Writing the file anyway would be friendly
in the moment and cruel later -- somebody edits it, and there is no way to get
the edit back to Jira. So a refused grade returns the exact wiki argv instead,
which is a workflow that exists and works today.

That is the whole reason `wiki` was built first.

**What this does with attachments, exactly.** It carries their references and
uploads nothing. A body naming `!diagram.png!` can be pulled, its prose edited,
and published back with that reference unchanged and proven unchanged. Adding an
image, renaming one, or shipping a local file is not supported -- there is no
upload here, and a Markdown `![](new.png)` pointing at a file Jira does not hold
would publish a reference to nothing. The push refuses that rather than sending
it, but the shorter version is: edit the words, not the pictures.

For the same reason a reference is refused when two attachments share its
filename. A reference names a filename only, so it cannot then be resolved to
one attachment, and a file whose references cannot be resolved is not one to
build on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.core.errors import NotFoundError, ValidationError
from atlassian_skills.jira.description_binding import (
    DescriptionBinding,
    binding_path,
    read_binding,
    source_sha256,
    write_binding,
)
from atlassian_skills.jira.description_grade import grade_description
from atlassian_skills.jira.description_io import (
    ambiguous_attachment_references,
    attachment_identity,
    read_exact,
    read_issue,
)
from atlassian_skills.jira.read_projection import assess_jira_read


def _convert(wiki: str) -> tuple[str, Any, bool]:
    """Markdown, the read report, and whether the converter itself failed."""

    try:
        result = cfxmark.from_jira_wiki(wiki)
    except Exception:  # noqa: BLE001 - a body we cannot convert is one we cannot grade
        return "", None, True
    markdown = result.markdown or ""
    report = assess_jira_read(
        wiki,
        markdown,
        document=getattr(result, "document", None),
        losses=tuple(result.losses or ()),
        attachments=tuple(result.attachments or ()),
    )
    return markdown, report, False


def _wiki_fallback(key: str, path: Path) -> list[dict[str, Any]]:
    """The workflow that does work, as an argv a caller can run unchanged.

    A complete argv, not a template. A placeholder here is what makes a caller
    compose their own command, which is the one thing the run-what-is-returned
    rule exists to prevent.
    """

    return [
        {
            "label": "edit this description as the exact markup Jira stores",
            "argv": [
                "jira",
                "issue",
                "description",
                "wiki",
                "pull",
                key,
                "--output",
                str(path.with_suffix(".wiki")),
            ],
            "requires_user_approval": False,
        }
    ]


def pull_md(client: Any, key: str, *, output_path: Path, site: str = "") -> dict[str, Any]:
    """Write the description as Markdown, but only if it could go back."""

    fields, wiki = read_issue(client, key)
    markdown, report, failed = _convert(wiki)
    grade = grade_description(wiki, read_report=report, conversion_failed=failed)

    if not grade.editable_as_markdown:
        raise ValidationError(
            f"This description cannot be managed as Markdown: {grade.status}.",
            hint=(
                "Use the exact wiki workflow, which holds the markup Jira stores byte for byte. "
                "Pulling Markdown here would give you a file whose edits could not be published."
            ),
            context={
                "reason": "description_not_markdown_manageable",
                "key": fields["key"],
                "grade": grade.to_dict(),
                "next_actions": _wiki_fallback(fields["key"], output_path),
            },
        )

    # Asked here and not only at push, because this file's own rule is that a
    # description which could not be published as Markdown is not written as
    # Markdown. Discovering it at push means discovering it after the editing.
    ambiguous = ambiguous_attachment_references(wiki, attachment_identity(fields))
    if ambiguous:
        raise ValidationError(
            "More than one attachment on this issue carries a filename the description references, "
            "so the reference does not resolve to one of them.",
            hint=(
                "Use the exact wiki workflow, which republishes the reference untouched. "
                "Managing this as Markdown would mean binding to an attachment nobody can name."
            ),
            context={
                "reason": "attachment_filename_ambiguous",
                "key": fields["key"],
                "filenames": list(ambiguous),
                "grade": grade.to_dict(),
                "next_actions": _wiki_fallback(fields["key"], output_path),
            },
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(markdown.encode("utf-8"))
    write_binding(
        output_path,
        DescriptionBinding(
            issue_id=fields["id"],
            issue_key=fields["key"],
            site=site,
            remote_updated=str(fields.get("updated") or ""),
            source_sha256=source_sha256(wiki),
            authority="md",
            base_wiki=wiki,
            base_markdown=markdown,
            grade=grade.status,
            attachments=attachment_identity(fields),
        ),
    )
    return {
        "status": "pulled",
        "key": fields["key"],
        "path": str(output_path),
        "binding_path": str(binding_path(output_path)),
        "updated": str(fields.get("updated") or ""),
        "grade": grade.to_dict(),
        # Named on the way in, because the Markdown renders `![](x.png)` whether
        # or not the file is anywhere the caller can reach.
        "attachments": list(getattr(report, "attachments", ()) or ()),
    }


def validate_md(path: Path) -> dict[str, Any]:
    """Offline: is this file still a managed Markdown description?

    Reports, like its wiki counterpart. What it adds is the authority check --
    a Markdown file whose binding says the wiki side is authoritative is one
    whose edits would be published by a different workflow, and finding that out
    at push time is finding out too late.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    body = read_exact(path)
    binding = read_binding(path)
    if binding is None:
        return {
            "status": "unbound",
            "path": str(path),
            "can_push": False,
            "detail": "no binding beside this file; Markdown cannot be published without the wiki it came from",
        }
    return {
        "status": "bound",
        "path": str(path),
        "key": binding.issue_key,
        "authority": binding.authority,
        "grade": binding.grade,
        "edited": body != binding.base_markdown,
        # False when the wiki side owns this directory. Stated rather than
        # discovered at push time.
        "markdown_is_authoritative": binding.authority == "md",
        "can_merge": bool(binding.base_markdown),
    }


def diff_md(client: Any, key: str, path: Path) -> dict[str, Any]:
    """What changed locally, and what changed on the server, kept apart.

    Two questions with one shape. A caller shown only the first republishes over
    somebody else's edit believing it changed nothing.
    """

    if not path.exists():
        raise NotFoundError(f"No such file: {path}")
    local = read_exact(path)
    fields, wiki = read_issue(client, key)
    binding = read_binding(path)
    remote_markdown, _report, failed = _convert(wiki)

    return {
        "key": fields["key"],
        "path": str(path),
        "local_edited": binding is not None and local != binding.base_markdown,
        "remote_changed_since_pull": binding is not None and source_sha256(wiki) != binding.source_sha256,
        "remote_updated": str(fields.get("updated") or ""),
        # The remote rendered as Markdown, for reading. Not a publish candidate:
        # what would be published is the local file converted back to wiki, and
        # that is the write path's question rather than this one.
        "remote_markdown_unavailable": failed,
        "identical": local == remote_markdown,
        # What publishing THIS file would do to the stored bytes. `identical`
        # compares two Markdowns, which can agree while the bytes Jira holds
        # still move -- so a caller reading only that would be told nothing
        # changes and then see the description's whitespace change.
        "change_class": _change_class(wiki, local),
    }


def _change_class(wiki: str, markdown: str) -> str:
    """What a push of this file would do, or nothing when it could not run."""

    from atlassian_skills.jira.description_push import classify_change

    try:
        candidate = cfxmark.to_jira_wiki(markdown, input_format="markdown").jira_wiki or ""
    except Exception:  # noqa: BLE001 - a body that will not convert has no candidate to compare
        return "unpublishable"
    return classify_change(wiki, candidate)


__all__ = ["diff_md", "pull_md", "validate_md"]
