---
name: atls
description: |
  ALL Atlassian work — Jira, Confluence, Bitbucket on Server/DC
  (지라/컨플루언스/비트버킷). Load BEFORE the first atls command.

  Without this body you WILL guess atls conventions wrong: JQL/CQL is
  positional, `-f` is --md-file not --format, managed push differs from
  page update, exit 5 = stale-version.

  TRIGGER: Jira, Confluence, Bitbucket, atls, JQL, CQL, PROJ-123,
  지라, 컨플루언스, 비트버킷, 아틀라시안.
---

# atls — Atlassian CLI dispatcher

<!-- installed-by: atls 0.4.2 -->

Load this skill before any `atls` command. Use `--help` for uncommon operations and `atls version --check` when a command is missing.

## Syntax agents often get wrong

- JQL/CQL is positional: `atls jira issue search "project=PROJ"`.
- Spell output `--format=json`; `-f` means `--md-file` on `md push`.
- compact for scans, JSON for decisions, md for reading, raw for byte-exact.
- `compare` is a local file against the live remote; `diff` is two things already on the server.
- Main groups: `jira`, `confluence`, `bitbucket`. There is no runtime `state` group.

## Invariants

Run only `next_actions[].argv`, exactly as returned; never compose one. `requires_user_approval=true` is never auto-run. Zero PUT before approval or on an unresolved conflict; storage publishes only the approved hash.

## Choose the Confluence workflow

- Read/summarize: `page get ID --body-repr=md`. On `content_complete=false` the Markdown is missing text — re-read with the returned `--body-repr=view` argv before summarizing. Reads change nothing; do it without asking.
- One exact text leaf: dry-run `page patch-text ID --find OLD --replace NEW --if-version N`; repeat only when one occurrence is patchable.
- Structure/table/macro/image edit: `page inspect ID --intent=structure-edit --format=json` first; `in_place_blocked` means only append or `patch-text` lands. Then `md pull`.
- Existing managed file: validate/compare/proof-push without an unnecessary get or repull.
- Locally authored Markdown: `page create` or `page update --body-format md`. Local `![](x.png)` uploads from the body file's directory; `--asset-dir` moves that base, stdin requires it. Every publish re-uploads pictures — prefer `md push` for a page edited twice. On `created_with_missing_images` run the returned `page recover-assets` argv; rerunning the write recovers nothing.
- Storage the user supplied: `page update --body-format storage`.

Managed `md push` and stateless `page update --body-format md` emit different raw JSON/`status`; branch on outcome, not field equality. `md compare` is a three-way Markdown comparison, not a storage-candidate proof.

For `patch-text`, branch on `error.context.reason`: `text_not_found`, `text_occurrence_not_unique` (add approved surrounding text), `cross_text_node_boundary`, `unsupported_target_context`. A failed patch is not a reason to switch to a lossy full-page migration. Never synthesize a new `--find` value from server text.

## What kind of document you pulled

Branch on `attention_required` at the top of the payload; `attention_reason` is the status. Non-JSON runs print it as a WARNING on stderr.

`md pull` and `md push --dry-run` report `compatibility.status`:

- `markdown_ready` edit and publish · `markdown_identity_bound` needs a registered identity capability
- `migration_required` show findings and locations, then ask · `converter_fix_required` our gap, not the author's to approve
- `xhtml_required` uses storage when `canonical_write_permitted` is false

`canonical_write_permitted`, not the grade, decides whether pull writes a file. Applied capabilities are named in `preservation_capability` and `identity_preservation_capability`.
`protected_remote_structures` may be preserved but not edited; such edits fail before PUT.

`workflow_decision_required` chooses the representation; `publish_consent_required` approves this dry-run candidate's loss. Ask only for the latter.

## Portable managed Markdown

- `md pull --output` is mandatory. A file is written only where `canonical_write_permitted` is true, returning `pulled` or `pulled_with_migrations`; otherwise `not_pulled`, `path: null` — run the argv in `next_actions` (`migration_required` gets `--accept-migration`).
- The top `atls:managed v=3` comment binds page/site/version/source, Markdown, asset-set, converter/profile and passthrough hashes; adjacent comments bind attachments. Do not hand-write it. Leave `cfxmark:` comments in place — `cfxmark:list` binds to the next block, so text between them changes the page.
- Files may be copied or moved; no checkout registry. Push and merge need no sidecar.
- Run `page md validate FILE --format=json` (offline), then `page md push ID --md-file FILE --if-version N --dry-run --format=json`. Pull with `--resolve-assets=sidecar --asset-dir=assets` for attachments.

