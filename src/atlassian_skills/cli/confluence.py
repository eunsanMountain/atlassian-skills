from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

import typer
from cfxmark.presentation import extract_presentation

from atlassian_skills.confluence.client import ConfluenceClient
from atlassian_skills.confluence.diagnostics import headline_for
from atlassian_skills.confluence.migration_preflight import describe_migration_code
from atlassian_skills.confluence.page_copy import copy_page as copy_confluence_page
from atlassian_skills.confluence.read_projection import assess_read_projection
from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import (
    AtlasError,
    ExitCode,
    NotFoundError,
    ValidationError,
    consent_retry_action,
    request_context_line,
)
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.format.markdown import (
    WriteConversionResult,
    confluence_storage_to_md_result,
    md_to_confluence_storage_result,
)
from atlassian_skills.core.models import WriteResult
from atlassian_skills.core.stdin import read_body
from atlassian_skills.core.tls import build_ssl_context

confluence_app = typer.Typer(help="Confluence commands", no_args_is_help=True)

# Sub-groups
page_app = typer.Typer(
    # The workflow groups below are the documented surface. The older flat
    # spellings still work -- scripts depend on them -- but they are hidden from
    # help and completion, because two names for one command is the confusion
    # the grouping was added to remove, and leaving both visible keeps it.
    help="Page commands. Managed workflows live under `md` and `xhtml`.",
    no_args_is_help=True,
)
space_app = typer.Typer(help="Space commands", no_args_is_help=True)
comment_app = typer.Typer(help="Comment commands", no_args_is_help=True)
label_app = typer.Typer(help="Label commands", no_args_is_help=True)
attachment_app = typer.Typer(help="Attachment commands", no_args_is_help=True)
user_app = typer.Typer(help="User commands", no_args_is_help=True)

confluence_app.add_typer(page_app, name="page")
confluence_app.add_typer(space_app, name="space")
confluence_app.add_typer(comment_app, name="comment")
confluence_app.add_typer(label_app, name="label")
confluence_app.add_typer(attachment_app, name="attachment")
confluence_app.add_typer(user_app, name="user")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(ctx_obj: dict[str, Any]) -> ConfluenceClient:
    profile_name: str = ctx_obj.get("profile", "default")
    timeout: float = ctx_obj.get("timeout", 30.0)
    config = load_config()
    profile = get_profile(config, profile_name)
    url = profile.confluence_url or os.environ.get(f"ATLS_{profile_name.upper()}_CONFLUENCE_URL")
    if not url:
        typer.echo(
            f"No Confluence URL for profile '{profile_name}'. "
            f"Set confluence_url in config or ATLS_{profile_name.upper()}_CONFLUENCE_URL env var.",
            err=True,
        )
        raise typer.Exit(1)
    credential = resolve_credential(profile_name, "confluence", profile)
    return ConfluenceClient(
        url.rstrip("/"),
        credential,
        timeout=timeout,
        verify=build_ssl_context(profile.ca_bundle),
        verbose=int(ctx_obj.get("verbose", 0)),
    )


def _fmt(ctx_obj: dict[str, Any]) -> OutputFormat:
    fmt = ctx_obj.get("format", OutputFormat.COMPACT)
    return OutputFormat(fmt) if not isinstance(fmt, OutputFormat) else fmt


def _resolve_fmt(ctx_obj: dict[str, Any], local_format: str | None) -> OutputFormat:
    if local_format:
        try:
            return OutputFormat(local_format)
        except ValueError:
            valid = ", ".join(f.value for f in OutputFormat)
            typer.echo(f"Error: Invalid format '{local_format}'. Valid: {valid}", err=True)
            raise typer.Exit(1)  # noqa: B904
    return _fmt(ctx_obj)


def _conversion_diagnostics(
    warnings: tuple[str, ...],
    losses: tuple[str, ...],
    push_safe: bool,
    *,
    table_background_omitted_count: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "push_safe": push_safe,
        "warnings": list(warnings),
        "losses": list(losses),
    }
    if table_background_omitted_count:
        payload["diagnostics"] = [
            {
                "code": "table-cell-background-omitted",
                "severity": "warning",
                "count": table_background_omitted_count,
                "message": (
                    f"Readable Markdown omits the backgrounds of {table_background_omitted_count} table cells; "
                    "the remote page remains the presentation source of truth."
                ),
            }
        ]
    return payload


def _emit_conversion_diagnostics(
    ctx: typer.Context,
    warnings: tuple[str, ...],
    losses: tuple[str, ...],
    push_safe: bool,
    *,
    table_background_omitted_count: int = 0,
) -> None:
    if ctx.obj.get("quiet"):
        return
    if not push_safe:
        typer.echo("# conversion: push_safe=false", err=True)
    for message in (*warnings, *losses):
        typer.echo(f"# conversion: {message}", err=True)
    if table_background_omitted_count:
        typer.echo(
            "# conversion: table-cell-background-omitted "
            f"count={table_background_omitted_count}; readable Markdown does not display these backgrounds",
            err=True,
        )


def _emit_compatibility_diagnostics(ctx: typer.Context, payload: Any, *, written: str | None = None) -> None:
    """Say on stderr what the JSON already says, for the person watching.

    The measurement was right and unreachable. A caller reading compact or md
    output got the compatibility verdict as one field of a one-line dict, which
    is not a thing anyone notices while a file lands successfully -- and the
    command exits 0, correctly, because a page Markdown cannot hold is a fact
    about the page and not a broken command.

    So the exit code stays 0 and the terminal says so out loud. Silent on
    `markdown_ready`: a tool that comments on every success teaches people to
    skim past the one time it matters.

    stderr, so a caller piping stdout still gets clean output, and suppressed by
    `--quiet` like every other diagnostic. The JSON signal is never suppressed --
    that is the machine's copy, and quiet is a preference about the terminal.
    """

    if ctx.obj.get("quiet") or not isinstance(payload, dict):
        return
    severity = str(payload.get("severity") or "none")
    if severity == "none":
        return

    label = "WARNING" if severity == "warning" else "INFO"
    status = str(payload.get("status") or "")
    typer.echo(f"{label}  {headline_for(status)} ({status})", err=True)
    for finding in payload.get("findings") or []:
        where = (finding.get("semantic_paths") or [None])[0]
        location = f"  at {where}" if where else ""
        typer.echo(f"         {finding.get('title')}  count={finding.get('count')}{location}", err=True)
    protected = [str(item) for item in payload.get("protected_remote_structures") or () if item]
    if protected:
        typer.echo(
            "         Markdown edits are allowed, but these remote-only structures are preserved only; "
            "editing them is refused before publishing:",
            err=True,
        )
        for structure in protected:
            typer.echo(f"           - {structure}", err=True)
    if written:
        typer.echo(f"         written: {written}", err=True)
    # The first action safe to print unprompted. `requires_user_approval` alone is
    # too blunt now: a refused `migration_required` pull offers exactly one action,
    # approving its named losses, and that approval writes a local file rather than
    # touching the page. Skipping it left the warning with no next step at all --
    # which is where agents start inventing commands, the thing this line exists to
    # prevent. What must never be printed as though it were free is a command that
    # changes the remote, so that is what gets checked.
    for action in payload.get("next_actions") or []:
        argv = list(action.get("argv") or ())
        if not action.get("requires_user_approval"):
            typer.echo(f"         next: atls {' '.join(argv)}", err=True)
            break
        if _writes_nothing_remote(argv):
            typer.echo(f"         next: atls {' '.join(argv)}   [read the findings above first]", err=True)
            break


#: Verbs that only ever read the remote. Spelled as an allowlist rather than a list
#: of writers, so a new mutating verb is excluded by default instead of by someone
#: remembering to add it.
_READ_ONLY_VERBS = frozenset({"pull", "get", "inspect", "compare", "diff", "validate"})


def _writes_nothing_remote(argv: list[str]) -> bool:
    """Whether this argv can be suggested without implying a remote change.

    `--dry-run` does not count: it is a promise about one command, and reading it
    off an argv would extend that promise to whatever the flag is attached to.
    """

    return any(token in _READ_ONLY_VERBS for token in argv) and not any(
        token in {"push", "publish", "record-reconciled-against", "rebaseline"} for token in argv
    )


def _emit_projection_diagnostics(ctx: typer.Context, report: Any, page_id: str) -> None:
    """Tell the reader, in words, that what they are looking at is not all of it.

    The strongest line is the instruction not to summarise from this output.
    Everything else here is a fact; that one is what changes what the reader
    does next, and it is the whole reason the check exists.

    Silent when the projection is faithful, which is most pages -- a warning on
    every read is one nobody reads.
    """

    if ctx.obj.get("quiet") or not report.attention_required:
        return
    if not report.content_complete:
        typer.echo("WARNING  this Markdown is missing part of the page (content_incomplete)", err=True)
        typer.echo("         do not summarize this page from this output alone", err=True)
    else:
        typer.echo("INFO     some elements are shown as placeholders (structure_incomplete)", err=True)
    for omission in report.omissions:
        where = f"  at {omission.semantic_path}" if omission.semantic_path else ""
        typer.echo(f"         {omission.label}{where}", err=True)
    for action in _next_actions_for(report, page_id):
        typer.echo(f"         next: atls {' '.join(action['argv'])}", err=True)
        break


