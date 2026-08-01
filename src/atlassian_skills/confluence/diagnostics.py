"""One name and one sentence per loss, shared by everything that reports it.

The same dropped cell background was called `td@data-highlight-colour` by the
compatibility assessment and `table-cell-background` by the candidate check --
two names for one fact, in two payloads a caller reads minutes apart. Nothing was
wrong with either; they were simply built separately, which is how vocabularies
drift.

So the join lives here, once. A finding carries the key it was measured under
*and* the canonical code, because dropping either would break a reader: the key
is what the comparator recorded, and the code is what a person can act on.

Severity is a property of the classification rather than of the finding. A page
is `migration_required` or it is not, and the individual differences beneath that
verdict do not each get a voice -- listing five findings at five severities is how
a caller learns to skim past all of them.
"""

from __future__ import annotations

from cfxmark.compatibility import NAMED_LOSS_ATTRIBUTES

#: What a person needs to hear about a loss, keyed by canonical code. Deliberately
#: short: this is read in a terminal beside four other lines, not in a manual.
TITLES: dict[str, str] = {
    "table-cell-background": "table cell background colour",
    "list-item-paragraph-unwrapped": "paragraph wrapper inside a list item",
}

#: How loudly each workflow status speaks. `markdown_ready` says nothing at all --
#: a tool that comments on every success trains people to ignore it.
SEVERITY_BY_STATUS: dict[str, str] = {
    "markdown_ready": "none",
    "markdown_identity_bound": "info",
    "migration_required": "warning",
    "converter_fix_required": "warning",
    "xhtml_required": "warning",
}

#: What the caller is being told to do about it, in three words.
HEADLINE_BY_STATUS: dict[str, str] = {
    "markdown_identity_bound": "publish through the managed path so macro identity carries",
    "migration_required": "Markdown cannot hold everything on this page",
    "converter_fix_required": "our converter cannot hold this page yet -- not yours to approve",
    "xhtml_required": "this page holds something we cannot classify; Markdown is not a safe round trip",
}


def canonical_code(finding_code: str) -> str:
    """The shared name for a difference the comparator recorded under its own key.

    Comparator keys are `element@attribute` or `element#structure`. Where a key
    names a loss we have a word for, that word is returned; otherwise the key is
    its own name, which is honest -- inventing a friendly label for a difference
    nobody has characterised would suggest we understand it.
    """

    if "@" in finding_code:
        element, _, attribute = finding_code.partition("@")
        named = NAMED_LOSS_ATTRIBUTES.get((element, attribute))
        if named is not None:
            return named
    return finding_code


def title_for(finding_code: str) -> str:
    return TITLES.get(canonical_code(finding_code), canonical_code(finding_code))


def severity_for(status: str) -> str:
    return SEVERITY_BY_STATUS.get(status, "warning")


def headline_for(status: str) -> str:
    return HEADLINE_BY_STATUS.get(status, status)


__all__ = [
    "HEADLINE_BY_STATUS",
    "SEVERITY_BY_STATUS",
    "TITLES",
    "canonical_code",
    "headline_for",
    "severity_for",
    "title_for",
]