Push proof order is `no_change`, `exact_remote_prefix_append`, then `full_migration`. Append preserves the old storage prefix byte-for-byte and needs existing Markdown unchanged with blocks added at exact EOF; deleting or moving blocks drops to `full_migration`, which can fail closed where append succeeds — dry-run first. Ambiguous source maps, duplicate identity, overlapping storage changes, page/site mismatch or stale evidence fail before PUT; `error.context.fatal_class` names which.
On `ownership_proof_invalid`, execute `next_actions` (compare, prepare, reconcile); never force-publish.

## When the page moved under your edit

`remote_stale` means someone else edited it. Do not repull and retype: a repull discards the
local edit. Only step 4 replaces a canonical body.

1. `page md compare ID --md-file FILE --format=json` — what differs, plus a `fingerprint`.
   Writes nothing; `stale` and `local_dirty` are separate.
2. `page md prepare-reconcile ID --md-file FILE --output-dir DIR --format=json` — writes
   `base`, `local`, `remote`, a report, and no candidate. `base_available: false` names why the
   base is missing; with no base an empty conflict list is not "merges cleanly".
3. Read base→local and base→remote, merge for meaning into a plain file, and ask the author
   only where the two disagree about the same thing.
4. `page md record-reconciled-against ID --md-file FILE --reconciled-file MERGED --compare-fingerprint FP`
   — refuses unless a fresh read still produces `FP`, and names which half moved. The receipt
   carries the body hash before and after.

`manifest_base_projection_mismatch` means the baseline is wrong, not the body:
`page md rebaseline ID --md-file FILE --accept-remote-baseline FP` moves it, body untouched.
`converter_nondeterministic` has no way out.

## Storage workflow, for pages Markdown cannot hold

`page xhtml pull ID --output page.xhtml` → edit → `page xhtml validate page.xhtml` (offline: parse, namespaces, dropped identity) → `page xhtml compare ID page.xhtml` → `page xhtml push ID --xhtml-file page.xhtml --dry-run`, then rerun with `--accept-candidate` and the hash.

Within one directory only one representation publishes: `xhtml pull` makes storage authoritative, so `md push` refuses with `xhtml_is_authoritative`; hand back with `page xhtml set-authority ID --to=markdown --md-file FILE`. A copy elsewhere is not covered — what protects a page is every push re-measuring the fresh remote.

## Informed consent

On `migration_consent_required`: show the loss summary and fatal diagnostics before proposing a command; never infer consent from tests, `push_safe`, prior approval or agent policy; ask, then run the returned argv exactly.

Do not store or replay a fingerprint; keep it in the conversation only. Any change to remote source, local candidate, asset plan, converter/profile, report or proof invalidates it — the exact cfxmark version is fingerprint input, so an upgrade does too. Flags: `--accept-migration` on `md push` and Markdown `page update`, `--accept-conversion` on Markdown `page create`.

## Assets and recovery

- Assets are identified by attachment ID, version, remote filename and local hash; filename alone is not identity. Automatic asset synchronization exists only in Confluence `page md push`: unchanged assets are not re-uploaded, unreferenced files ignored, removing a reference never deletes it. Jira `issue description md push` uploads nothing — see below.
- Reject cross-origin or credential-bearing URLs. Never put a server filename outside the bound path.
- Partial progress is recorded by bounded operation comments in the managed file; they hold no storage, body, bytes or credentials, and are never consent. After a crash rerun the same push: fresh evidence reconciles `upload_unknown`, `body_put_not_observed`, `readback_pending`, `reconciled`, `published_normalized` or `conflict`. `manual_recovery` means the page holds a body that is neither yours nor your source: a person must look, do not rerun. `published_normalized` succeeded and rewrote your file to what the server stored, so a diff after it is expected. This in-file journal is Confluence `page md push` only; the Jira description journal is a sidecar and is described below.