def _next_actions_for(report: Any, page_id: str) -> list[dict[str, Any]]:
    actions = report.to_dict(page_id).get("next_actions", [])
    return list(actions) if isinstance(actions, list) else []


def _readable_table_background_omission_count(conversion: Any) -> int:
    if conversion.document is None:
        return 0
    return sum(cell.background is not None for cell in extract_presentation(conversion.document).cells)


def _output_with_write_diagnostics(
    ctx: typer.Context,
    output: Any,
    fmt: OutputFormat,
    conversion: WriteConversionResult,
) -> Any:
    has_diagnostics = bool(conversion.warnings or conversion.losses or not conversion.push_safe)
    if not has_diagnostics:
        return output
    if fmt != OutputFormat.JSON:
        _emit_conversion_diagnostics(
            ctx,
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        )
        return output
    if isinstance(output, dict):
        payload: Any = dict(output)
    elif hasattr(output, "model_dump"):
        payload = output.model_dump(mode="json", exclude_none=True)
    else:
        payload = output
    if isinstance(payload, dict):
        payload["conversion"] = _conversion_diagnostics(
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        )
        return payload
    return {
        "result": payload,
        "conversion": _conversion_diagnostics(
            conversion.warnings,
            conversion.losses,
            conversion.push_safe,
        ),
    }


# Counts that explain *why* a selection failed. JSON callers already receive the
# whole context; without these lines the human reader loses the one number that
# distinguishes "no match" from "three matches" and has to re-run with
# --format=json to find out.
# (context key, label, always show). The primary count is always shown because
# "0 matches" is itself the answer; the other two are noise when zero.
_DIAGNOSTIC_COUNT_KEYS = (
    ("match_count", "matches", True),
    ("boundary_match_count", "spanning inline markup", False),
    ("excluded_match_count", "in attributes or macros", False),
)

# Allowlisted local templates. A description_code that is not listed here is
# ignored rather than echoed, so no server-provided string can reach the user
# as if it were an instruction from atls.
_NEXT_ACTION_TEXT = {
    "PATCH_RETRY_SINGLE_LEAF": "Retry --find with the exact text of a single plain-text part.",
    "PATCH_USE_MANAGED_EDIT": "For structural or macro content, use pull-md and edit as Markdown.",
}


#: The console rendering for every approval-gated retry `consent_retry_action` can
#: build, keyed by its `description_code`: (approval option, fingerprint prefix,
#: what to do *instead* of approving).
#:
#: A code missing from here is not a cosmetic gap. `_consent_retry_display` returns
#: None for it, so `_handle_error` prints neither the loss summary nor the retry
#: command -- leaving a hint that says "run the returned command exactly" above an
#: output that returned none, and the JSON envelope as the only way to recover the
#: fingerprint. `REVIEW_FULL_REPLACEMENT_AND_RETRY` shipped in exactly that state.
#: `test_next_action_argv.py` now derives the required keys from the call sites.
class _ConsentRule(NamedTuple):
    """How one consent kind's sanctioned retry command is recognized and described."""

    #: The primary approval flag. Exactly one, at the end of the command.
    option: str
    #: What its fingerprint must begin with, so an approval of one kind cannot be
    #: displayed as an approval of another.
    fingerprint_prefix: str
    #: Additional approval flags this kind -- and only this kind -- may carry after the
    #: primary one, each at most once and each repeating the primary fingerprint.
    #: Empty for a consent that takes a single approval.
    companions: frozenset[str]
    #: What to do *instead* of approving.
    alternative: str


_CONSENT_ACTIONS = {
    "REVIEW_MIGRATION_AND_RETRY": _ConsentRule(
        "--accept-migration",
        "mig_sha256:",
        frozenset(),
        "Use page inspect and patch-text for a narrow plain-text edit, or revise the managed Markdown and rerun "
        "--dry-run.",
    ),
    "REVIEW_CONVERSION_AND_RETRY": _ConsentRule(
        "--accept-conversion",
        "conv_sha256:",
        frozenset(),
        "Revise unsupported Markdown constructs and rerun --dry-run before approving conversion.",
    ),
    "REVIEW_FULL_REPLACEMENT_AND_RETRY": _ConsentRule(
        "--accept-full-replacement",
        "repl_sha256:",
        frozenset({"--accept-discarded-identities"}),
        "Re-read the page and re-apply the change in steps the in-place proof can attribute, then rerun --dry-run; "
        "approve a replacement only when rewriting the whole page body is the intent.",
    ),
}

#: Every approval flag any rule can carry. `_consent_retry_display` refuses to show a
#: command whose *command part* contains one of these: the approval section is the tail
#: and nothing else, so a second primary approval, a companion belonging to a different
#: consent kind, or a stray approval in the middle all fail closed rather than being
#: normalized into an apparently sanctioned retry.
_ALL_CONSENT_OPTIONS = frozenset(
    {rule.option for rule in _CONSENT_ACTIONS.values()}
    | {companion for rule in _CONSENT_ACTIONS.values() for companion in rule.companions}
)

_MIGRATION_EFFECT_LABELS = {
    "converted": "converted",
    "normalized": "normalized",
    "removed": "removed",
    "unsupported": "unsupported",
    "fatal": "fatal",
}

_DIAGNOSTIC_CATEGORY_LABELS = {
    "readable_simplification": "readable simplification",
    "canonicalization": "canonicalization",
    "presentation_loss": "presentation loss",
    "content_loss": "content loss",
    "structural_loss": "structural loss",
    "manual_replacement": "manual replacement",
}


def _report_groups(report: Any, field: str, labels: dict[str, str]) -> list[str]:
    if not isinstance(report, dict) or not isinstance(report.get("occurrences"), list):
        return []
    counts = Counter(
        occurrence.get(field)
        for occurrence in report["occurrences"]
        if isinstance(occurrence, dict) and occurrence.get(field) in labels
    )
    return [f"{label}={counts[value]}" for value, label in labels.items() if counts[value]]


def _replacement_groups(context: dict[str, Any]) -> list[str]:
    """What an explicit full replacement discards, from its candidate-bound manifest.

    This route can legitimately carry no migration or conversion occurrence at all --
    the strict proof refused to *attribute* the change, which is not the same as the
    change being lossy. Without a contribution here the summary comes back empty and
    `_handle_error` withholds the retry command, which is the correct fail-closed rule
    (no disclosure, no approval token) applied to a case that does have a disclosure.

    The counted identities are macro identities the replacement drops. Their count is
    the load-bearing number; the raw UUIDs behind it never leave cfxmark's private
    manifest, so nothing here can carry page content.
    """

    manifest = context.get("full_replacement")
    if not isinstance(manifest, dict):
        return []
    count = manifest.get("discarded_identity_count")
    # A negative count is not a disclosure, it is a malformed one. Letting it through
    # produced a non-empty summary, and a non-empty summary is exactly what unlocks the
    # retry command -- so the fail-closed rule (no disclosure, no approval token) has to
    # reject it here rather than render it.
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return []
    return ["full replacement=whole page body", f"discarded identities={count}"]


def _consent_loss_summary(context: dict[str, Any]) -> str | None:
    groups = _replacement_groups(context)
    groups.extend(_report_groups(context.get("migration_report"), "effect", _MIGRATION_EFFECT_LABELS))
    groups.extend(_report_groups(context.get("source_conversion_report"), "category", _DIAGNOSTIC_CATEGORY_LABELS))
    if not groups:
        conversion = context.get("conversion")
        losses = conversion.get("losses") if isinstance(conversion, dict) else None
        if isinstance(losses, list) and losses:
            groups.append(f"source loss={len(losses)}")
    return ", ".join(groups) if groups else None


