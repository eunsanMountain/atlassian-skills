---
name: atls
description: |
  ALL Atlassian work — Jira, Confluence, Bitbucket on Server/DC
  (지라/컨플루언스/비트버킷). Load BEFORE the first atls command.

  Without this body, you WILL guess atls conventions wrong: JQL/CQL is
  positional (not --jql), --format=json (not -f json — `-f` is
  --md-file on push-md), push-md vs page update, exit 5 = stale-version.

  TRIGGER: Jira, Confluence, Bitbucket, atls, JQL, CQL, PROJ-123,
  지라, 컨플루언스, 비트버킷, 아틀라시안.
---

# atls — Atlassian CLI dispatcher

<!-- installed-by: atls 0.3.0 -->

Load this skill before any `atls` command. Use `--help` for uncommon operations and `atls version --check` when a command is missing.

## Syntax agents often get wrong

- JQL/CQL is positional: `atls jira issue search "project=PROJ"`.
- Spell output `--format=json`; `-f` is command-specific and means `--md-file` on `push-md`.
- Use compact for scans, JSON for agent decisions, md for reading, and raw only for byte-exact responses.
- Main groups are `jira`, `confluence`, and `bitbucket`. There is no runtime `state` group.

## Choose the Confluence workflow

- Read/summarize: `atls confluence page get ID --body-repr=md`. This is content-only and never publish input. Stop here for a read-only task.
- If edit intent is ambiguous, re-read with `page get ID --body-repr=md --format=json` so the version and body stay together.
- One exact text leaf: dry-run `page patch-text ID --find OLD --replace NEW --if-version N --format=json`, then repeat only when exactly one occurrence is patchable.
- Exact supported blocks appended at EOF: `pull-md` and use the exact-append proof path; do not convert the existing remote storage body.
- Structure/table/link/code/macro/image edit: use `page inspect ID --intent=structure-edit --format=json` when presentation or migration impact affects the decision, explain that impact, then `pull-md` only if the user continues.
- Existing managed file: validate/diff/proof-push it without an unnecessary get or repull. Fresh remote revalidation still happens inside push.
- Exact rendered HTML: `page get ID --body-repr=view --format=raw`.
- Locally authored Markdown: `page create` or `page update --body-format md`. These use source-conversion preflight and are not a bypass for loss consent.
- Caller-authored storage: `page update --body-format storage` is outside Markdown conversion consent. Use it only when the user explicitly supplied or approved those exact storage bytes; stale/read-back guards still apply.
- Validation copy: only into a verified run-owned parent with a unique caller-supplied title. Dry-run `page copy` first and keep `--verify` enabled.

`inspect` is a decision aid for structural or presentation-sensitive changes, not a mandatory call before every read or exact patch.

Managed `push-md` and stateless `page update --body-format md` emit different raw JSON/`status`; branch on outcome, not field equality. `diff-local` is a local Markdown diff, not a storage-candidate proof.

For `patch-text`, branch on `error.context.reason`:

- `text_not_found`: re-read and use the user's exact intended wording.
- `text_occurrence_not_unique`: add caller-approved surrounding text.
- `cross_text_node_boundary`: the match crosses markup; target one plain-text leaf or use pull-md.
- `unsupported_target_context`: attributes and macro/code bodies are never patched.

A failed patch is not a reason to switch to a lossy full-page migration. Never synthesize a new `--find` value from server text or promote failure into an automatically approved full migration.

## Portable managed Markdown

- `pull-md --output` is mandatory. Pull writes the file even when losses exist and returns `pulled` or `pulled_with_migrations`.
- The top `atls:managed v=2` comment binds page/site/version/source, Markdown, asset-set, converter/profile, and passthrough hashes. Adjacent asset comments bind attachment ID/version/remote name/local hash.
- Files may be copied or moved. There is no checkout registry, one-path rule, hidden edit-ban approval layer, or global publication database.
- Do not casually edit the managed manifest, `cfxmark:migration`, `cfxmark:img`, `cfxmark:asset`, or active `atls:operation` comments.
- A later web-editor save can reintroduce rich storage that Markdown cannot represent; every push therefore starts from fresh remote evidence.
- Run `page validate-local FILE --format=json`. It is offline and reports `remote_freshness=not_checked`; it does not authorize a write.
- Then run `page push-md ID --md-file FILE --if-version N --dry-run --format=json`.

