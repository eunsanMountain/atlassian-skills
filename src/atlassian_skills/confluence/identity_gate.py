"""Never publish storage that drops identity the remote page still holds.

Confluence uses `ac:macro-id` as an instance identity key: reusing the same value
makes it treat a fragment as the same macro instance, which is what keeps that
macro's comments, permissions and attachments resolving to it. Publish the macro
without the id and the server assigns a new one, so anything attached to the old
instance no longer resolves -- and the page renders identically, which is why
nobody would notice.

**What this is not.** It is not a fix for an observed bug in the shipped paths.
Measured against a live server on 2026-07-29, both real publish paths render
against a base and the id survives, including when the edit is inside the macro's
own body:

    managed  push-md,      prose edited beside the macro     id preserved
    managed  push-md,      macro body edited                 id preserved
    stateless update --md, prose edited beside the macro     id preserved
    stateless update --md, macro body edited                 id preserved

An earlier version of this file said the state-free path dropped the id. That came
from a run against the project's fake test client, not from the server, and the
server disagrees. Measured true positives: zero.

So this is an invariant on the candidate, kept because the damage it guards
against leaves nothing to find afterwards. If a later change to how the base is
threaded stops carrying identity, no test of appearance and no read of the page
would catch it.

**Correspondence, not counts.** An earlier version compared macro counts against
id counts, and that cannot tell two different edits apart:

    remote     A(id=a)  B(id=b)
    candidate  A(id=a)  C(no id)      -- B deleted, C inserted

Both sides have two macros and the candidate holds one id, exactly as if A had
kept its id and B had lost one. The count rule refused this, blocking an ordinary
edit. So macros are matched by what they contain, and one is reported only when
*that* macro is still present and its identity is not.

Where content cannot distinguish two macros, the answer is neither "carried" nor
"lost" but "cannot tell", and the gate fails closed. Guessing which of two
identical macros owns which id is how a comment thread lands on the wrong one.

**The limit, stated rather than papered over.** Content correspondence cannot
decide one case:

    remote     macro, body "n",      id=a
    candidate  macro, body "edited", no id

That is either "the body was edited and the id was dropped" or "the old macro was
deleted and a new one written", and nothing in the two documents distinguishes
them. This gate does not guess; it reports nothing there.

That case is not unguarded, it is guarded elsewhere. The managed path holds the
base artifact and the operation journal, so it knows which edit was actually
made, and its ownership proof decides on that. This module is the cheap
last-resort invariant for the state-free path, not a replacement for the proof.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from cfxmark.compatibility import IDENTITY_BEARING
from cfxmark.storage_map import extract_storage_source, storage_leaf_identity

from atlassian_skills.core.errors import ValidationError

MACRO_ELEMENT = "ac:structured-macro"

_MACRO_IDENTITY = frozenset(attribute for element, attribute in IDENTITY_BEARING if element == MACRO_ELEMENT)
#: `ac:name` is identity-bearing but it is also what a macro *is* -- an `info` and
#: a `warning` are not the same fragment with a lost id -- so it stays in the
#: content signature and the two are never matched to each other.
TRACKED = tuple(sorted(_MACRO_IDENTITY - {"ac:name"}))


@dataclass(frozen=True)
class IdentityLoss:
    """A macro that survives into the candidate without the identity it had."""

    attribute: str
    #: Counted per macro, not per leaf: "how many macros" is the number a person
    #: can act on.
    detached: int
    #: Set when duplicate content made the correspondence undecidable. Reported
    #: rather than resolved.
    ambiguous: bool = False


def _element_root(path: tuple[str, ...], element: str) -> tuple[str, ...] | None:
    """The instance of `element` a leaf belongs to, as a path prefix.

    The outermost instance, not the innermost: one nested inside another is part
    of what the outer contains, and matching them independently would let an outer
    element look unchanged while its contents moved.
    """

    for index, segment in enumerate(path):
        if segment.startswith(f"{element}["):
            return path[: index + 1]
    return None


def _instances(storage: str, element: str, tracked: tuple[str, ...]) -> list[tuple[frozenset[tuple], dict[str, str]]]:
    """Every instance of `element`, as (content signature, tracked values).

    The signature is built from the leaves beneath the instance with their paths
    made relative to it, so the same element at a different position still matches
    itself -- moving something is not losing it.
    """

    content: dict[tuple[str, ...], set[tuple]] = defaultdict(set)
    values: dict[tuple[str, ...], dict[str, str]] = defaultdict(dict)

    for leaf in extract_storage_source(storage).leaves:
        path, field, attribute, ordinal = storage_leaf_identity(leaf)
        root = _element_root(path, element)
        if root is None:
            continue
        if field == "attribute" and attribute in tracked and len(path) == len(root):
            values[root][attribute] = leaf.value_fingerprint
            continue
        content[root].add((path[len(root) :], field, attribute, ordinal, leaf.value_fingerprint))

    # An instance may carry only tracked attributes and no other leaves, so the
    # roots come from both maps or such an instance would vanish from the census.
    return [(frozenset(content.get(root, ())), dict(values.get(root, {}))) for root in set(content) | set(values)]


def find_dropped_attributes(
    remote_storage: str,
    candidate_storage: str,
    *,
    element: str,
    attributes: tuple[str, ...],
) -> tuple[IdentityLoss, ...]:
    """Elements that survive into the candidate without an attribute they had.

    The general form of the identity check, because "did this actually get
    dropped" is the same question for a macro's id and for a cell's background,
    and answering it twice in two places is how the two answers drift apart.

    Deleting the element is not reported -- the author asked for it to go.
    Inserting one is not reported -- it never had the attribute. What is reported
    is the third case, where the element is still on the page and the attribute
    is not.
    """

    remote = _instances(remote_storage, element, attributes)
    candidate = _instances(candidate_storage, element, attributes)
    losses: list[IdentityLoss] = []

    for attribute in attributes:
        remote_with = Counter(signature for signature, values in remote if values.get(attribute))
        present: dict[frozenset[tuple], list[bool]] = defaultdict(list)
        for signature, values in candidate:
            present[signature].append(bool(values.get(attribute)))

        detached = 0
        ambiguous = False
        for signature, had in remote_with.items():
            survivors = present.get(signature, [])
            if not survivors:
                continue  # the element is gone, and so is the question
            missing = min(len(survivors), had) - sum(survivors)
            if missing > 0:
                detached += missing
                ambiguous = ambiguous or had > 1 or len(survivors) > 1

        if detached:
            losses.append(IdentityLoss(attribute=attribute, detached=detached, ambiguous=ambiguous))

    return tuple(losses)


def find_rebound_attributes(
    remote_storage: str,
    candidate_storage: str,
    *,
    element: str,
    attributes: tuple[str, ...],
) -> tuple[IdentityLoss, ...]:
    """Elements that survive into the candidate carrying a *different* identity.

    Dropping an id and swapping one are different failures with the same cause,
    and only the first was being looked for. Measured: a candidate that replaces
    `ac:macro-id="A"` with `ac:macro-id="B"` on the same macro reports no loss
    at all -- the attribute is present, so nothing counts as detached.

    The server treats an id it does not recognise as a new macro, so a swap
    detaches that macro's comments exactly as a drop does, silently and with
    nothing to notice afterwards.

    Reported, never resolved. Which of the two ids is right is not a question
    this can answer from the two documents alone.
    """

    remote = _instances(remote_storage, element, attributes)
    candidate = _instances(candidate_storage, element, attributes)
    rebound: list[IdentityLoss] = []

    for attribute in attributes:
        remote_values: dict[frozenset[tuple], list[str]] = defaultdict(list)
        for signature, values in remote:
            if values.get(attribute):
                remote_values[signature].append(values[attribute])
        candidate_values: dict[frozenset[tuple], list[str]] = defaultdict(list)
        for signature, values in candidate:
            if values.get(attribute):
                candidate_values[signature].append(values[attribute])

        changed = 0
        ambiguous = False
        for signature, before in remote_values.items():
            after = candidate_values.get(signature, [])
            if not after:
                continue  # dropped or deleted -- `find_dropped_attributes` owns that
            # Asked of the candidate, not of the remote: which ids is it carrying
            # that the remote never issued? Those are the rebindings.
            #
            # The reverse question -- which remote ids are gone -- is a different
            # one with the same shape, and asking it here reported a deletion as
            # a rebind. Two macros with identical bodies share a signature, so an
            # author removing one leaves `bbb` missing from a candidate that
            # rebound nothing; and a macro merely losing its id was counted once
            # by each function, reading as two detached macros where there is one.
            unknown = sum((Counter(after) - Counter(before)).values())
            if unknown > 0:
                # Capped by what the remote actually had: a candidate cannot
                # rebind more identities than existed to rebind.
                #
                # Known over-report, deliberately left: a candidate that ADDS a
                # macro carrying an id the remote never issued is counted here
                # even though every remote id survived. Inert through cfxmark,
                # which assigns ids only from the proven correspondence map, so
                # an inserted macro gets none -- and the direction is
                # fail-closed. It becomes real only if this is ever handed a
                # candidate from somewhere else.
                changed += min(unknown, len(before))
                ambiguous = ambiguous or len(before) > 1 or len(after) > 1

        if changed:
            rebound.append(IdentityLoss(attribute=attribute, detached=changed, ambiguous=ambiguous))

    return tuple(rebound)


def find_identity_losses(remote_storage: str, candidate_storage: str) -> tuple[IdentityLoss, ...]:
    """Macros that survive into the candidate stripped of the identity they had.

    Deleting a macro is not reported: the author asked for it to go and its
    comments go with it. Inserting one is not reported either -- the server
    assigns an id on save, and reading "no id" as "lost id" would refuse every
    insertion. What is reported is the third case, where the same macro is still
    on the page and its identity is not.
    """

    return find_dropped_attributes(remote_storage, candidate_storage, element=MACRO_ELEMENT, attributes=TRACKED)


def identity_census(storage: str) -> dict[str, Counter[str]]:
    """Per tracked attribute, how many macros carry each value.

    Values *and* counts, because either alone misses a real detach. Two macros with
    the same id going down to one leaves the set of values unchanged, and one macro
    swapping its id for another's leaves the count unchanged.
    """

    census: dict[str, Counter[str]] = {attribute: Counter() for attribute in TRACKED}
    for _signature, values in _instances(storage, MACRO_ELEMENT, TRACKED):
        for attribute, value in values.items():
            census[attribute][value] += 1
    return census


def identity_not_stored(sent_storage: str, stored_storage: str) -> dict[str, int]:
    """Per attribute, how many identities we sent that the server is not holding.

    Asked *after* the write, and it is a different question from the gate's. The gate
    compares the remote about to be replaced against the candidate about to be sent,
    and refuses. This compares the candidate we sent against what came back, and can
    only report -- the write has already happened.

    It exists because nothing could see this. The readback check converts both sides
    to Markdown and compares that, and Markdown does not carry `ac:macro-id` -- which
    is correct, it is not author content. The consequence was that a server which
    re-assigned every id on the PUT produced an unqualified `reconciled` receipt, and
    a detached comment does not announce itself.

    The live server has not been observed doing this: measured 2026-07-29, both atls
    paths carry the base forward and the id survives even an edit inside the macro's
    own body. So this is a guard on an unobserved failure, kept for the same reason the
    stored-`"2"` schema downgrade is kept as a finding -- unobserved is not impossible,
    and the cost of being wrong here is silent and unrecoverable.
    """

    sent = identity_census(sent_storage)
    stored = identity_census(stored_storage)
    missing: dict[str, int] = {}
    for attribute, wanted in sent.items():
        held = stored.get(attribute, Counter())
        # Multiset difference: a value we sent twice and got back once counts once.
        gap = sum((wanted - held).values())
        if gap:
            missing[attribute] = gap
    return missing


def assert_identity_carried(
    page_id: str,
    remote_storage: str,
    candidate_storage: str,
    *,
    workflow: str,
) -> None:
    """Stop a publish that would change identity the remote page still holds.

    Raised rather than logged: the page renders identically afterwards and the
    comments that stopped resolving do not announce themselves, so a warning in a
    log nobody reads is not a guard.

    Both ways of losing an identity, not just the obvious one. A dropped id and
    a swapped id have the same consequence -- Confluence treats an id it does
    not recognise as a new macro either way -- but only the drop was asked
    about, so a candidate that replaced `A` with `B` walked straight through a
    guard whose whole purpose is to stop exactly that.
    """

    losses = find_identity_losses(remote_storage, candidate_storage)
    rebound = find_rebound_attributes(remote_storage, candidate_storage, element=MACRO_ELEMENT, attributes=TRACKED)
    if not losses and not rebound:
        return
    # Paired with its kind here rather than recovered later by membership:
    # `IdentityLoss` is a frozen dataclass, so a rebind carrying the same
    # attribute and count as a drop compares equal to it and would be reported
    # as the wrong one.
    findings = [(loss, "dropped") for loss in losses] + [(loss, "rebound") for loss in rebound]
    detail = ", ".join(f"{loss.attribute} x{loss.detached}" for loss, _kind in findings)
    ambiguous = any(loss.ambiguous for loss, _kind in findings)
    # Named apart because they need different things from a reader: a drop asks
    # where the id went, a rebind asks where the new one came from.
    reason = "identity_would_be_dropped" if losses else "identity_would_be_rebound"
    if ambiguous:
        reason = "identity_mapping_ambiguous"
    raise ValidationError(
        f"This publish would leave {detail} on the page without the identity it has now. "
        "Confluence treats a macro without its original id as a new macro, so comments, "
        "permissions and attachments attached to it would no longer resolve."
        + (
            " More than one macro here has identical content, so which id belongs to which "
            "cannot be decided from the document."
            if ambiguous
            else ""
        ),
        hint=(
            "Publish through the managed workflow, which renders against the page it pulled: "
            "pull the page with 'atls confluence page md pull', edit that file, and push it."
        ),
        context={
            "reason": reason,
            "workflow": workflow,
            "page_id": page_id,
            "identity": [
                {
                    "attribute": loss.attribute,
                    "count": loss.detached,
                    "ambiguous": loss.ambiguous,
                    "kind": kind,
                }
                for loss, kind in findings
            ],
            # No argv. This path has no file to name -- the caller reached it from a
            # state-free write, which has no managed document -- and an argv with a
            # hole in it is the thing the "run what is returned" rule forbids. The
            # earlier placeholder here survived a sweep because the test that
            # claimed to check "anywhere" only read the compatibility payload.
            "next_action_hint": "pull the page as managed Markdown, then push that file",
        },
    )


__all__ = [
    "MACRO_ELEMENT",
    "TRACKED",
    "IdentityLoss",
    "assert_identity_carried",
    "find_identity_losses",
    "find_rebound_attributes",
]
