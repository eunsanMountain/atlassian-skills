"""What was about to be written, so a crash can be concluded rather than guessed.

A description publish is a PUT with nothing transactional under it. Any of the
steps can be the last one that happened: the intent may never be recorded, the
request may be sent and its response lost, the readback may fail after the
server already took the body. What separates those afterwards is a record of
what was going to be sent, plus a fresh read of what is there now.

**The journal records intent. The remote decides the outcome.** That division is
the whole design. A journal saying "applied" cannot make a body be there, and a
journal saying "planned" cannot mean the PUT did not land -- a lost response
looks exactly like a request that was never sent. So nothing here authorises a
write. Every run re-proves from a fresh read; the journal only says which of the
answers that read gives is recoverable and which needs a person.

It earns its place in two situations a fresh read alone cannot resolve:

    remote == candidate    somebody put this body here. The journal says it was
                           us, so this is reconciliation rather than a
                           collision with an editor who happened to agree.

    remote == base         nothing landed -- unless the stage says the server
                           acknowledged the write, in which case the body was
                           taken and then removed, and retrying would fight
                           whoever removed it.

**Value-limited on purpose.** Hashes, an issue identity, a stage. No Markdown,
no wiki, no credentials, no line of anybody's description. Recovery state
outlives the process that wrote it, may be copied into a report or a bug
attachment, and is read by people who were never shown the issue -- so it holds
what is needed to decide, and nothing that would be a leak if it travelled. The
size follows from that: every field is a fixed-width hash, a short enum or an
id, so the file cannot grow with the description.

Beside the file rather than inside it, which is where the Confluence journal
lives. There the managed Markdown carries a manifest line that is stripped
before publishing, so a marker can ride along; here the file's whole content IS
the description, and an in-band marker would be published into the issue.

Separate from the binding for the same kind of reason: the binding is what makes
the file recoverable at all, and folding a per-operation record into it would
mean rewriting it at the one moment a crash is most likely.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA = "atls-jira-description-operation-v1"

#: Beside the description and beside its binding. Named so a person who finds
#: one after a crash can tell what it belongs to without opening it.
SUFFIX = ".atls.op.json"

PLANNED = "planned"
BODY_APPLIED_READBACK_PENDING = "body_applied_readback_pending"
_STAGES = frozenset({PLANNED, BODY_APPLIED_READBACK_PENDING})


def journal_path(description_path: Path) -> Path:
    return description_path.with_name(description_path.name + SUFFIX)


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def attachment_plan_sha256(attachments: tuple[dict[str, str], ...]) -> str:
    """The attachment identity set as one hash.

    Sorted and reduced to id and filename: `created` and `size` are carried in
    the binding for a human to read, and including them here would make the
    journal disagree with itself over a field nobody decides on.
    """

    identity = sorted((item.get("id", ""), item.get("filename", "")) for item in attachments)
    return _sha256_text(json.dumps(identity, ensure_ascii=True, separators=(",", ":")))


@dataclass(frozen=True)
class DescriptionOperation:
    """One intended description write, in values that are safe to keep."""

    operation_id: str
    issue_id: str
    issue_key: str
    authority: str
    #: The remote description this operation was proven against.
    base_sha256: str
    #: What the PUT was going to send.
    candidate_sha256: str
    #: The attachment identity set at the moment of proving.
    attachments_sha256: str
    stage: str = PLANNED
    #: Binds every field above into one value. A journal is a file somebody can
    #: edit, and without this a hand-changed `base_sha256` would turn "the
    #: remote diverged" into "nothing landed, go ahead and retry".
    proof_bundle: str = ""

    def __post_init__(self) -> None:
        if self.stage not in _STAGES:
            raise ValueError("invalid_operation_stage")
        expected = operation_proof_bundle_sha256(self)
        if not self.proof_bundle:
            object.__setattr__(self, "proof_bundle", expected)
        elif self.proof_bundle != expected:
            raise ValueError("invalid_operation_bundle")

    def applied(self) -> DescriptionOperation:
        """The same operation, with the server's acknowledgement recorded."""

        return DescriptionOperation(
            operation_id=self.operation_id,
            issue_id=self.issue_id,
            issue_key=self.issue_key,
            authority=self.authority,
            base_sha256=self.base_sha256,
            candidate_sha256=self.candidate_sha256,
            attachments_sha256=self.attachments_sha256,
            stage=BODY_APPLIED_READBACK_PENDING,
        )

    def to_dict(self) -> dict[str, object]:
        return {"schema": SCHEMA, **asdict(self)}


def operation_proof_bundle_sha256(operation: DescriptionOperation) -> str:
    payload = {
        "schema": SCHEMA,
        "operation_id": operation.operation_id,
        "issue_id": operation.issue_id,
        "issue_key": operation.issue_key,
        "authority": operation.authority,
        "base_sha256": operation.base_sha256,
        "candidate_sha256": operation.candidate_sha256,
        "attachments_sha256": operation.attachments_sha256,
        "stage": operation.stage,
    }
    return _sha256_text(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def new_operation(
    *,
    issue_id: str,
    issue_key: str,
    authority: str,
    base_sha256: str,
    candidate_sha256: str,
    attachments: tuple[dict[str, str], ...],
) -> DescriptionOperation:
    return DescriptionOperation(
        operation_id=f"op_{secrets.token_hex(16)}",
        issue_id=issue_id,
        issue_key=issue_key,
        authority=authority,
        base_sha256=base_sha256,
        candidate_sha256=candidate_sha256,
        attachments_sha256=attachment_plan_sha256(attachments),
    )


def write_journal(description_path: Path, operation: DescriptionOperation) -> Path:
    """Record the intent, and make sure it is on disk before the caller acts.

    Written through a temporary file and renamed. A journal half-written by a
    crash would be unreadable, and an unreadable journal beside a description
    that may or may not have been published is the one state this cannot
    recover from.
    """

    path = journal_path(description_path)
    staging = path.with_name(path.name + ".partial")
    staging.write_text(json.dumps(operation.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(path)
    return path


def read_journal(description_path: Path) -> DescriptionOperation | None:
    """The pending operation beside this file, or None when there is not one.

    A journal that cannot be understood is returned as None only when it is
    absent. Anything present and unreadable raises: silently treating it as
    absent would resume a write whose intent nobody can see.
    """

    path = journal_path(description_path)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("unknown_operation_schema")
    known = {key: value for key, value in payload.items() if key != "schema"}
    return DescriptionOperation(**known)


def clear_journal(description_path: Path) -> None:
    """Remove the record once the operation has an answer.

    Failing to remove it is not an error worth stopping for: the next run reads
    the remote, finds it already holds the candidate, and reconciles. That path
    exists for a lost response and covers this too, so raising here would turn a
    tidy-up problem into a failed publish.
    """

    journal_path(description_path).unlink(missing_ok=True)


__all__ = [
    "BODY_APPLIED_READBACK_PENDING",
    "PLANNED",
    "SCHEMA",
    "SUFFIX",
    "DescriptionOperation",
    "attachment_plan_sha256",
    "clear_journal",
    "journal_path",
    "new_operation",
    "operation_proof_bundle_sha256",
    "read_journal",
    "write_journal",
]
