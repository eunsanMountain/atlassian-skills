# atlassian-skills 0.3 architecture

This document is the source-level architecture for the 0.3.0 Atlassian Server/Data Center CLI. The CLI is the product; the bundled skill routes an agent to the same public commands and never supplies hidden authority.

## Product boundary

`atls` separates reading, targeted storage edits, and managed Markdown publication:

| Intent | Public path | Publish authority |
|---|---|---|
| Read or summarize | `confluence page get --body-repr=md` | None; content-only Markdown is not publish input |
| Exact rendered view | `confluence page get --body-repr=view --format=raw` | None; exact server-rendered HTML |
| Inspect migration or presentation impact | `confluence page inspect --intent=...` | None; advisory only |
| Replace one exact plain-text storage leaf | `confluence page patch-text` | Fresh version plus exact leaf selection |
| Edit structure, presentation, links, code, macros, or images | `md pull` -> edit -> `md validate` -> `md push --dry-run` -> `md push` | Portable manifest, fresh remote proof, and explicit consent when required |
| Author new Markdown | `page create` or `page update --body-format=md` | Source-conversion preflight and explicit consent when required |

`inspect` is not a mandatory tax on every request. It is used when structural or presentation impact changes the decision.

## Cost-based routing

1. For a read-only request, call readable `page get` and stop.
2. If edit intent is ambiguous, fetch readable Markdown as JSON so body and version stay together.
3. For one exact text change, dry-run `patch-text`. Retry an exact, caller-approved leaf only when the diagnostic permits it. Do not synthesize `--find` from server text and do not auto-escalate failure to a full migration.
4. For supported root blocks appended at exact EOF, pull and let preflight attempt `exact_remote_prefix_append` before any page-wide migration.
5. For a structural or presentation-sensitive edit, inspect with the matching intent, explain the migration impact, and pull only if the user continues.
6. For an existing managed file, validate, diff, and proof-push it without an unnecessary get or repull. Push performs its own fresh remote revalidation.

## Portable managed Markdown

The first line is a canonical `atls:managed v=2` comment. Its fields are:

- `page`: Confluence content identity.
- `site`: normalized-site fingerprint, never a credential-bearing URL.
- `remote_version` and `remote_storage`: the pulled version and storage hash.
- `base_md`: the managed body hash at pull/finalization.
- `assets`: the canonical asset-record set hash.
- `converter` and `profile`: the cfxmark implementation contract.
- `passthrough`: the ordered, canonical passthrough prefixes.

Adjacent `cfxmark:asset` records bind the local materialization path to attachment ID, attachment version, remote filename, and local SHA-256. The document can be copied or moved. There is no checkout registry, one-file-per-page database, global binding, or protected edit region.

Pull uses cfxmark `managed_markdown`, not the source-range body alone. The distinction is intentional:

- `markdown` is the body against which cfxmark source ranges and source maps are measured.
- `managed_markdown` is the projection written to the user's file, including disclosure and occurrence comments.

Pull succeeds whenever the remote storage is parseable. Unsupported, normalized, or omitted constructs are reported in the file and JSON result; they do not make the read fail. A later web-editor save may reintroduce rich storage, so a manifest is never permanent authority over the remote page.

## Source-bound preflight

Every managed push fetches fresh page storage and version, verifies page/site/resource ownership, revalidates the local file and asset set, and evaluates proofs in this order:

1. `no_change`
2. `exact_remote_prefix_append`
3. `full_migration`

Exact append is available only when the complete pulled Markdown prefix is unchanged and supported root blocks were added at exact EOF. It keeps the existing remote storage bytes as a prefix and converts only the appended fragment. A middle edit, changed asset plan, ambiguous boundary, unsupported appended block, or remote drift disqualifies the proof.

Full migration uses cfxmark's S0/M0/C0/M1/C1 ownership proof. Every final storage-leaf change must have exactly one actual migration occurrence ID or one actual Markdown edit-operation ID. Unclassified, multiply owned, migration/edit overlap, incomplete source maps, duplicate identity, or move ambiguity is fatal before PUT.

