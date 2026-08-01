"""Reading primitives shared by both description workflows.

Here rather than in one workflow and imported by the other. The wiki and
Markdown paths need the same three answers -- what the issue says, what the file
says, and which attachments exist -- and a shared helper reached through a
private name is a coupling nobody declared.

They belong together for a reason beyond tidiness: if the two workflows read the
issue differently they bind their files to different moments, and the stale
check on one starts disagreeing with the other about the same description.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import cfxmark

from atlassian_skills.jira.description_grade import attachment_filenames


def description_of(fields: dict[str, Any]) -> str:
    """Jira returns `null` for an issue with no description.

    Read as an empty string so the file is writable and the hash is defined. The
    difference between "no description" and "an empty one" is not one Jira
    itself preserves, so inventing it here would be inventing a distinction.
    """

    return fields.get("description") or ""


def read_exact(path: Path) -> str:
    """The file as stored, with no line-ending translation.

    Bytes, not text. A description holding CRLF must come back byte-identical:
    letting Python translate line endings would make the file differ from what
    its hash was taken over, and every later stale check a false alarm.
    (`read_text(newline=...)` says this too, but it is 3.13-only.)
    """

    return path.read_bytes().decode("utf-8")


def read_issue(client: Any, key: str) -> tuple[dict[str, Any], str]:
    """The description and the fields it came with, in ONE request.

    One request, deliberately. Reading the description and its `updated` in two
    calls binds a file to two different moments, and the stale check is then
    comparing against a state that never existed.
    """

    raw = client.get_issue_raw(key, fields=["description", "updated", "attachment"])
    fields = raw.get("fields") or {}
    return {"id": str(raw.get("id") or ""), "key": str(raw.get("key") or key), **fields}, description_of(fields)


def attachment_identity(fields: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """Id, filename and size together.

    Filename alone is not identity on Jira -- it accepts two attachments under
    one name -- so a set keyed on filename conflates them and a later sync picks
    whichever it happens to find first.
    """

    items = []
    for item in fields.get("attachment") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or ""),
                "filename": str(item.get("filename") or ""),
                "created": str(item.get("created") or ""),
                "size": str(item.get("size") or ""),
            }
        )
    return tuple(sorted(items, key=lambda entry: (entry["id"], entry["filename"])))


def baseline_markdown(wiki: str, preferred: str) -> str:
    """A Markdown that provably converts to this wiki, or nothing.

    `assess_candidate` measures converter drift by asking whether the binding's
    base Markdown still reproduces its base wiki. That question needs A Markdown
    reproducing the wiki, not the particular one somebody typed -- so when the
    file on disk no longer qualifies, the wiki's own conversion is offered and
    checked before being believed.

    It matters because the server is allowed to keep something other than what
    was sent. Binding the file's Markdown to a body the server rewrote leaves a
    pair that does not round trip, and the next push reports `converter_drift`
    about a converter that never changed.

    Empty when neither reproduces it. The drift check then abstains, which is
    the honest answer: there is no baseline to measure drift against, and
    inventing one would make the next result mean nothing.
    """

    try:
        if preferred and (cfxmark.to_jira_wiki(preferred, input_format="markdown").jira_wiki or "") == wiki:
            return preferred
        regenerated = cfxmark.from_jira_wiki(wiki).markdown or ""
        if (cfxmark.to_jira_wiki(regenerated, input_format="markdown").jira_wiki or "") == wiki:
            return regenerated
    except Exception:  # noqa: BLE001 - a body that will not convert has no baseline, which is the answer
        return ""
    return ""


def ambiguous_attachment_references(wiki: str, attachments: tuple[dict[str, str], ...]) -> tuple[str, ...]:
    """Filenames the body references that more than one attachment carries.

    A reference names a filename and nothing else, so when two attachments share
    that name the reference does not resolve to one of them. We can prove the
    candidate still holds the same STRING; we cannot prove it still means the
    same attachment, and those are different claims.

    Nothing here rewrites references, so an ambiguous one that is left untouched
    goes back exactly as it came. The refusal is not about this push corrupting
    anything -- it is that a managed file whose references cannot be resolved is
    one no later attachment work could act on, and admitting it now would build
    that debt on purpose.
    """

    if not attachments:
        return ()
    counts = Counter(item.get("filename", "") for item in attachments)
    return tuple(sorted({name for name in attachment_filenames(wiki) if counts[name] > 1}))


__all__ = [
    "ambiguous_attachment_references",
    "attachment_identity",
    "baseline_markdown",
    "description_of",
    "read_exact",
    "read_issue",
]