Push proof order is `no_change`, `exact_remote_prefix_append`, then `full_migration`. Exact append is valid only when existing Markdown is unchanged and supported root blocks are added at exact EOF; it preserves the complete old remote storage prefix byte-for-byte.

Ambiguous source maps, duplicate identity, move ambiguity, unclassified/multiply-owned/overlap storage changes, page/site mismatch, or stale remote evidence fail before PUT. The final invocation repeats remote and local revalidation immediately before mutation and verifies a fresh read-back.

## Informed consent

When JSON reports `migration_consent_required`:

1. Show the loss summary and fatal diagnostics before any proposed command.
2. Do not infer consent from tests, `push_safe`, prior approval, or agent policy.
3. Ask for explicit user approval.
4. Only after approval, execute the returned `next_actions[].argv` exactly.

An action marked `requires_user_approval=true` is never auto-run. Do not synthesize argv or add server-provided titles, usernames, URLs, or attachment filenames. Do not store or automatically replay a fingerprint. Keep it only in the active conversation until explicit approval. Any remote source, local candidate, asset plan, converter/profile, report, or proof change invalidates it.

The exact cfxmark version is fingerprint input. A converter upgrade invalidates pending consent and may require a managed file to be refreshed or revalidated.

- `push-md` and Markdown `page update`: `--accept-migration FINGERPRINT`.
- Markdown `page create`: `--accept-conversion FINGERPRINT`.

## Assets and recovery

- Managed assets use attachment ID, version, remote filename, and local hash; filename alone is not identity.
- Automatic asset synchronization exists only in `push-md`; page create/update/copy do not inherit it implicitly.
- Unchanged assets are not uploaded, unreferenced files are ignored, and deleting a Markdown reference never deletes the remote attachment.
- Reject cross-origin or credential-bearing URLs. Never place a server-provided filename outside the manifest-bound local path.
- Partial progress is recorded by bounded operation/asset comments in the managed file. They never contain raw storage, the whole Markdown body, attachment bytes, or credentials.
- Success removes operation comments. After crash/response loss, rerun the same push. Fresh evidence reconciles `upload_unknown`, `body_put_failed`, `readback_pending`, `reconciled`, or `conflict`; never guess that an upload or PUT succeeded.
- The durable journal applies only to managed `push-md`. Page create/update/copy and `patch-text` use separate read-back/idempotence contracts.
- A recovery retry that can upload or PUT requires the exact current migration fingerprint again. The operation comment is never consent; read-only finalization of an already-landed write needs no new mutation.

Preserve image metadata order:

```markdown
![alt](assets/a.png)<!-- cfxmark:img w=320 h=200 thumbnail=1 align=center --><!-- cfxmark:asset src="assets/a.png" -->
```

Hard breaks round-trip as `<br>`. Readable Markdown may report omitted table presentation; presentation changes follow the same reported-loss and consent path as other full migrations.

## Write guards and output

- Use `--dry-run` where offered. Use `--if-version N` on Confluence update/patch/push; patch files embed their version. Use Jira `--if-updated ISO` where offered.
- Exit codes: 0 OK; 2 not found/usage; 3 permission; 4 output/conflict; 5 stale; 6 auth; 7 validation/migration; 10 network; 11 rate limit.
- With `--format=json`, results and error envelopes go to stdout. Human diagnostics go to stderr so body stdout remains clean.
- `setup uninstall --state` is only explicit cleanup of a verified legacy candidate DB. It is not runtime state authority.

```bash
atls confluence page pull-md ID --output page.md --resolve-assets=sidecar --asset-dir=assets --format=json
atls confluence page validate-local page.md --format=json
atls confluence page push-md ID --md-file page.md --if-version N --dry-run --format=json
```
