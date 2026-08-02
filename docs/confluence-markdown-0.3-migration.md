# Confluence Markdown 0.3 migration guide

atlassian-skills 0.3.0 makes portable managed Markdown the local source of truth for Confluence editing. The safety boundary is the v2 manifest, fresh remote storage/version, cfxmark's source-bound ownership proof, and exact read-back. There is no global SQLite authority, checkout registry, protected-region approval database, or stored consent fingerprint.

## Choose the workflow

### Read or inspect

```bash
atls confluence page get PAGE_ID --body-repr=md --format=md
atls confluence page get PAGE_ID --body-repr=view --format=raw
atls confluence page inspect PAGE_ID --format=json
```

Readable Markdown and view HTML are not publish input. `inspect` is read-only and recommends the narrowest safe workflow from current conversion evidence.

### Patch one exact text leaf

```bash
atls confluence page patch-text PAGE_ID \
  --find "old text" \
  --replace "new text" \
  --if-version VERSION \
  --dry-run \
  --format=json
```

Proceed only when exactly one decoded plain-text storage leaf is patchable. `text_not_found`, `text_occurrence_not_unique`, `cross_text_node_boundary`, and `unsupported_target_context` are distinct diagnostics. Attributes, macro/code bodies, duplicate or overlapping selectors, and remote drift fail before PUT. Batch patches use a JSON `--patch-file` with a positive `version` and exact `node_path`, `before_fingerprint`, and `after_text` selectors.

### Pull, edit, and push

```bash
atls confluence page pull-md PAGE_ID \
  --output page.md \
  --resolve-assets=sidecar \
  --asset-dir assets \
  --format=json
```

`--output` is mandatory. Pull publishes a managed file even when conversion reports losses, returning `pulled` or `pulled_with_migrations`. The top `atls:managed v=2` comment binds the page, normalized site, remote version/storage hash, baseline Markdown hash, asset-set hash, converter/profile, and passthrough prefixes. Asset comments bind attachment ID, version, remote filename, materialization mode, and local hash.

A reported migration occurrence is not by itself a loss or a consent trigger. Informational occurrences such as `table-cell-background-omitted` are recorded for review while the pull still returns `pulled`; only occurrences that make the candidate unpublishable escalate the pull to `pulled_with_migrations`, and only a full-migration push with unresolved loss returns `migration_consent_required`. A consumer must not treat the mere presence of an occurrence code as a blocker — branch on the pull status and the push proof outcome, not on the occurrence list.

The file does not contain raw storage XHTML, a full hidden baseline copy, credentials, or a machine-local path. It may be copied or moved. A re-pull refuses to overwrite local edits or unsafe/unrelated destinations; a byte-identical refresh preserves the existing file identity and mtime.

Unknown macros and opaque content are not silently described as preserved. They are either proven by cfxmark's current public artifact behavior or reported as migration loss in the file and pull result.

## Local validation

```bash
atls confluence page validate-local page.md --format=json
```

This command is offline. It verifies the v2 manifest, canonical Markdown hash, adjacent asset records, local asset containment, and local hashes. It reports body and asset dirtiness separately, `remote_freshness=not_checked`, and `state_authority=false`. It does not open a database or contact Confluence.

The reported `body.content_sha256` is the canonical Markdown hash with the managed manifest and every `cfxmark:migration`/`cfxmark:migrations`/`cfxmark:notice`/`cfxmark:asset` control comment excluded. Those control comments carry fresh-baseline digests that legitimately change on each pull, so two managed files represent the same body exactly when their `body.content_sha256` values match. Compare that hash — not a byte diff of the whole files — to confirm a round-trip fixed point after a pull/edit/push cycle.

## Remote preflight and proof order

```bash
atls confluence page push-md PAGE_ID \
  --md-file page.md \
  --if-version VERSION \
  --dry-run \
  --format=json
```

Push binds the requested page and configured site to the manifest, fetches fresh remote storage/version and attachment inventory, regenerates the baseline with the manifest's converter/profile, and evaluates these modes in order:

1. `no_change`
2. `exact_remote_prefix_append`
3. `full_migration`

`no_change` performs no PUT. Exact append is available only when the current managed content is the complete baseline byte prefix and all new content is supported root-level Markdown appended at EOF. It converts only the suffix and preserves the entire existing remote storage prefix byte-for-byte. An edit within the baseline, asset dirtiness, ambiguity, or a new blocker disqualifies append.