The last mutation path repeats remote and local revalidation immediately before PUT and verifies a fresh read-back. A stale version, source hash, candidate hash, report, proof, converter/profile, or asset plan cannot reuse prior consent.

## Informed consent

Preflight returns the migration report, fatal diagnostics, source-bound fingerprint, and `next_actions[].argv`. A lossy full migration is not authorized by a green test, `push_safe`, a prior approval, or an agent policy.

The caller must:

1. show the loss summary before the proposed command;
2. ask for explicit user approval;
3. execute the returned argv exactly only after approval.

Actions marked `requires_user_approval=true` are never auto-run. atls does not persist or replay consent fingerprints. An agent may hold the current fingerprint only in the active conversation until approval. `argv` contains CLI constants and arguments supplied by the current user; server titles, usernames, URLs, and attachment filenames are not injected into it.

## Patch-text boundary

`patch-text` targets a single decoded plain-text leaf in fresh Confluence storage, not Markdown syntax. It rejects absent or duplicate text, cross-node matches, overlapping batches, attributes, macro/code bodies, stale versions, and selector fingerprint drift. A dry-run returns the exact selector evidence for a versioned patch file. Failure remains a diagnostic; it is not permission to perform a full-page migration.

## Assets and trust boundaries

Body dirtiness and asset dirtiness are independent. Smart synchronization is a `md push` feature only. Unchanged attachments are not uploaded, unreferenced local files are ignored, and removing a Markdown reference never deletes a remote attachment.

All local asset paths must remain beneath the approved asset root after resolving the file and every ancestor. Symlinks, ancestor symlinks, traversal, and platform reparse escapes fail closed. Server-provided filenames are metadata, not filesystem authority. Cross-origin or credential-bearing URLs are never fetched with Atlassian credentials.

## State-free durable recovery

Recovery authority lives in bounded comments in the managed file, not SQLite or a hidden user-directory database. An operation comment binds operation ID, proof mode, source/candidate hashes, version, page/site, asset-plan hash, and stage. Asset comments bind upload intent and receipt evidence.

Important states include `upload_unknown`, `body_applied_readback_pending`, `readback_pending`, `reconciled`, `manual_recovery`, and `conflict`. A PUT whose response never arrived is `body_put_not_observed`, named for what is known rather than for an outcome: the request may have landed. After a crash or response loss, rerunning the same `md push` compares fresh remote body and attachment evidence with the journal. It either proves reconciliation, safely retries an unapplied operation, or reports a conflict. It never guesses that an upload or PUT succeeded.

The journal never stores raw storage, the full Markdown body, attachment bytes, or credentials. Successful finalization updates the manifest/asset records atomically and removes operation comments. Failed or uncertain work keeps only the bounded recovery evidence.

This durable journal is scoped to managed `md push`. Page create/update/copy and `patch-text` retain their own fresh
read, read-back, and idempotence guarantees but do not claim the same crash journal. A recovery retry that can upload or
PUT must receive the exact current migration fingerprint again; the operation comment never stores or implies consent.
If fresh remote evidence proves the write already landed, read-only reconciliation may finish without another mutation.

Direct `page update --body-format=storage` is a raw-storage transport boundary, not a Markdown conversion. It therefore
does not produce migration consent, but still requires the caller's storage bytes and the ordinary stale/read-back
guards. The supported cfxmark dependency range is `>=0.5.0,<0.6`; the exact converter version participates in managed
file and consent fingerprints, so changing it invalidates pending approvals.

## Removed authority

Version 0.3 has no runtime SQLite state, global binding, checkout relocation lifecycle, protected-region edit policy, or `state` CLI group. `setup uninstall --state` is a narrow cleanup command for a verified legacy candidate database at the exact platform path; it does not open SQLite and is not part of publication.

## Validation and non-goals

`md validate` is deliberately file-only and reports `remote_freshness=not_checked`. It verifies manifest, Markdown, asset records, hashes, and path containment but cannot authorize a remote write.

The supported release proof is local: unit/property/fault-injection tests, quality checks, reproducible source/wheel builds, clean installed CLI and skill parity, and read-only saved corpus evidence. Live Atlassian writes, native Windows verification, push, tag, GitHub Release, and PyPI publication are separate release-operator actions.