def _safe_loss_text(value: Any, *, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    flattened = " ".join("".join(character if character.isprintable() else " " for character in value).split())
    if not flattened:
        return None
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _consent_loss_details(context: dict[str, Any], *, limit: int = 5) -> tuple[str, ...]:
    details: list[str] = []
    total = 0
    for report_key in ("migration_report", "source_conversion_report"):
        report = context.get(report_key)
        occurrences = report.get("occurrences") if isinstance(report, dict) else None
        if not isinstance(occurrences, list):
            continue
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                continue
            total += 1
            if len(details) >= limit:
                continue
            # Prefer the atls-owned value-free curated description for a known stable
            # code (shown with the code for traceability); the value-free JSON envelope
            # already strips cfxmark's display_label, so this restores console
            # readability without reintroducing any arbitrary-content leak. Fall back to
            # a display_label only when a caller passes an un-redacted context, then to
            # the raw stable code.
            code_value = occurrence.get("code")
            described = describe_migration_code(code_value if isinstance(code_value, str) else None)
            if described is not None:
                label: str | None = f"{described} ({code_value})"
            else:
                label = _safe_loss_text(occurrence.get("display_label")) or _safe_loss_text(code_value)
            impact = _safe_loss_text(occurrence.get("user_impact"))
            before = _safe_loss_text(occurrence.get("before_summary"))
            after = _safe_loss_text(occurrence.get("after_summary"))
            workflow = _safe_loss_text(occurrence.get("suggested_workflow"))
            lines = [f"Loss detail {len(details) + 1}: {label or 'unlabelled migration'}"]
            if impact:
                lines.append(f"  Impact: {impact}")
            if before or after:
                lines.append(f"  Change: {before or 'unknown'} -> {after or 'unknown'}")
            if workflow:
                lines.append(f"  Suggested: {workflow}")
            details.append("\n".join(lines))
    if total > limit:
        details.append(f"Loss detail: {total - limit} additional occurrence(s) omitted; use --format=json for all.")
    return tuple(details)


def _consent_retry_display(action: Any) -> tuple[str, str] | None:
    if not isinstance(action, dict) or action.get("id") != "retry_with_consent":
        return None
    if action.get("requires_user_approval") is not True:
        return None
    rule = _CONSENT_ACTIONS.get(str(action.get("description_code")))
    argv = action.get("argv")
    if rule is None or not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        return None
    if argv[:3] != ["atls", "confluence", "page"]:
        return None
    # Peel *this rule's own* companions off the tail, each at most once, before looking
    # for the primary approval. Anchoring at the end -- rather than searching argv for
    # the option -- keeps the original guarantee that what gets displayed is the
    # sanctioned shape and not a command that merely contains the flag somewhere.
    head = list(argv)
    seen_companions: set[str] = set()
    companion_fingerprints: list[str] = []
    while len(head) >= 4 and head[-2] in rule.companions:
        if head[-2] in seen_companions:
            return None
        seen_companions.add(head[-2])
        companion_fingerprints.append(head[-1])
        head = head[:-2]
    if len(head) < 2 or head[-2] != rule.option:
        return None
    # Everything before the approval section must be command, not approval. This single
    # check covers a repeated primary approval, a companion belonging to a different
    # consent kind, and an approval flag buried in the middle -- none of which any
    # producer builds, and each of which would otherwise be normalized into a command
    # the user is told to run verbatim.
    if any(argument in _ALL_CONSENT_OPTIONS for argument in head[:-2]):
        return None
    fingerprint = head[-1]
    if any(value != fingerprint for value in companion_fingerprints):
        return None
    # `removeprefix` is a no-op when the prefix is absent, so the length check alone
    # would have accepted a bare digest carrying no approval kind at all.
    if not fingerprint.startswith(rule.fingerprint_prefix):
        return None
    digest = fingerprint[len(rule.fingerprint_prefix) :]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    display = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    return rule.alternative, display


def _handle_error(err: AtlasError, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(err.to_dict()))
    else:
        typer.echo(f"Error: {err.message}", err=True)
        request_line = request_context_line(err)
        if request_line:
            typer.echo(request_line, err=True)
        context = err.context or {}
        counts = [
            f"{context[key]} {label}"
            for key, label, always in _DIAGNOSTIC_COUNT_KEYS
            if isinstance(context.get(key), int) and (always or context[key])
        ]
        if counts:
            typer.echo(f"Found: {', '.join(counts)}", err=True)
        actions = context.get("next_actions")
        consent_retries = (
            [retry for action in actions if (retry := _consent_retry_display(action)) is not None]
            if isinstance(actions, list)
            else []
        )
        if consent_retries:
            summary = _consent_loss_summary(context)
            if summary:
                typer.echo(f"Loss summary: {summary}", err=True)
                for detail in _consent_loss_details(context):
                    typer.echo(detail, err=True)
                for alternative, _command in consent_retries:
                    typer.echo(f"Alternative: {alternative}", err=True)
            else:
                typer.echo("Loss summary: unavailable; retry command withheld.", err=True)
                consent_retries = []
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
        if isinstance(actions, list):
            for action in actions:
                text = _NEXT_ACTION_TEXT.get(str(action.get("description_code")))
                if text:
                    typer.echo(f"Next:  {text}", err=True)
        for _alternative, command in consent_retries:
            typer.echo("", err=True)
            typer.echo(f"Retry: {command}", err=True)
    raise typer.Exit(err.exit_code)


# ---------------------------------------------------------------------------
# page get <id>
# ---------------------------------------------------------------------------


@page_app.command("get")
def page_get(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    body_repr: str | None = typer.Option(None, "--body-repr", help="Body representation: md|raw|storage|view"),
    passthrough_prefix: list[str] | None = typer.Option(
        None,
        "--passthrough-prefix",
        help="Preserve an additional HTML comment prefix during Markdown conversion (repeatable)",
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
) -> None:
    """Get a Confluence page by ID."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)

    try:
        if body_repr not in {None, "md", "raw", "storage", "view"}:
            raise ValidationError(
                "--body-repr must be md, raw, storage, or view",
                context={"reason": "invalid_body_representation", "body_repr": body_repr},
            )
        markdown_conversion = body_repr == "md" or (body_repr is None and fmt == OutputFormat.MD)
        if passthrough_prefix and not markdown_conversion:
            raise ValidationError(
                "--passthrough-prefix requires Markdown body conversion",
                context={"reason": "passthrough_requires_markdown_conversion"},
            )
        from atlassian_skills.core.managed_manifest import (
            ManagedManifestError,
            parse_passthrough,
            serialize_passthrough,
        )

        try:
            canonical_prefixes = parse_passthrough(serialize_passthrough(passthrough_prefix or ()))
        except ManagedManifestError as error:
            raise ValidationError("Invalid passthrough prefix", context=error.context) from error
        client = _make_client(ctx.obj)

        # RAW format: return server response text verbatim (byte-preserving contract)
        if fmt == OutputFormat.RAW and body_repr is None:
            typer.echo(client.get_page_raw_text(page_id))
            return

        if body_repr == "view":
            page = client.get_page(page_id, expand="body.view,version,space,history", include_body=True)
            if not isinstance(page.body_view, str):
                raise ValidationError(
                    "Confluence rendered view body is missing",
                    context={"reason": "view_body_missing", "page_id": page_id},
                )
            if fmt == OutputFormat.RAW:
                typer.echo(page.body_view, nl=False)
            elif fmt == OutputFormat.JSON:
                payload = page.model_dump(mode="json")
                payload.update(
                    {
                        "representation": "view",
                        "editable": False,
                        "publishable": False,
                        "reason": "server-rendered-html",
                    }
                )
                typer.echo(format_output(payload, fmt))
            else:
                typer.echo(page.body_view)
            return

        needs_body = body_repr in ("md", "raw", "storage") or fmt == OutputFormat.MD
        include_body = needs_body or fmt != OutputFormat.COMPACT
        page = client.get_page(page_id, include_body=include_body)

        conversion = None
        if page.body_storage is not None and markdown_conversion:
            conversion = confluence_storage_to_md_result(
                page.body_storage,
                profile="readable",
                passthrough_prefixes=canonical_prefixes,
            )
            if body_repr == "md":
                page.body_storage = "" if page.body_storage == "" else conversion.markdown or ""
        # "raw" and "storage" keep the storage XHTML as-is

        if fmt == OutputFormat.JSON and conversion is not None:
            table_background_omitted_count = _readable_table_background_omission_count(conversion)
            payload = page.model_dump(mode="json")
            payload["conversion"] = _conversion_diagnostics(
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
                table_background_omitted_count=table_background_omitted_count,
            )
            payload["representation"] = "md"
            payload["editable"] = False
            payload["publishable"] = False
            # Whether this projection still holds the page. `editable=false` says
            # it is not publish input; it does not say a paragraph is missing,
            # and a caller summarising from an incomplete projection is
            # confidently wrong with nothing to warn them.
            payload.update(assess_read_projection(conversion.document).to_dict(page_id))
            payload["conversion_options"] = {"passthrough_prefixes": list(canonical_prefixes)}
            typer.echo(format_output(payload, fmt))
        elif body_repr == "md" and conversion is not None:
            typer.echo(conversion.markdown or "", nl=False)
        elif fmt == OutputFormat.MD and conversion is not None:
            from atlassian_skills.core.format.markdown import format_page_md_document, format_page_md_header

            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(format_page_md_document(header, conversion.markdown or ""))
        elif fmt == OutputFormat.RAW and body_repr in {"raw", "storage"}:
            typer.echo(page.body_storage or "", nl=False)
        elif fmt == OutputFormat.MD and body_repr:
            from atlassian_skills.core.format.markdown import format_page_md_header

            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(header + (page.body_storage or ""))
        else:
            typer.echo(format_output(page, fmt))
        if conversion is not None and fmt != OutputFormat.JSON:
            _emit_projection_diagnostics(ctx, assess_read_projection(conversion.document), page_id)
            _emit_conversion_diagnostics(
                ctx,
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
                table_background_omitted_count=_readable_table_background_omission_count(conversion),
            )
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("inspect")
def page_inspect(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    intent: str = typer.Option(..., "--intent", help="read|text-edit|append|structure-edit|presentation-edit"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Inspect a page and recommend a non-authoritative edit workflow."""

    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.page_inspect import inspect_page

        result = inspect_page(_make_client(ctx.obj), page_id, intent=intent)
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page search <cql>
# ---------------------------------------------------------------------------


@page_app.command("search")
def page_search(
    ctx: typer.Context,
    cql: str = typer.Argument(..., help="CQL query string"),
    limit: int = typer.Option(25, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Search Confluence pages with CQL."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.search(cql, limit=limit)
        typer.echo(format_output(result.results, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page children <id>
# ---------------------------------------------------------------------------


@page_app.command("children")
def page_children(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Parent page ID"),
    limit: int = typer.Option(25, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List child pages of a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        pages = client.get_children(page_id, limit=limit)
        typer.echo(format_output(pages, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page history <id> <version>
# ---------------------------------------------------------------------------


@page_app.command("history")
def page_history(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    version: int = typer.Argument(..., help="Version number"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get a specific historical version of a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        page = client.get_page_history(page_id, version)
        if fmt == OutputFormat.MD:
            from atlassian_skills.core.format.markdown import format_page_md_document, format_page_md_header

            conversion = confluence_storage_to_md_result(page.body_storage or "", profile="readable")
            space_key = page.space.key if page.space else ""
            header = format_page_md_header(page.title, space_key, page.version)
            typer.echo(format_page_md_document(header, conversion.markdown or ""))
            _emit_conversion_diagnostics(
                ctx,
                conversion.warnings,
                conversion.losses,
                conversion.push_safe,
            )
        else:
            typer.echo(format_output(page, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page diff <id> --from <ver> --to <ver>
# ---------------------------------------------------------------------------


@page_app.command("diff")
def page_diff(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    from_ver: int = typer.Option(..., "--from", help="From version number"),
    to_ver: int = typer.Option(..., "--to", help="To version number"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Show unified diff between two page versions."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        diff = client.get_page_diff(page_id, from_ver, to_ver)
        if fmt == OutputFormat.JSON:
            typer.echo(json.dumps({"diff": diff}))
        else:
            typer.echo(diff)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page images <id>
# ---------------------------------------------------------------------------


@page_app.command("images")
def page_images(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List image attachments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        images = client.get_page_images(page_id)
        typer.echo(format_output(images, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# space tree <key>
# ---------------------------------------------------------------------------


@space_app.command("tree")
def space_tree(
    ctx: typer.Context,
    space_key: str = typer.Argument(..., help="Space key"),
    limit: int = typer.Option(200, "--limit", "-l", help="Max pages"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get the page tree of a space."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.get_space_tree(space_key, limit=limit)
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment list <page_id>
# ---------------------------------------------------------------------------


@comment_app.command("list")
def comment_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List comments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        comments = client.list_comments(page_id)
        typer.echo(format_output(comments, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# label list <page_id>
# ---------------------------------------------------------------------------


@label_app.command("list")
def label_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List labels on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        labels = client.list_labels(page_id)
        typer.echo(format_output(labels, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment list <page_id>
# ---------------------------------------------------------------------------


@attachment_app.command("list")
def attachment_list(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """List attachments on a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        attachments = client.list_attachments(page_id, limit=limit)
        typer.echo(format_output(attachments, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment download <att_id> --output <path>
# ---------------------------------------------------------------------------


@attachment_app.command("download")
def attachment_download(
    ctx: typer.Context,
    att_id: str = typer.Argument(..., help="Attachment content ID"),
    output: str = typer.Option(..., "--output", "-o", help="Output file path"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Download a single attachment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        path = client.download_attachment(att_id, output)
        typer.echo(format_output({"downloaded": str(path)}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment download-all <page_id> --output-dir <dir>
# ---------------------------------------------------------------------------


@attachment_app.command("download-all")
def attachment_download_all(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Output directory"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Download all attachments from a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        paths = client.download_all_attachments(page_id, output_dir)
        data = [{"downloaded": str(p)} for p in paths]
        typer.echo(format_output(data, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# user search <query>
# ---------------------------------------------------------------------------


@user_app.command("search")
def user_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search query (fuzzy match on name/email)"),
    group: str = typer.Option("confluence-users", "--group", "-g", help="Group to search in"),
    limit: int = typer.Option(200, "--limit", "-l", help="Max group members to fetch"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Search Confluence users by name/email (fuzzy match on group members)."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        users = client.search_users(query, group_name=group, limit=limit)
        typer.echo(format_output(users, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@user_app.command("me")
def user_me(
    ctx: typer.Context,
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Get the current authenticated user."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        user = client.get_current_user()
        typer.echo(format_output(user, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ===========================================================================
# Write commands
# ===========================================================================


def _resolve_body(body_file: str | None, body_format: str) -> WriteConversionResult:
    """Read body from file/stdin and convert md to storage if needed."""
    if body_file is None:
        typer.echo("Error: --body-file is required for this command", err=True)
        raise typer.Exit(ExitCode.VALIDATION)
    raw = read_body(body_file=body_file)
    if body_format == "md":
        from atlassian_skills.confluence.push_md import _assert_push_safe_source

        _assert_push_safe_source(raw)
        result = md_to_confluence_storage_result(raw)
        if not result.push_safe or result.losses:
            raise ValidationError(
                "The Markdown body cannot be converted without losing content.",
                hint="Resolve the reported conversion losses before publishing.",
                context=_conversion_diagnostics(result.warnings, result.losses, result.push_safe),
            )
        return result
    return WriteConversionResult(body=raw)


# ---------------------------------------------------------------------------
# page create
# ---------------------------------------------------------------------------


def _asset_base(body_file: str | None, asset_dir: str | None) -> Path | None:
    """Where a relative image reference resolves from.

    The body file's own directory by default, because that is what an author sees
    when they write `![](diagram.png)` next to the file. `--asset-dir` widens it.

    `None` for standard input, which has no directory. Falling back to the
    working directory would make the same command mean different things in
    different terminals, so a document from stdin that references a local file is
    refused with the flag to fix it.
    """

    if asset_dir is not None:
        return Path(asset_dir)
    if body_file is None or body_file == "-":
        return None
    return Path(body_file).parent


@page_app.command("create")
def page_create(
    ctx: typer.Context,
    space: str = typer.Option(..., "--space", "-s", help="Space key"),
    title: str = typer.Option(..., "--title", "-t", help="Page title"),
    parent_id: str | None = typer.Option(None, "--parent-id", help="Parent page ID"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    accept_conversion: str | None = typer.Option(
        None,
        "--accept-conversion",
        help="Exact source conversion fingerprint returned by preflight",
    ),
    asset_dir: str | None = typer.Option(
        None,
        "--asset-dir",
        help="Directory local image references resolve from (default: the body file's own directory)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Create a new Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if body_file is None:
            raise ValidationError("--body-file is required for this command", context={"reason": "body_file_required"})
        body = read_body(body_file=body_file)
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.stateless_write import create_page_stateless

        next_action = [
            "atls",
            "confluence",
            "page",
            "create",
            "--space",
            space,
            "--title",
            title,
            "--body-file",
            body_file,
            "--body-format",
            body_format,
        ]
        if parent_id is not None:
            next_action.extend(("--parent-id", parent_id))
        result = create_page_stateless(
            client,
            space=space,
            title=title,
            parent_id=parent_id,
            body=body,
            body_format=body_format,
            dry_run=dry_run,
            accept_conversion=accept_conversion,
            next_action_argv=tuple(next_action),
            asset_dir=_asset_base(body_file, asset_dir),
            body_source=body_file,
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "created":
            typer.echo(format_output(WriteResult(action="created", key=str(result["id"]), summary=title), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("copy")
def page_copy(
    ctx: typer.Context,
    source_page_id: str = typer.Argument(..., help="Source page ID (read-only)"),
    parent_id: str = typer.Option(..., "--parent-id", help="Destination parent page ID"),
    space: str = typer.Option(..., "--space", "-s", help="Destination space key"),
    title: str | None = typer.Option(None, "--title", "-t", help="Destination title (default: source title)"),
    include_attachments: bool = typer.Option(
        False,
        "--include-attachments",
        help="Copy every source attachment; required when attachments exist",
    ),
    verify: bool = typer.Option(
        True,
        "--verify/--no-verify",
        help="Download copied attachments and verify storage/attachment hashes",
    ),
    reason: str | None = typer.Option(None, "--reason", help="Add a visible comment to the copied page"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read and preflight without creating content"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Copy one page and its attachments into a verified run-owned page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        result = copy_confluence_page(
            _make_client(ctx.obj),
            source_page_id,
            destination_parent_id=parent_id,
            destination_space=space,
            title=title,
            include_attachments=include_attachments,
            verify=verify,
            reason=reason,
            dry_run=dry_run,
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "copied":
            typer.echo(
                format_output(
                    WriteResult(
                        action="copied",
                        key=str(result["target"]["id"]),
                        summary=str(result["target"]["title"]),
                    ),
                    fmt,
                )
            )
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page update
# ---------------------------------------------------------------------------


@page_app.command("update")
def page_update(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to update"),
    title: str | None = typer.Option(None, "--title", "-t", help="New title (default: keep current)"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    accept_migration: str | None = typer.Option(
        None,
        "--accept-migration",
        help="Exact migration fingerprint returned by preflight",
    ),
    accept_full_replacement: str | None = typer.Option(
        None,
        "--accept-full-replacement",
        help="Exact full-replacement fingerprint returned by preflight",
    ),
    accept_discarded_identities: str | None = typer.Option(
        None,
        "--accept-discarded-identities",
        help="Same full-replacement fingerprint, required when identities are discarded",
    ),
    asset_dir: str | None = typer.Option(
        None,
        "--asset-dir",
        help="Directory local image references resolve from (default: the body file's own directory)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Update an existing Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if body_file is None:
            raise ValidationError("--body-file is required for this command", context={"reason": "body_file_required"})
        body = read_body(body_file=body_file)
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.stateless_write import (
            _require_full_replacement_consent,
            build_page_update_preflight,
            publish_page_update,
        )

        preflight = build_page_update_preflight(
            client,
            page_id,
            body,
            body_format=body_format,
            title=title,
            if_version=if_version,
            asset_dir=_asset_base(body_file, asset_dir),
        )
        next_action = [
            "atls",
            "confluence",
            "page",
            "update",
            page_id,
            "--body-file",
            body_file,
            "--body-format",
            body_format,
        ]
        if title is not None:
            next_action.extend(("--title", title))
        if if_version is not None:
            next_action.extend(("--if-version", str(if_version)))
        if reason is not None:
            next_action.extend(("--reason", reason))
        if minor_edit:
            next_action.append("--minor-edit")
        if dry_run:
            _require_full_replacement_consent(
                preflight,
                accept_full_replacement=accept_full_replacement,
                accept_discarded_identities=accept_discarded_identities,
                argv=tuple(next_action),
            )
            dry_result = {**preflight.to_dict(), "status": "dry_run", "method": "PUT"}
            if preflight.consent_required:
                assert preflight.migration_fingerprint is not None
                dry_result["next_actions"] = [
                    consent_retry_action(
                        tuple(next_action),
                        option="--accept-migration",
                        fingerprint=preflight.migration_fingerprint,
                        description_code="REVIEW_MIGRATION_AND_RETRY",
                    )
                ]
            typer.echo(format_output(dry_result, fmt))
            return
        result = publish_page_update(
            client,
            preflight,
            accept_migration=accept_migration,
            reason=reason,
            minor_edit=minor_edit,
            next_action_argv=tuple(next_action),
            accept_full_replacement=accept_full_replacement,
            accept_discarded_identities=accept_discarded_identities,
        )
        if fmt == OutputFormat.COMPACT and result["status"] == "updated":
            output = WriteResult(action="updated", key=page_id)
            typer.echo(format_output(output, fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("patch-text")
def page_patch_text(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    find: str | None = typer.Option(None, "--find", help="Exact decoded plain text to find"),
    replace: str | None = typer.Option(None, "--replace", help="Replacement plain text"),
    patch_file: str | None = typer.Option(
        None,
        "--patch-file",
        help="JSON batch patch file with version and exact node selectors",
    ),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and report exact text nodes without PUT"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Patch one or more exact storage text nodes with a state-free verified write."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.patch_text import parse_patch_document, patch_text

        if patch_file is not None:
            if find is not None or replace is not None or if_version is not None:
                raise ValidationError(
                    "--patch-file cannot be combined with --find, --replace, or --if-version",
                    context={"reason": "patch_input_conflict"},
                )
            try:
                payload = json.loads(Path(patch_file).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    "Unable to read a valid UTF-8 JSON patch file",
                    context={"reason": "patch_file_read_failed", "path": patch_file},
                ) from error
            document = parse_patch_document(payload)
        else:
            if find is None or replace is None:
                raise ValidationError(
                    "--find and --replace are required when --patch-file is omitted",
                    context={"reason": "patch_input_missing"},
                )
            document = None
            if if_version is None:
                raise ValidationError(
                    "patch-text requires --if-version when --patch-file is omitted",
                    context={"reason": "patch_version_required"},
                )

        client = _make_client(ctx.obj)
        result = patch_text(
            client,
            page_id,
            old=find,
            new=replace,
            patch_document=document,
            if_version=if_version,
            dry_run=dry_run,
            reason=reason,
            minor_edit=minor_edit,
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page delete
# ---------------------------------------------------------------------------


@page_app.command("delete")
def page_delete(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to delete"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a Confluence page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)

        if dry_run:
            typer.echo(format_dry_run("DELETE", f"{client.base_url}/rest/api/content/{page_id}", fmt=fmt.value))
            return

        client.delete_page(page_id)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="deleted", key=page_id), fmt))
        else:
            typer.echo(format_output({"deleted": page_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page move
# ---------------------------------------------------------------------------


@page_app.command("move")
def page_move(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID to move"),
    target: str = typer.Option(..., "--target", help="Target page ID"),
    position: str = typer.Option("append", "--position", "-p", help="Position: append, above, below"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Move a page relative to a target page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.move_page(page_id, position, target)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="moved", key=page_id), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment add
# ---------------------------------------------------------------------------


@comment_app.command("add")
def comment_add(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add a comment to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_conversion = _resolve_body(body_file, body_format)
        body = body_conversion.body
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "container": {"id": page_id, "type": "page"},
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            _emit_conversion_diagnostics(
                ctx,
                body_conversion.warnings,
                body_conversion.losses,
                body_conversion.push_safe,
            )
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/content",
                    body=payload,
                    fmt=fmt.value,
                )
            )
            return

        result = client.add_comment(page_id, body, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="commented", key=page_id)
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# comment reply
# ---------------------------------------------------------------------------


@comment_app.command("reply")
def comment_reply(
    ctx: typer.Context,
    comment_id: str = typer.Argument(..., help="Comment ID to reply to"),
    body_file: str | None = typer.Option(None, "--body-file", "-f", help="Body file path (- for stdin)"),
    body_format: str = typer.Option("storage", "--body-format", help="Body format: storage or md"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Reply to an existing comment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        body_conversion = _resolve_body(body_file, body_format)
        body = body_conversion.body
        client = _make_client(ctx.obj)

        if dry_run:
            payload = {
                "type": "comment",
                "ancestors": [{"id": comment_id}],
                "body": {"storage": {"value": body, "representation": "storage"}},
            }
            _emit_conversion_diagnostics(
                ctx,
                body_conversion.warnings,
                body_conversion.losses,
                body_conversion.push_safe,
            )
            typer.echo(
                format_dry_run(
                    "POST",
                    f"{client.base_url}/rest/api/content",
                    body=payload,
                    fmt=fmt.value,
                )
            )
            return

        result = client.reply_to_comment(comment_id, body, body_format="storage")
        if fmt == OutputFormat.COMPACT:
            output = WriteResult(action="replied", key=str(result.get("id", comment_id)))
            typer.echo(format_output(_output_with_write_diagnostics(ctx, output, fmt, body_conversion), fmt))
        else:
            typer.echo(format_output(_output_with_write_diagnostics(ctx, result, fmt, body_conversion), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# label add
# ---------------------------------------------------------------------------


@label_app.command("add")
def label_add(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    labels: list[str] = typer.Argument(..., help="Labels to add"),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Add labels to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        result = client.add_label(page_id, labels)
        if fmt == OutputFormat.COMPACT:
            typer.echo(format_output(WriteResult(action="labeled", key=page_id, summary=",".join(labels)), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment upload
# ---------------------------------------------------------------------------


@attachment_app.command("upload")
def attachment_upload(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    file: str = typer.Argument(..., help="File path to upload"),
    comment: str | None = typer.Option(None, "--comment", "-c", help="Attachment comment"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Upload a single attachment to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        path = Path(file)
        if not path.exists():
            typer.echo(f"Error: file not found: {file}", err=True)
            raise typer.Exit(1)
        client = _make_client(ctx.obj)
        result = client.upload_attachment(page_id, path, comment=comment)
        if fmt == OutputFormat.COMPACT:
            att_id = None
            if isinstance(result, dict):
                results_list = result.get("results") if isinstance(result.get("results"), list) else None
                first = results_list[0] if results_list else result
                if isinstance(first, dict) and first.get("id"):
                    att_id = str(first["id"])
            typer.echo(format_output(WriteResult(action="uploaded", key=page_id, id=att_id, summary=path.name), fmt))
        else:
            typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment upload-batch
# ---------------------------------------------------------------------------


@attachment_app.command("upload-batch")
def attachment_upload_batch(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Page ID"),
    files: list[str] = typer.Argument(..., help="File paths to upload"),  # noqa: B008
    if_exists: str = typer.Option("skip", "--if-exists", help="Behaviour for existing: skip, replace, version"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Upload multiple attachments to a page."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        for f in files:
            if not Path(f).exists():
                typer.echo(f"Error: file not found: {f}", err=True)
                raise typer.Exit(1)
        client = _make_client(ctx.obj)
        results = client.upload_attachments_batch(page_id, list(files), if_exists=if_exists)
        typer.echo(format_output(results, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# attachment delete
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# page push-md
# ---------------------------------------------------------------------------


@page_app.command("push-md", hidden=True)
def page_push_md(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path; stdin is rejected"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    accept_migration: str | None = typer.Option(
        None,
        "--accept-migration",
        help="Exact migration fingerprint returned by the current preflight",
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Prove and publish a managed Markdown edit, then verify remote read-back."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if md_file == "-":
            raise ValidationError(
                "Managed Confluence push requires --md-file PATH; stdin has no portable manifest identity.",
                context={"reason": "managed_file_required"},
            )
        md_path = Path(md_file)
        if not md_path.exists():
            raise NotFoundError(
                f"Managed Markdown file not found: {md_file}",
                context={"reason": "managed_file_not_found", "path": md_file},
            )

        md_content = read_body(body_file=md_file)
        client = _make_client(ctx.obj)

        from atlassian_skills.confluence.push_md import push_md

        next_action = ["atls", "confluence", "page", "md", "push", page_id, "--md-file", md_file]
        for prefix in passthrough_prefix:
            next_action.extend(("--passthrough-prefix", prefix))
        if if_version is not None:
            next_action.extend(("--if-version", str(if_version)))
        if reason is not None:
            next_action.extend(("--reason", reason))
        if minor_edit:
            next_action.append("--minor-edit")

        result = push_md(
            client,
            page_id,
            md_content,
            passthrough_prefixes=passthrough_prefix or None,
            dry_run=dry_run,
            if_version=if_version,
            managed_path=md_path,
            reason=reason,
            minor_edit=minor_edit,
            accept_migration=accept_migration,
            next_action_argv=tuple(next_action),
        )
        conversion = result.get("conversion")
        if isinstance(conversion, dict) and fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(
                ctx,
                tuple(conversion.get("warnings", ())),
                tuple(conversion.get("losses", ())),
                bool(conversion.get("push_safe", True)),
            )
            result = {key: value for key, value in result.items() if key != "conversion"}
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("prepare-merge", hidden=True)
def page_prepare_merge(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path; stdin is rejected"),
    output_dir: str | None = typer.Option(
        None, "--output-dir", help="Where to write base/local/remote (default: <md-file>.merge/)"
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Write base, local and remote side by side so a stale edit can be merged.

    Writes local files and reads the page. It never publishes: the merge is the
    caller's to make, and this lays out what making it needs.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        if md_file == "-":
            raise ValidationError(
                "Preparing a merge requires --md-file PATH; stdin has no portable manifest identity.",
                context={"reason": "managed_file_required"},
            )
        md_path = Path(md_file)
        if not md_path.exists():
            raise NotFoundError(
                f"Managed Markdown file not found: {md_file}",
                context={"reason": "managed_file_not_found", "path": md_file},
            )

        from atlassian_skills.confluence.prepare_merge import prepare_merge

        client = _make_client(ctx.obj)
        workspace = prepare_merge(
            client,
            page_id,
            md_path,
            output_dir=Path(output_dir) if output_dir else md_path.with_suffix(md_path.suffix + ".merge"),
        )
        typer.echo(format_output(workspace.to_dict(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("recover-assets")
def page_recover_assets(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    body_file: str = typer.Option(..., "--body-file", help="The document whose images should be on the page"),
    asset_dir: str | None = typer.Option(None, "--asset-dir", help="Base directory for relative image paths"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Upload the pictures a page is missing, without touching its body.

    For a create whose uploads did not all land. Rerunning the write does not
    recover them: the page body already is the candidate, so the update finds
    nothing to change and never reaches the uploads.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.local_assets import recover_assets

        path = Path(body_file)
        if not path.exists():
            raise NotFoundError(
                f"Document not found: {body_file}",
                context={"reason": "body_file_not_found", "path": body_file},
            )
        client = _make_client(ctx.obj)
        result = recover_assets(
            client,
            page_id,
            path.read_text(encoding="utf-8"),
            base_dir=_asset_base(body_file, asset_dir),
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("finalize-merge", hidden=True)
def page_finalize_merge(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="The managed Markdown file the merge started from"),
    candidate: str = typer.Option(..., "--candidate", help="The merged body, as reviewed by the caller"),
    output: str | None = typer.Option(None, "--output", "-o", help="Where to write it (default: merged.md beside it)"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Rebind a merged body to the current remote, as a document push-md accepts.

    The merge workspace holds plain Markdown so an agent can read and edit it.
    This turns the result back into a managed document without asking anyone to
    write a manifest by hand.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.prepare_merge import finalize_merge

        for label, name in (("Managed Markdown", md_file), ("Merged body", candidate)):
            if not Path(name).exists():
                raise NotFoundError(
                    f"{label} file not found: {name}",
                    context={"reason": "managed_file_not_found", "path": name},
                )
        client = _make_client(ctx.obj)
        result = finalize_merge(
            client,
            page_id,
            Path(md_file),
            Path(candidate),
            output_path=Path(output) if output else None,
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("pull-xhtml", hidden=True)
def page_pull_xhtml(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    output: str = typer.Option(..., "--output", "-o", help="Where to write the storage document"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Pull a page as storage XHTML, for documents Markdown cannot hold."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.xhtml_workflow import pull_xhtml

        client = _make_client(ctx.obj)
        typer.echo(format_output(pull_xhtml(client, page_id, output_path=Path(output)), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("validate-xhtml", hidden=True)
def page_validate_xhtml(
    ctx: typer.Context,
    xhtml_file: str = typer.Argument(..., help="Local storage document"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Check an edited storage document offline: parse, namespaces, identity."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.xhtml_workflow import validate_xhtml

        path = Path(xhtml_file)
        if not path.exists():
            raise NotFoundError(
                f"Storage document not found: {xhtml_file}",
                context={"reason": "xhtml_file_not_found", "path": xhtml_file},
            )
        result = validate_xhtml(path)
        typer.echo(format_output(result, fmt))
        if result["findings"]:
            raise typer.Exit(1)
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("diff-xhtml", hidden=True)
def page_diff_xhtml(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    xhtml_file: str = typer.Argument(..., help="Local storage document"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Compare a local storage document against what the server holds now."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.xhtml_workflow import diff_xhtml

        path = Path(xhtml_file)
        if not path.exists():
            raise NotFoundError(
                f"Storage document not found: {xhtml_file}",
                context={"reason": "xhtml_file_not_found", "path": xhtml_file},
            )
        client = _make_client(ctx.obj)
        result = diff_xhtml(client, page_id, path)
        if fmt == OutputFormat.JSON:
            typer.echo(format_output(result, fmt))
        else:
            typer.echo(result["diff"] or "Identical (no differences)")
        if not result["identical"]:
            raise typer.Exit(1)
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("push-xhtml", hidden=True)
def page_push_xhtml(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    xhtml_file: str = typer.Option(..., "--xhtml-file", "-f", help="Local storage document"),
    if_version: int | None = typer.Option(None, "--if-version", help="Expected current version (stale check)"),
    accept_candidate: str | None = typer.Option(
        None, "--accept-candidate", help="Exact candidate_sha256 returned by --dry-run"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    reason: str | None = typer.Option(None, "--reason", help="Confluence version message"),
    minor_edit: bool = typer.Option(False, "--minor-edit", help="Mark the Confluence version as a minor edit"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Publish a storage document, only the exact bytes the caller approved."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.xhtml_workflow import push_xhtml

        path = Path(xhtml_file)
        if not path.exists():
            raise NotFoundError(
                f"Storage document not found: {xhtml_file}",
                context={"reason": "xhtml_file_not_found", "path": xhtml_file},
            )
        client = _make_client(ctx.obj)
        result = push_xhtml(
            client,
            page_id,
            path,
            if_version=if_version,
            accept_candidate=accept_candidate,
            dry_run=dry_run,
            reason=reason,
            minor_edit=minor_edit,
        )
        typer.echo(format_output(result, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("set-authority", hidden=True)
def page_set_authority(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    to: str = typer.Option(..., "--to", help="Which representation may publish: markdown|xhtml"),
    md_file: str | None = typer.Option(None, "--md-file", help="Managed Markdown file for this page"),
    xhtml_file: str | None = typer.Option(None, "--xhtml-file", help="Storage document for this page"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Declare which representation publishes this page, so two cannot."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.xhtml_workflow import set_authority

        typer.echo(
            format_output(
                set_authority(
                    page_id,
                    to=to,
                    md_path=Path(md_file) if md_file else None,
                    xhtml_path=Path(xhtml_file) if xhtml_file else None,
                ),
                fmt,
            )
        )
    except AtlasError as e:
        _handle_error(e, fmt)


@page_app.command("validate-local", hidden=True)
def page_validate_local(
    ctx: typer.Context,
    local_file: str = typer.Argument(..., help="Managed Markdown file path"),
    format: str | None = typer.Option(None, "--format", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Validate a portable managed Markdown file without contacting Confluence."""

    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.validate_local import validate_local

        typer.echo(format_output(validate_local(Path(local_file)), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


# ---------------------------------------------------------------------------
# page pull-md
# ---------------------------------------------------------------------------


@page_app.command("pull-md", hidden=True)
def page_pull_md(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    output: str = typer.Option(..., "--output", "-o", help="Required managed Markdown output file"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    resolve_assets: str | None = typer.Option(None, "--resolve-assets", help="Asset resolution mode: sidecar"),
    asset_dir: str | None = typer.Option(None, "--asset-dir", help="Directory for resolved assets"),
    no_assets: bool = typer.Option(
        False, "--no-assets", help="Keep remote asset identity without local materialization"
    ),
    accept_migration: str | None = typer.Option(
        None,
        "--accept-migration",
        help="Exact migration_report_sha256 from this page's refused pull; approves its named losses",
    ),
    write_base_cache: bool = typer.Option(
        False,
        "--write-base-cache",
        help="Also write a .md.atls.json base cache for offline or retention use (not written by default)",
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format (compact|json|md|raw)"),
) -> None:
    """Download a page as managed Markdown with its version/hash binding embedded.

    A page whose grade forbids a canonical write (§8.2) writes nothing and returns
    `not_pulled` with the commands that move it forward. `--accept-migration`
    approves the named losses of a `migration_required` page and only that grade.
    Historical base content stays on the server; `--write-base-cache` is the
    explicit opt-in for a local offline copy.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        output_path = Path(output)

        from atlassian_skills.confluence.pull_md import pull_md

        result = pull_md(
            client,
            page_id,
            output_path=output_path,
            passthrough_prefixes=passthrough_prefix or None,
            resolve_assets=resolve_assets,
            asset_dir=Path(asset_dir) if asset_dir else None,
            site_url=getattr(client, "base_url", None),
            portable=True,
            no_assets=no_assets,
            accept_migration=accept_migration,
            write_base_cache=write_base_cache,
        )
        written = result.status != "not_pulled"
        typer.echo(
            format_output(
                {
                    "status": result.status,
                    # Null when the grade forbade the write. Reporting the path
                    # regardless would name a file that does not exist, and the
                    # next command in the chain would be run against it.
                    "path": str(output_path) if written else None,
                    "version": result.version,
                    "assets": list(getattr(result, "assets", ())),
                    "edit_guidance": list(getattr(result, "edit_guidance", ())),
                    "migration_report": result.migration_report,
                    "migration_report_sha256": result.migration_report_sha256,
                    # What Markdown cannot hold for this page, measured against
                    # the storage that was just read. Emitted at pull time so the
                    # caller knows what kind of document it has before editing
                    # it, instead of finding out when the push is refused.
                    "compatibility": result.compatibility,
                    # Lifted out of `compatibility` on purpose. Everything here
                    # was already true three levels down, and a caller that does
                    # not descend publishes as if the page were plain.
                    "attention_required": bool((result.compatibility or {}).get("attention_required")),
                    "attention_reason": (result.compatibility or {}).get("attention_reason"),
                    "conversion": {
                        **_conversion_diagnostics(result.warnings, result.losses, result.push_safe),
                        "blockers": list(result.blockers),
                    },
                },
                fmt,
            )
        )
        if fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
            _emit_compatibility_diagnostics(ctx, result.compatibility, written=str(output_path))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page pull-batch
# ---------------------------------------------------------------------------


@page_app.command("pull-batch", hidden=True)
def page_pull_batch(
    ctx: typer.Context,
    page_ids: list[str] = typer.Argument(..., help="One or more Confluence page IDs"),
    output_dir: str = typer.Option(..., "--output-dir", help="Root directory for page folders"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="HTML comment prefixes to preserve during Markdown conversion"
    ),  # noqa: B008
    no_assets: bool = typer.Option(
        False, "--no-assets", help="Keep remote asset identity without local materialization"
    ),
    format: str | None = typer.Option(None, "--format", "-f", help="Override output format"),
) -> None:
    """Preflight and publish all managed pages/assets as one durable batch."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        from atlassian_skills.confluence.pull_md import pull_pages_batch

        results = pull_pages_batch(
            client,
            page_ids,
            Path(output_dir),
            passthrough_prefixes=passthrough_prefix or None,
            site_url=getattr(client, "base_url", None),
            portable=True,
            no_assets=no_assets,
        )
        typer.echo(
            format_output(
                [
                    {
                        "page_id": result.page_id,
                        "title": result.title,
                        "path": str(result.path),
                        "version": result.version,
                        "assets": result.assets,
                        "status": result.status,
                        "migration_report": result.migration_report,
                        "migration_report_sha256": result.migration_report_sha256,
                        "conversion": _conversion_diagnostics(
                            result.warnings,
                            result.losses,
                            result.push_safe,
                        )
                        | {"blockers": list(result.blockers)},
                    }
                    for result in results
                ],
                fmt,
            )
        )
        if fmt != OutputFormat.JSON:
            for result in results:
                _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# page diff-local
# ---------------------------------------------------------------------------


@page_app.command("diff-local", hidden=True)
def page_diff_local(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    local_file: str = typer.Argument(..., help="Local markdown file path"),
    passthrough_prefix: list[str] = typer.Option(
        [], "--passthrough-prefix", help="Passthrough prefixes (supported only on push-md/pull-md/diff-local)"
    ),  # noqa: B008
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Compare local markdown file vs server page content."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        local_path = Path(local_file)
        if not local_path.exists():
            typer.echo(f"Error: file not found: {local_file}", err=True)
            raise typer.Exit(1)

        client = _make_client(ctx.obj)

        from atlassian_skills.core.managed_file import read_managed_utf8
        from atlassian_skills.core.managed_manifest import ManagedManifestError, parse_managed_manifest

        local_markdown = read_managed_utf8(local_path, reason="local_markdown_read_failed")
        legacy_manifest = False
        try:
            parse_managed_manifest(local_markdown)
        except ManagedManifestError as manifest_error:
            portable_managed = False
            legacy_manifest = manifest_error.reason == "legacy_binding_marker"
        else:
            portable_managed = True
        if portable_managed:
            import difflib

            from atlassian_skills.confluence.migration_preflight import build_managed_preflight

            managed_preflight = build_managed_preflight(
                client,
                page_id,
                local_path,
                passthrough_prefixes=tuple(passthrough_prefix) if passthrough_prefix else None,
            )
            diff = "".join(
                difflib.unified_diff(
                    managed_preflight.base_markdown.splitlines(keepends=True),
                    managed_preflight.edited_markdown.splitlines(keepends=True),
                    fromfile="base",
                    tofile="local",
                )
            )
            identical = not managed_preflight.would_update
            typer.echo(format_output({**managed_preflight.to_dict(), "diff": diff, "identical": identical}, fmt))
            if not identical:
                raise typer.Exit(1)
            return

        if legacy_manifest:
            raise ValidationError(
                "Legacy managed Markdown must be re-pulled into the portable v2 format",
                context={
                    "reason": "legacy_manifest_repull_required",
                    "path": str(local_path),
                    "page_id": page_id,
                },
            )

        from atlassian_skills.confluence.diff_local import diff_local

        result = diff_local(client, page_id, local_path, passthrough_prefixes=passthrough_prefix or None)
        conversion = _conversion_diagnostics(result.warnings, result.losses, result.push_safe)
        if result.exit_code == 0:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": True, "conversion": conversion}))
            else:
                typer.echo("Identical (no differences)")
        else:
            if fmt == OutputFormat.JSON:
                typer.echo(json.dumps({"identical": False, "diff": result.diff_output, "conversion": conversion}))
            else:
                typer.echo(result.diff_output)
        if fmt != OutputFormat.JSON:
            _emit_conversion_diagnostics(ctx, result.warnings, result.losses, result.push_safe)
        raise typer.Exit(result.exit_code)
    except AtlasError as e:
        _handle_error(e, fmt)


@attachment_app.command("delete")
def attachment_delete(
    ctx: typer.Context,
    att_id: str = typer.Argument(..., help="Attachment content ID"),
    format: str | None = typer.Option(None, "--format", help="Override output format (same as global atls --format)"),
) -> None:
    """Delete a single attachment."""
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        client.delete_attachment(att_id)
        typer.echo(format_output({"deleted": att_id}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Workflow groups
#
# The same function objects registered under a second name, so the two spellings
# cannot drift: there is one implementation, one set of options, one behaviour.
#
# The names existed but the shape did not. `get`/`update` are single actions,
# while `pull-md`, `push-xhtml`, `prepare-merge` and the rest are two managed
# workflows -- and nothing in the naming said which was which, so a reader had to
# know the answer already. Grouping them says it.
#
# Merging these into `get`/`update` behind a flag was considered and rejected:
# one option would change whether a file is written, whether a manifest and
# sidecar are created, which representation may publish, how attachments are
# handled, whether a stale merge is offered, whether an ownership proof runs,
# whether a journal is kept, whether the user is asked, and whether a read-back
# is verified. `get` would also stop meaning "read".
#
# `recover-assets` deliberately stays where it is. It repairs a state-free
# create's images and takes an ordinary body file; under `md` it would read as
# part of the managed workflow, which it is not.
# ---------------------------------------------------------------------------

# --------------------------------------------------------------------------
# the four reconciliation commands (SSOT §7.1-§7.5)
#
# Registered on `md_app` only, further down. §7.1 lists them under `page md` and
# nowhere else -- the flat `page pull-md` spellings exist because they shipped that
# way and are kept as hidden aliases, which is not a reason to invent a flat spelling
# for a command that never had one.
# --------------------------------------------------------------------------


def _managed_file_path(md_file: str, *, what: str) -> Path:
    if md_file == "-":
        raise ValidationError(
            f"{what} requires --md-file PATH; stdin has no portable manifest identity.",
            context={"reason": "managed_file_required"},
        )
    path = Path(md_file)
    if not path.exists():
        raise NotFoundError(
            f"Managed Markdown file not found: {md_file}",
            context={"reason": "managed_file_not_found", "path": md_file},
        )
    return path


def page_compare(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path"),
    view: str = typer.Option("summary", "--view", help="summary|diff"),
    base_file: str | None = typer.Option(
        None, "--base-file", help="A managed document to use as the base if history cannot supply one"
    ),
    write_workspace_dir: str | None = typer.Option(
        None, "--write-workspace", help="Also lay the three versions out in this directory"
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Say what differs between the base, the local file and the page.

    Read-only in both directions: no PUT, and no canonical file written unless
    --write-workspace names a directory. The single comparison command: three-way, so a
    remote edit made since the pull shows up here rather than being discovered by a
    refused push. `--view=diff` renders the same comparison as text.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.reconcile import compare, compare_payload, write_workspace

        md_path = _managed_file_path(md_file, what="Comparing")
        client = _make_client(ctx.obj)
        comparison = compare(client, page_id, md_path, base_file=Path(base_file) if base_file else None)
        payload = compare_payload(comparison, view=view)
        if write_workspace_dir:
            payload["workspace"] = write_workspace(
                comparison, output_dir=Path(write_workspace_dir), managed_path=md_path
            )
        typer.echo(format_output(payload, fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


def page_prepare_reconcile(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path"),
    output_dir: str | None = typer.Option(
        None, "--output-dir", help="Where to write base/local/remote/report (default: <md-file>.reconcile/)"
    ),
    base_file: str | None = typer.Option(None, "--base-file", help="A managed document to use as the base"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Lay out base, local and remote so a stale document can be reconciled.

    Writes only inside the directory it is given. The canonical file is not touched:
    the merge is the caller's to make, and `record-reconciled-against` is what brings
    the result back.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.reconcile import compare, write_workspace

        md_path = _managed_file_path(md_file, what="Preparing a reconciliation")
        client = _make_client(ctx.obj)
        target = Path(output_dir) if output_dir else md_path.with_name(f"{md_path.name}.reconcile")
        comparison = compare(client, page_id, md_path, base_file=Path(base_file) if base_file else None)
        typer.echo(format_output(write_workspace(comparison, output_dir=target, managed_path=md_path), fmt))
    except AtlasError as error:
        _handle_error(error, fmt)


def page_record_reconciled_against(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="The canonical managed file to replace"),
    reconciled_file: str = typer.Option(..., "--reconciled-file", help="The reconciled body, as plain Markdown"),
    compare_fingerprint: str = typer.Option(
        ..., "--compare-fingerprint", help="The fingerprint the comparison returned"
    ),
    base_file: str | None = typer.Option(None, "--base-file", help="A managed document to use as the base"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Rebind a reconciled body to the remote it was reconciled against.

    No PUT. This is the only step in the stale flow that may replace a canonical
    body, and it refuses unless a fresh read still produces the fingerprint the
    comparison did -- so a page that moved, or a file somebody edited meanwhile, ends
    in a named refusal instead of a body reconciled with something that is gone.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.reconcile import record_reconciled_against

        md_path = _managed_file_path(md_file, what="Recording a reconciliation")
        reconciled = Path(reconciled_file)
        if not reconciled.exists():
            raise NotFoundError(
                f"Reconciled file not found: {reconciled_file}",
                context={"reason": "reconciled_file_not_found", "path": reconciled_file},
            )
        client = _make_client(ctx.obj)
        typer.echo(
            format_output(
                record_reconciled_against(
                    client,
                    page_id,
                    md_path,
                    reconciled,
                    compare_fingerprint=compare_fingerprint,
                    base_file=Path(base_file) if base_file else None,
                ),
                fmt,
            )
        )
    except AtlasError as error:
        _handle_error(error, fmt)


def page_rebaseline(
    ctx: typer.Context,
    page_id: str = typer.Argument(..., help="Confluence page ID"),
    md_file: str = typer.Option(..., "--md-file", "-f", help="Managed Markdown file path"),
    accept_remote_baseline: str = typer.Option(
        ..., "--accept-remote-baseline", help="The fingerprint of the comparison you reviewed"
    ),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    """Move the baseline to the current remote, leaving the body alone.

    The narrow way out of a base projection that does not reproduce its recorded hash
    while everything else about the binding checks out. No PUT, no body change, and
    the next push performs the full proof with no waiver.
    """
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        from atlassian_skills.confluence.reconcile import rebaseline

        md_path = _managed_file_path(md_file, what="Rebaselining")
        client = _make_client(ctx.obj)
        typer.echo(
            format_output(rebaseline(client, page_id, md_path, accept_remote_baseline=accept_remote_baseline), fmt)
        )
    except AtlasError as error:
        _handle_error(error, fmt)


md_app = typer.Typer(help="Managed Markdown workflow: pull, edit, prove, publish", no_args_is_help=True)
xhtml_app = typer.Typer(help="Managed storage workflow, for pages Markdown cannot hold", no_args_is_help=True)

md_app.command("pull")(page_pull_md)
md_app.command("push")(page_push_md)
md_app.command("validate")(page_validate_local)
md_app.command("compare")(page_compare)
md_app.command("prepare-reconcile")(page_prepare_reconcile)
md_app.command("record-reconciled-against")(page_record_reconciled_against)
md_app.command("rebaseline")(page_rebaseline)
# Visible, because a refusal names them. `merge_available` offers `prepare-merge` and
# `prepare-merge` offers `finalize-merge`, both as `next_actions` argv a caller is meant
# to run -- and a command the product tells you to run must be a command `--help` admits
# exists. Hidden, the pair made the merge a dead end for anyone discovering the CLI by
# reading its help, which is how an agent discovers it.
#
# Not folded into the reconciliation quartet: `prepare-reconcile` is `compare` plus a
# workspace and answers a baseline that will not reproduce, while this pair three-way
# merges a page that moved. Same-looking names, different questions, different code.
md_app.command("prepare-merge")(page_prepare_merge)
md_app.command("finalize-merge")(page_finalize_merge)
# §7.1: a hidden compatibility spelling, kept working and kept out of the canonical help
# so a new flow cannot reach for it by reading `--help`. `pull-batch` shipped visible in
# 0.3.x and is a removal candidate for the next breaking release.
#
# `diff` is deliberately NOT re-registered here. It was a 0.4.0-only alias for
# `page diff-local`, which answers "what did I change against my base" -- not the
# question `compare` answers, which is "how do the base, my file and the page stand
# against each other". Two spellings one letter apart giving different answers, with the
# one that cannot see a remote edit named `diff`, is a trap; the shipped flat spelling
# `page diff-local` is still there for anything that already calls it.
md_app.command("pull-batch", hidden=True)(page_pull_batch)

xhtml_app.command("pull")(page_pull_xhtml)
xhtml_app.command("push")(page_push_xhtml)
xhtml_app.command("validate")(page_validate_xhtml)
# `compare`, matching `md compare`: both answer "this local file against the page as it
# stands now". `diff` across this CLI means two things that already exist on the server
# -- `page diff` between two versions, `bitbucket pr diff` -- and a local file is not one
# of those. New in 0.4.0 and never published under the other spelling, so there is
# nothing to keep an alias for.
xhtml_app.command("compare")(page_diff_xhtml)
xhtml_app.command("set-authority")(page_set_authority)

page_app.add_typer(md_app, name="md")
page_app.add_typer(xhtml_app, name="xhtml")