Full migration uses cfxmark's source-bound proof. Every final storage leaf must have exactly one real Markdown edit-operation or migration-occurrence owner. Unclassified, multiply owned, overlap, incomplete source mapping, duplicate identity, or move ambiguity is fatal before PUT.

### Which edit shapes are provable

The proof is what decides whether an edit lands, and it is not a function of how large or how careful the edit is. Only one shape is provable by construction:

> **Leave existing blocks untouched and add at the end of the document.** That is the exact-append path, and it preserves the old remote storage byte-for-byte.

Anything that deletes or moves an existing block drops to `full_migration`, where the outcome is document-dependent. A minimal example: delete one paragraph and add one paragraph.

| Where the new block goes | Result |
|---|---|
| In the deleted block's position | provable |
| At the end of the document | can fail: `semantic-mapping-ambiguous` |
| At the start of the document | can fail: `semantic-mapping-ambiguous` |
| No deletion, appended at EOF | provable (exact append) |

The failure is a tie: the alignment can read the change as *delete-then-insert* or as *update-then-move* at equal cost, and those two readings splice different remote storage. Reading 1 drops the old node — with any macro, user mention, or inline-comment anchor it carried; reading 2 keeps the node and patches its text. Because both are equally cheap, the publish refuses rather than guess.

This is not about links, images, or block counts. The same three-position table holds for plain text, code spans, and links alike, and a document can grow in total block count and still fail. It is also **not universal** — the same intervention that fails on a small, repetitive document often succeeds on a large one with varied content, because varied content constrains the alignment to a single reading.

So there is no authoring rule beyond the one above. For anything else, run `push-md --dry-run` and branch on `error.context.fatal_class`; `page inspect --intent=structure-edit` predicts the same verdict before you pull.

### Reading a failed proof

A fatal proof reports a stable, value-free `fatal_class` plus an atls-authored description of what could not be decided:

```json
{"error": {"context": {
  "reason": "ownership_proof_invalid",
  "fatal_class": "table-presentation-ambiguous",
  "fatal_class_description": "A table's stored cell presentation could not be matched to exactly one table in the edited Markdown; publishing could move that presentation onto the wrong table",
  "supported_alternatives": ["append_markdown_blocks", "page_patch_text"]
}}}
```

Plain (non-JSON) output prints the message and a hint only, so re-run with `--format=json` to see the class. A failed in-place proof never blocks the append and `patch-text` paths.

### Converter upgrades invalidate managed files

A managed file records the converter that produced it (`converter=cfxmark/X.Y.Z` in the manifest). Upgrading cfxmark therefore fails every existing managed file with `managed_converter_mismatch` until it is re-pulled. Plan converter upgrades so that a batch of managed files is created once, after the upgrade, rather than re-pulled after each one.

## Informed migration consent

A dry-run may report `migration_consent_required` with:

- the loss summary and fatal diagnostics;
- source/candidate/report/proof hashes;
- the current `migration_fingerprint`;
- `next_actions[].argv`.

Display the loss summary before asking for approval. Do not interpret a green test, push-safe flag, or prior fingerprint as consent. On explicit approval, execute the returned argv exactly. It contains CLI constants plus arguments the caller supplied; server titles, usernames, URLs, and attachment filenames are never inserted into it.

The fingerprint is returned only in the response. atls does not store it in the managed file, an operation comment, config, cache, sidecar, or database. Any remote source, page/site identity, local Markdown, asset plan, converter/profile, report, or proof change invalidates it. The final invocation repeats fresh version/hash checks immediately before the first mutation.

The relevant flags are:

- managed push/update: `--accept-migration FINGERPRINT`;
- page create from Markdown: `--accept-conversion FINGERPRINT`.

Never let an agent auto-approve or automatically replay a fingerprint from an earlier response.

## Body and asset writes

Referenced assets are matched by attachment ID, remote version, remote filename, and local hash. Filename alone is never identity. Body and asset dirtiness are independent:

- unchanged referenced assets are not uploaded;
- a changed asset can be synchronized without an unrelated body rewrite;
- unreferenced local files are not uploaded;
- removing a Markdown reference never deletes the remote attachment;
- cross-origin or credential-bearing URLs are rejected before credentials can cross an origin boundary.

The first remote mutation is preceded by a fresh page/version/hash and local-file revalidation. After PUT, atls performs a new GET and verifies the intended source-bound result rather than trusting the PUT response.

## State-free durable recovery