Image metadata order: `![alt](a.png)`, `cfxmark:img`, `cfxmark:asset`.

## Jira issue bodies

Jira Server stores wiki markup; `issue get KEY --body-repr=md` converts it and answers two questions separately.

- `content_complete=false` — text is missing. Re-read with the returned argv before summarizing.
- `write_back_safe=false` — the text is all here, so summarizing is fine; publishing this Markdown back would change the issue. `first_difference` names the line; edit the exact markup instead, via `issue get KEY --fields description --format=raw`. Across a live corpus the text always survived and write-back often did not, so this is never a reason to distrust a summary.
- `attention_reason=requested_projection` — you asked for a `--section`; nothing was lost.

`--body-repr` takes `md|raw|wiki`; `--body-format` and `--comment-format` take `md|wiki`. Anything else is refused, not published raw. `comment add`, `comment edit`, `worklog add` and `issue transition --comment` all take `--comment-format`.

`--fields-json` cannot replace a field the command already set — pass a body one way or the other. `issue update` returns the new `updated` for the next `--if-updated`; `description_matches_sent=false` means the server kept something other than what was sent.

`conversion.attachments` lists the files the body names. The Markdown renders `![](x.png)` whether or not you hold that file, and Jira accepts two attachments under one filename, so filename is not identity there either.

## Editing a Jira description as a file

`issue update` replaces a description in one shot; `issue description` keeps one in a file across edits, bound by `<file>.atls.json`. One representation owns a directory at a time.

- `description wiki pull|validate|compare|push` (`--wiki-file`) — the exact markup, always available.
- `description md pull|validate|compare|push` (`--md-file`) — only where the round trip proves it.
- `description prepare-reconcile|record-reconciled-against|set-authority` (`--file`, `--merged`, `--to`). `--to md` is refused — it would publish an ungraded body. Use `md pull`, which grades one.

`md pull` writes nothing when the grade refuses, and returns a complete `wiki pull` argv — run it unchanged. A description write is **best effort, not conditional**: the endpoint has no precondition, so a save landing between our read and the server applying is overwritten and still reported `updated`.

A description graded `markdown_identity_bound` is editable AND the push re-proves identity: smart links and attachment references round trip, so their values and counts are compared against a fresh read. `wiki_required` with `identity_not_carried` names the construct — a `[~user]` mention is deleted and cannot be regenerated.

**Attachments are carried, never uploaded.** Adding, renaming or removing a reference is refused, as is one whose filename two attachments share.

A readback holding different words is `description_readback_mismatch`, a refusal that keeps the operation file. `push` reports `change_class`: `no_change`, `content_change`, or `whitespace_only_change` — same words, different bytes, possible on a first push because the grade normalises whitespace and the publish does not. Refusals are distinct: `description_remote_changed` (`prepare-reconcile`, then `record-reconciled-against`), `remote_changed_since_prepare`, `candidate_proof_failed`, `description_operation_*`.

A crash leaves `<file>.atls.op.json` — hashes and a stage, no body text. Rerun the same push: it reconciles what landed without a second PUT, retries what did not, or refuses. Never edit it by hand.

## Files this writes to your disk

| what | written by | commit it? | safe to delete? |
| --- | --- | --- | --- |
| `atls:managed` comment, first line of the `.md` | every `md pull`/`md push` | yes — it *is* the page binding | no; the file stops being managed |
| `NAME.md.atls.json` | `md pull` | no | yes — a cache only; push and merge read page history |
| `NAME.xhtml` and its `.atls.json` | `xhtml pull` | your call | not while you intend to push it: the sidecar holds the exact base |
| `NAME.reconciled.md` | `md prepare-reconcile` | no | yes, once `record-reconciled-against` succeeded |
| `atls:operation` comment | mid-publish only | no | no — resume the operation or `md compare` first |

## Write guards and output

- Use `--dry-run` where offered, `--if-version N` on Confluence writes, Jira `--if-updated`.
- Exit codes: 0 OK; 2 not found/usage; 3 permission; 4 output/conflict; 5 stale; 6 auth; 7 validation; 10 network; 11 rate limit.
- With `--format=json`, results and errors go to stdout, diagnostics to stderr.