Partial progress is represented by bounded `atls:operation` and per-asset comments in the same managed file. They contain operation/page/source/candidate hashes and receipt identity, never raw storage, the full Markdown body, attachment bytes, or credentials.

States include `asset receipt`, `upload_unknown`, `body_put_not_observed`, `readback_pending`, `reconciled`, and `conflict`. After success the operation comments are removed. After a crash or lost response, rerun the same `push-md`; fresh remote evidence either adopts the exact mutation, retries a proved-unapplied request, or reports a conflict. It never silently retries an unknown create/upload/PUT and never reports success without journal/read-back reconciliation.

A successful managed `push-md` reports `status=reconciled`. This is the journal-finalize vocabulary; it does not by itself mean a recovery happened. An actually adopted response loss is flagged separately by `adopted_response_loss` or `adopted_asset_response_loss` on that same receipt. A stateless `page update --body-format md` success instead reports `status=updated`, and a proven no-op reports `status=no_change` with `put_count=0` on either path — a success, not a skip. The intermediate states above (`readback_pending`, `manual_recovery`, `local_finalize_conflict`, `conflict`, …) and a stale version are not successes and require rerun or reconcile. Branch a receipt consumer on these outcomes, never on field-by-field equality between the managed and stateless transports.

The durable journal applies only to managed `push-md`. Page create/update/copy and `patch-text` use their separate
fresh-read/read-back or idempotent-selector guarantees. A recovery retry that would upload or PUT requires the exact
current migration fingerprint again; an operation marker is not stored consent. If the remote read proves that the
previously approved write already landed, atls may finalize the local journal without another remote mutation.

`page update --body-format=storage` transports explicit caller-provided storage and is outside Markdown-conversion
consent. It remains subject to stale-version and read-back checks. atls requires `cfxmark>=0.6.1,<0.6.2`; because the exact
converter version is fingerprint input, an upgrade invalidates pending consent and requires managed-file revalidation.

Managed files and local assets reject symlink, ancestor-symlink, hardlink/reparse, and destination-identity swaps at their mutation boundaries.

## Page create, update, and copy

Locally authored Markdown uses the same loss-first contract:

```bash
atls confluence page create --space SPACE --title TITLE --body-file page.md --body-format md --dry-run --format=json
atls confluence page update PAGE_ID --body-file page.md --body-format md --if-version VERSION --dry-run --format=json
```

Both require exact informed consent for lossy conversion and verify read-back. Update performs a second remote check before PUT. Create adopts a lost response only when exactly one current version-1 page proves the caller-supplied title/space/parent/storage identity.

`page copy` is for a verified run-owned destination. Dry-run first, use a unique caller-supplied title and verified parent, and keep `--verify` enabled. An unknown create outcome never authorizes attachment upload or cleanup against an unproved page ID.

## Table and image presentation

Readable Markdown reports omitted table backgrounds instead of claiming lossless editability. Managed projection comments expose conversion occurrences; there is no hidden protected-region edit ban or `table-style` state command. A full migration that changes unsupported presentation requires the same source-bound loss report and consent.

Hard line breaks use literal `<br>`. Image metadata stays immediately after the image and before the asset record:

```markdown
![alt](assets/a.png)<!-- cfxmark:img w=320 h=200 thumbnail=1 align=center --><!-- cfxmark:asset src="assets/a.png" -->
```

## Legacy candidate cleanup

Normal 0.3.0 runtime never creates, opens, migrates, or consults the withdrawn candidate's SQLite database. `atls setup uninstall` preserves user content, configuration, credentials, managed Markdown, assets, and remote content by default.

```bash
atls setup uninstall --dry-run
atls setup uninstall --state --dry-run
atls setup uninstall --state --yes
```

`--state` is only an explicit legacy artifact cleanup. It validates the exact platformdirs path, regular-file/symlink identity, SQLite header, atls application ID, and regular WAL/SHM companions without importing the SQLite runtime. Invalid or ambiguous files are preserved.

Old `atls:binding v1` files are not upgraded in place. Push reports `legacy_manifest_repull_required`; preserve any local edits, pull a fresh v2 file to a separate safe path, and reapply the intended edits.

## Release integration note

Local validation installs the exact locally built cfxmark 0.5.0 wheel into a clean environment and records its SHA-256 and site-packages import path. The canonical dependency/lock must not claim a local path or unpublished registry version. Publishing cfxmark and atlassian-skills remains a separately authorized release step.
