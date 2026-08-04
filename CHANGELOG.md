# Changelog

All notable changes to this project will be documented in this file.

## Upgrading

```bash
# Recommended — auto-detects uv / pipx / pip and refreshes Claude/Codex skill assets in one shot
atls upgrade

# Manual alternatives (if you prefer to run the underlying command yourself)
uv tool upgrade atlassian-skills        # for uv tool installs
pipx upgrade atlassian-skills            # for pipx installs
pip install -U atlassian-skills          # for plain pip installs
```

After a manual upgrade, rerun `atls setup --skills-only` to refresh the bundled skill
files (`atls upgrade` already does this automatically). Windows, macOS, and Linux all
use the same commands — on Windows they run identically in PowerShell, cmd, or Git Bash.

---

## [Unreleased]

## [0.4.3] - 2026-08-04

### Fixed

- **Publishing a document with a picture a second time failed on the first image.**
  `page update`, `page create` and `page md push` place a referenced image by creating
  an attachment, and Confluence answers a create for a filename the page already holds
  with `400 Cannot add a new attachment with same file name as an existing attachment`.
  So a republish stopped there even though the pictures were already correct: the body
  was left unchanged, the page stayed on its old version, and the refusal reported
  `uploaded: []` for files that were on the page. Only the first publish of such a
  document had ever worked.

  A filename the page already stores is now posted to that attachment's own data
  endpoint, which adds a version — the only thing Server/DC can do. `attachment
  upload-batch` was fixed for this in 0.4.1; the path behind every body publish was
  not, and it is the one a Markdown workflow uses.

  Uploading again rather than reusing is unchanged and deliberate: a name and a byte
  count do not prove two files are the same, so a republish costs an attachment
  version and no content. The page's attachment list is read once per publish, and
  only when there is something to upload. A list that cannot be read is not fatal —
  every file then goes to the create endpoint, which is what this path did before, so
  losing the list cannot cost a publish or, on the create path, the id of the page it
  just made.

  Two library seams gained an optional argument, both defaulting to the previous
  behaviour: `ConfluenceClient.upload_attachment(..., attachment_id=...)` posts a
  version of a stored attachment instead of creating one, and
  `upload_assets(..., stored=...)` lets a caller that has already read the attachment
  list pass it rather than pay for a second read. Callers that pass neither are
  unaffected.

  Still outstanding: `confluence page attachment upload` — the single-file command a
  caller drives directly — always creates, so uploading a name the page already holds
  is refused there. Use `attachment upload-batch --if-exists=version` for that today.

- **A refused upload no longer says that re-running will reuse the files.** `page md
  push` offered that hint, and nothing on this path reuses by content — after the fix a
  rerun posts a new version instead. The wording now says so, as the other two paths
  already did.

- **`external_images.count` counted every picture, including the ones being uploaded.**
  The field reports beside `availability_verified: false`, so it means "this page will
  point at something we neither placed nor checked". Counting local pictures that were
  about to become attachments made it a warning about nothing, on exactly the documents
  the fix above is for. It now counts only images the publish leaves pointing elsewhere:
  a document with no remote image reports `0` where it previously reported however many
  pictures it carried. What gets refused is unchanged — an image this path cannot place
  is still refused with `stateless_image_source_unsupported`.

## [0.4.2] - 2026-08-02

### Fixed

- **A full-replacement refusal did not print the command it told you to run.** The hint
  read "run the returned command exactly", but outside `--format=json` nothing was
  returned: no loss summary, no retry line, just the message and the hint. The approval
  fingerprint lives only in the refusal, so there was no way to obtain it short of
  reading the JSON envelope by hand — and a caller that shells out to `atls` and reports
  the exit code showed nothing but `exit 7`.

  The console now prints what the replacement discards and the exact command to approve
  it, including the second approval flag when the rewrite drops macro identities. The
  refusal itself, the approval contract, and the proof are unchanged; only the rendering
  was missing.

- **A displayed retry command is now checked against the shape the CLI produces.** An
  approval flag belonging to a different consent kind, a repeated approval, a companion
  approval carrying a different fingerprint, or a fingerprint with no approval prefix are
  refused rather than shown as something to run. The printed command is executed
  verbatim by whoever reads it, so anything unrecognized fails closed and is withheld.

## [0.4.1] - 2026-08-02

**Requires cfxmark 0.6.1 exactly**, `>=0.6.1,<0.6.2`. The pin is narrow on purpose: the
preservation capabilities name a converter build, and a build they were never measured
against must not inherit them.

### Added

- **A deliberate whole-page rewrite has a path.** Editing a page section for section used
  to have no supported route — the ownership proof asks which stored content an edit
  replaces, and a rewrite cannot answer, so the only advice was "use the web editor".
  The proof is untouched and still refuses. Beside it, `page update --body-format md`
  now accepts `--accept-full-replacement`, and `--accept-discarded-identities` when the
  rewrite drops macro identities.

  Both are fingerprints, and the second exists because the two decisions are different
  ones: "replace this page" and "accept that these identities go away for good". A
  single token would let a retrying caller reuse a printed command line for both. The
  fingerprint binds the remote version, the source and final-candidate hashes, the
  converter, and a hash of the discarded-identity list — so a token stops matching the
  moment any of them moves, including the list itself.

  After approval the page is read again immediately before the write, and the stored
  result is read back after it. Neither check is skipped on this path; they are the only
  ones left. Refusals name which approval is missing or wrong, and no raw macro UUID
  appears in any payload — kind, semantic path, ordinal and count instead.

### Fixed

- **`attachment upload-batch --if-exists=version` did nothing.** Only `skip` was
  implemented; `version` and `replace` fell through to the create endpoint, where
  Confluence answers `400 Cannot add a new attachment with same file name as an existing
  attachment` — a message that never mentions the flag. A page whose images were all
  already attached failed on the first one and took the whole publish with it. Both modes
  now post a new version, which is the only thing Server/DC can do; they are two
  spellings of one operation rather than a `replace` that deletes history to look
  different. An unrecognised mode is refused before any request, so a typo cannot cost a
  version.

## [0.4.0] - 2026-08-01

The version is 0.4.0 rather than 0.3.4 because 0.3.4 was a local milestone that
was never published, and this release makes the Jira description command names
public API. A patch number would have made that new surface a contract without
saying so.


A Jira description you can keep in a file, edit, and publish back with proof
that nothing lost its identity on the way.

**Requires cfxmark 0.6.0.**

### Added

- **The first registered preservation capability: `ragged-table-island-v1`.** §8.2
  used to answer two questions with one word — *can Markdown express this page* and
  *does the managed publish keep it* — and conflating them cost half the live corpus
  its Markdown workflow. A page whose unclassifiable structures lie wholly inside a
  ragged table island may now have a canonical Markdown file written: prose outside
  the island is editable and publishes with the island's exact remote bytes carried
  through, while an edit *inside* it is refused before any PUT with
  `protected_region_edited` and exit 7.

  Deliberately hard to earn. A capability names the exact diagnostic codes it covers,
  is closed by a contract test that publishes through the public `push_md` path, and
  refuses any page whose unknown findings touch text. Merged-cell tables are defined
  and **not** registered — their contract test refused to close them, because the
  managed path rejects the very edit the capability would exist to unlock. Nested
  tables and tables inside macros are excluded and do not become included by
  resembling a shape that is.

- **`page md pull` says which structures are frozen.** A page can be
  `canonical_write_permitted` and still contain regions Markdown may not touch. Those
  are now named on stderr, so "Markdown is allowed here" and "these parts are
  preserved only" stop being one sentence read two ways.

- **`jira issue description`** — eleven commands, in two representations that do
  not publish from the same directory at once. `wiki pull|validate|compare|push`
  holds the markup Jira stores byte for byte and is always available.
  `md pull|validate|compare|push` holds Markdown and is available only where a
  round trip proves the edit could come back. `prepare-reconcile`,
  `record-reconciled-against` and `set-authority` are shared by both.

  They were built and kept hidden until all of them worked. A workflow that can
  pull but not publish, or publish but not recover, is one a caller builds on
  before finding out the other half has no safe answer.

- **`markdown_identity_bound`, and a publish that earns it.** The grade means
  "editable, and publishing verifies identity was carried", so it needed a
  mechanism before it could be given to anything. The push now compares identity
  VALUES and their multiplicity against an issue read freshly in the same call:
  a candidate keeping one of two links, or swapping one attachment's filename
  for another, is refused rather than published.

- **A `wiki_required` refusal says which construct caused it.** Measured through
  the converter rather than assumed: smart links and attachment references round
  trip exactly, and a `[~user]` mention is deleted — its name would have to be
  looked up again, and the lookup can return the wrong person or nobody. Only
  the mention is refused, where previously all three were, which had left every
  description carrying a link or a screenshot unmanageable.

- **Attachments are carried, never uploaded.** Existing references republish
  unchanged and proven unchanged. Adding, renaming or removing one is refused,
  and so is a reference whose filename two attachments share — it then resolves
  to neither in particular.

- **A crash can be concluded rather than guessed.** A publish records what it
  was about to send in `<file>.atls.op.json` — hashes, an issue identity and a
  stage, no body text — and rerunning the same push reads the issue to decide.
  A write that landed is reconciled without a second PUT; one that did not is
  retried exactly once; anything else refuses. Recovery never asks anyone to
  edit state by hand.

### Changed

- **One verb per question: `compare` for a local file against the live remote, `diff`
  for two things that already exist on the server.** `confluence page md compare`
  answered "how do the base, my file and the page stand against each other" while
  `jira issue description md diff` answered the same question under the other name, and
  `confluence page md diff` was a third thing again — the base against the local file,
  which cannot see a remote edit at all. Two spellings one letter apart giving different
  answers, with the one that reads like the obvious choice being the one that misses
  somebody else's edit.

  So `description md diff` and `description wiki diff` are now `compare`, `page xhtml
  diff` is `page xhtml compare`, and `page md diff` is withdrawn. All four were new in
  this release and had never been published, so nothing that ever worked stops working;
  the shipped `page diff-local` spelling is unchanged and still available. `page diff`
  between two versions and `bitbucket pr diff` keep the name, because they are the case
  the name is for.

  The two Jira description group headers were left listing the old word over the new
  command list, so `--help` advertised a `diff` the same wheel then rejected. A test now
  reads every group header against that group's own commands.

- **The merge recovery commands appear in `--help` again.** A refused push returns
  `next_actions` naming `page md prepare-merge` and then `page md finalize-merge`, and
  the Skill tells an agent to run those argv exactly — but both were hidden, so an agent
  that had only `--help` to go on reached a refusal whose one stated way out looked like
  it did not exist. They are visible now. On the Jira side the same actions named the
  hidden `finalize-merge` alias rather than the visible `record-reconciled-against`, and
  now name what is documented.

- **A presentation change the author did not ask for now needs consent.** Publishing
  a page holding an empty paragraph could alter the spacing readers see, and the
  receipt disclosed it without asking. It is now consent-required and carries
  `change_kind: presentation`, so it is distinguishable from a content loss. The
  argument that the platform's editor converges the same form anyway is evidence
  about the editor, not a licence for our REST publish. (cfxmark 0.6.0 removes the
  divergence itself for the bare form.)

- **`publish_consent_required` is the gate's own answer.** It was recomputed from
  `candidate_loss`, which counts named losses and presentation changes — and missed the
  third trigger, a migration occurrence, so an emoticon page reported `false` while the
  gate held `true` and the push refused. SKILL.md tells an agent to branch on this field
  and no other, so the two disagreeing is a public contract contradicting itself: the
  agent does not ask, pushes, and is refused with no way to know why. The field is now
  the value the gate holds rather than a second expression that has to be kept in step.

- **The dependency on cfxmark is pinned to the build the capabilities were measured
  against**, `>=0.6.0,<0.6.1`, rather than the compatible range. The preservation
  registry binds each capability to a converter string so that none is inherited by a
  build nobody measured — correct, but it fails *silently*: on a permitted 0.6.1 both
  capabilities stop matching, `canonical_write_permitted` drops, and nothing is raised.
  The lock pins 0.6.0 so CI could never see it; only a user resolving the newer patch
  would.

- **The identity carry must cover the page, not merely the build.** Registration
  checked converter and profile alone, so every identity-bound page on a matching
  build was granted a canonical write, including identity structures nobody had
  measured. It now asks about the page: the covered attribute list is exactly
  `ac:macro-id`, an unknown finding declines, and macros sharing a content signature
  decline.

- **Jira reconciliation uses Confluence's public names** — `prepare-reconcile` and
  `record-reconciled-against`. One operation had two vocabularies across the two
  products; the old spellings remain as hidden aliases.

- **Both Jira description writers disclose the write window, and both narrow it.**
  `description_push` re-read immediately before its PUT and returned
  `concurrency.guarantee: best_effort`; `wiki push` did neither, so the representation
  documented as always available was the one that said least about what it guarantees
  and left a wider window between deciding and writing. The exact-wiki path now does
  both.

- **The Jira write window is disclosed rather than merely accepted.** Re-reading
  immediately before the PUT catches everything a client can observe, and the interval
  between that response and the server applying it is a property of an API without
  preconditions. Both the dry run and the receipt now say `concurrency.guarantee:
  best_effort` instead of leaving the reader to assume a guarantee.

### Fixed

- **A missing local image no longer reports where it looked on this machine.** The
  refusal carried the absolute resolved path, which is base directory plus the filename
  the caller already has — nothing to act on, and this machine's directory layout and
  account name in a JSON envelope that ends up in an agent transcript. The filename as
  the author wrote it stays, because it is what locates the line to fix. The same for
  the traversal refusal, whose hint printed the base directory.

- **A refused Jira description pull no longer reproduces the description that was
  refused.** The grade lists one entry per loss, each naming the construct it is about,
  so a description with a mention in every paragraph produced an entry per paragraph: on
  one measured body a JSON error envelope 62% the size of the description, out of a
  command that had just declined to write anything. The serialized grade now shows the
  first ten and a `losses_total` count, the same shape the Confluence side already used
  for leaf identities. The full list stays available to a caller holding the grade.

- **The identity carry could be granted without being shown the page.** Its coverage
  check ends by asking whether the page holds two macros a positional walk cannot tell
  apart — a question that needs the page — and the argument carrying it defaulted to
  empty. An empty page answers "nothing found", which reads as "no ambiguity", so a
  caller who omitted it was granted the carry with the last check silently skipped. The
  argument is required now. No shipped path omitted it; the default was a loaded gun on
  the wrong side of a fail-closed rule.

- **An identity-bound page the carry cannot cover no longer recommends a workflow it
  will not write.** Where the registered carry declines a page — two macros with
  identical content signatures, which nothing in a positional walk can tell apart —
  `canonical_write_permitted` flipped to `False` and every other field kept saying the
  opposite: `recommended_workflow: markdown`, `workflow_decision_required: false`, and a
  summary about publishing through the managed path. An agent reading the fields it is
  told to read ran the pull and found no file. The preservation axis had this treatment
  for when its capability applies; the identity axis now has the mirror for when its
  carry does not, with `attention_reason: identity_carry_not_proven_for_this_page`.

- **Asset discovery read Markdown with a hand-written line scanner.** Three separate
  content corruptions came out of it, each found after the previous was patched: a
  narrower fence inside a wider one read as a close, a tab-indented line read as
  opening a fence, and a fence inside a block quote or list item not recognised at
  all. Each could hide a real image from discovery or rewrite an example inside a code
  block. Replaced with `mistletoe`, the tokenizer every document is already parsed
  with, so code ranges come from a parser rather than a growing pile of regular
  expressions.

- **An uncapped diagnostic list could reach a CLI error envelope.** The rule that
  keeps it in-process lived in one helper, and the ownership-refusal path did not call
  it. It is now enforced at the serialization boundary, which covers every raise site
  rather than the remembered ones.

- **A trailing space stopped being reported as a converter defect.** The grade
  is decided on a round trip that ignores blank lines and trailing whitespace,
  because comparing them called every faithful body a change; the publish
  compared bytes. So a paragraph ending in a space graded `markdown_ready` and
  then failed to publish with `converter_drift`, naming a bug that does not
  exist. Publishing is now classified as `no_change`, `whitespace_only_change`
  or `content_change`, reported on push, on dry run and in diff. Allowed, and
  never silent.

- **A description bound to Markdown the server did not keep.** The server may
  store something other than what was sent, and the file was bound to the sent
  Markdown anyway, leaving a pair that does not round trip — so the next push
  reported drift. It now binds Markdown proven to reproduce what is stored.

- **A finalized merge no longer measures against the pre-merge body.** On the
  Markdown side the binding kept its old base while its hash said it was current
  with the remote, and the next push compared the merged text to the wrong
  baseline.

- **A test that rebound the CLI's client factory and did not put it back.**
  Found by sweeping randomised orders: five tests failed in a file that had
  nothing to do with the cause. An autouse guard now fails whichever test leaves
  a factory replaced, so the failure lands where the mistake is.

- **A reference to a file the issue does not have is refused.** Adding one was
  already documented as refused and was not checked: identity compares what the
  base had against what the candidate kept, which is satisfied by a candidate
  that keeps everything and adds one more. It published as a broken image at
  exit 0. The refusal now names the filenames and returns the argv that attaches
  them.

- **An attachment the description never mentions no longer blocks the push.**
  The identity check compared the whole attachment set, so a log dropped on the
  issue during an edit refused the publish — with a finding about attachments,
  on a body that references none. It now compares only the files this
  description points at, which is the question the check exists to ask. The
  refusals that matter are unchanged: replacing a referenced attachment is still
  caught, and each finding now carries its own hint instead of one sentence
  covering six.

- **A publish whose readback holds something else is no longer reported as
  updated.** The comparison was made and left in a field nothing had to read, so
  a concurrent editor arrived as `status: updated` and exit 0 while the issue
  held their text. It is now a refusal that keeps the pending-operation record,
  which is what the next run needs to tell a collision from a fresh local edit.
  A server that only normalises whitespace still publishes, and says so.

- **`description set-authority --to md` is refused instead of publishing an
  ungraded body.** It moved the authority field without grading the stored
  description or converting it, and the publish proof reads a missing Markdown
  baseline as nothing to check — so `safe: true` at exit 0, with a numbered list
  arriving as two H1s and a table as escaped pipes. Pull the issue with
  `description md pull`, which grades it. Handing authority back lands with that
  grading in place.

- **A pending-operation file from a newer version no longer crashes the push.**
  An unknown field raised `TypeError` past the handler, so the traceback went to
  stderr and `--format=json` returned empty stdout with no envelope. It is now
  the same refusal as any other unreadable journal.

## [0.3.3] - 2026-07-28

Diagnosability for failed in-place edits, plus the cfxmark repairs that make a
class of ordinary page editable again.

**Requires cfxmark 0.5.1.** The manifest binds the converter that produced it, so
every existing managed Markdown file fails with `managed_converter_mismatch`
until `page pull-md` is run again, and pending consent fingerprints are
invalidated. That is the designed effect of a converter change — the error now
says so and tells you what to run.

### Fixed
- **A failed in-place proof now says what it could not decide, and what still
  works.** Plain output prints the message and a hint, and these errors carried
  no hint, so `Markdown update has no complete source-bound ownership proof` was
  the entire diagnosis. `--format=json` added a `fatal_class` with no
  explanation. A table-identity clash, an unattributable storage change, and a
  tied edit alignment were indistinguishable, and the only way forward was to
  bisect the document. Every ownership failure now carries a hint, and the JSON
  envelope carries `fatal_class_description` plus a per-diagnostic
  `description`. The five classes an edit actually dies on —
  `table-presentation-ambiguous`, `unclassified-storage-change`,
  `multiple-change-owners`, `semantic-mapping-ambiguous`,
  `semantic-source-map-incomplete` — were all unmapped and now name the question
  the proof could not answer.
- **`managed_converter_mismatch` says how to resolve it.** A converter upgrade
  invalidates every managed file at once, and the error was `not current` with
  no remedy. It now points at `page pull-md`.
- **`page inspect` no longer green-lights a page an in-place edit cannot touch.**
  `styled_cells` counts background colour only, so a page styled entirely by
  Markdown table alignment reported `styled_cells: 0`, `consent_required: false`,
  `recommended_workflow: pull-md` — a clean bill of health for exactly the pages
  where a table edit failed. `features` gains `aligned_cells` and
  `duplicate_table_shapes`, the two inputs to that ambiguity, and
  `styled_cells` keeps its meaning.

### Added
- `page inspect --intent` of `text-edit`, `structure-edit`, or
  `presentation-edit` now returns `edit_guidance`, the same in-place prediction
  `pull-md` already ran. Pull answers "can this be edited in place?" by doing one
  no-edit proof, but an operator deciding *whether to pull* only had inspect, and
  the skill points at inspect for exactly that decision. `read` and `append` do
  not pay for it: the prediction is proportional to page size, `read` writes
  nothing, and `append` has its own exact-append path.

### Changed
- **Requires `cfxmark>=0.5.1`.** That release repairs a table identity that made
  pages with two same-shape tables uneditable when one used column alignment,
  stops adjacent emphasis from injecting literal asterisks, applies the real
  CommonMark flanking rule so `**bold**` followed by a word character is no
  longer escaped into raw HTML (which affected nearly every bold span in
  Korean), and closes a namespace hole in storage comparison. See the cfxmark
  0.5.1 changelog.
- Documentation now states which edit shapes are provable. Only one holds by
  construction: leave existing blocks alone and add at the end of the document.
  Anything that deletes or moves a block drops to `full_migration`, where the
  outcome is document-dependent — the migration guide explains why, and why it is
  not about links, images, or block counts.

---

## [0.3.2] - 2026-07-27

Follow-ups to 0.3.1, reported by @credmond in
[#16](https://github.com/eunsanMountain/atlassian-skills/issues/16). Two were real
regressions; thank you for the fast and precise report.

### Fixed
- **Compressed API responses failed on every command** with `Connection error: Error -3 while
  decompressing data: incorrect header check`. A 0.3.0 regression: the streaming rewrite rebuilds
  each response from already-decoded bytes but kept the original `content-encoding` header, so
  httpx ran the gzip decoder a second time. Any instance that compresses responses (Jira does,
  whenever the client allows it) was unusable.
- **A broken `SSL_CERT_FILE` took every command down** as a redacted `Unexpected internal error` —
  including all of `atls doctor`, the tool meant to diagnose exactly this. `ssl.SSLError` escaping
  from httpx client construction and from the PyPI update check is now caught: the update check
  degrades to "couldn't reach PyPI", requests fail as a validation error that names the trust
  source, and `doctor` verifies that the file actually loads as PEM before anything has to fail.
- `doctor --check-auth` no longer also requires `--resolve-credentials`. `--check-auth` itself is
  the opt-in: the credential is resolved from env, keyring, or command as the profile is
  configured. Plain `doctor` still resolves nothing and makes no calls.

### Added
- **System trust store by default**, via [truststore](https://github.com/sethmlarson/truststore) —
  the mechanism pip uses. With the corporate root CA installed in the OS (the normal corporate
  setup), atls now needs no TLS configuration at all. Precedence: `ca_bundle` →
  `SSL_CERT_FILE`/`SSL_CERT_DIR` → OS trust store (certifi where truststore is unavailable).
- TLS failures now carry the underlying OpenSSL detail (`certificate verify failed: self-signed
  certificate in certificate chain` instead of a bare "TLS verification failed"), and the
  entrypoint's `Unexpected internal error` now names the exception type.

### Changed
- README now recommends `ca_bundle` (scoped to atls) over `SSL_CERT_FILE`, which is process-global:
  uv, pip, and node read it too, and a file they cannot parse breaks them as well — a broken
  `SSL_CERT_FILE` stops uv from building its HTTP client entirely.

## [0.3.1] - 2026-07-27

Diagnosability and TLS/proxy support for corporate networks. Reported by @credmond in
[#16](https://github.com/eunsanMountain/atlassian-skills/issues/16),
[#17](https://github.com/eunsanMountain/atlassian-skills/issues/17),
[#18](https://github.com/eunsanMountain/atlassian-skills/issues/18) and
[#19](https://github.com/eunsanMountain/atlassian-skills/issues/19).

### Added
- `--verbose 1..3` now does something. It was previously accepted and silently ignored. Output goes to stderr only, so
  `--format=json` on stdout stays parseable. Level 1 logs one line per request, level 2 adds headers and the effective
  proxy environment, level 3 adds response shape. Credentials are redacted, URLs are stripped of query/fragment/userinfo,
  and response bodies are never printed at any level.
- `atls doctor --check-auth` calls each configured product and classifies the outcome: authenticated, 401, 403,
  redirect to a login page, off-origin redirect (naming the host), TLS verification failure, or an unreachable host.
  Off by default, so plain `doctor` keeps making no calls to your instance and never prompts for a credential.
- `atls doctor` reports which trust store will verify TLS (`ca_bundle`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, or the bundled
  certifi) and warns that directory-based options need an OpenSSL hashed layout.
- `atls upgrade --system-certs` passes the flag through to `uv tool upgrade` for TLS-inspecting proxies. Opt-in, because
  older `uv` builds reject it. On failure, every install path now prints the corporate-TLS hint.
- README gained a *Corporate network (proxy & TLS)* section covering `ca_bundle`, `SSL_CERT_FILE`, `NO_PROXY` notation,
  and exporting a corporate root CA as PEM on Windows.

### Changed
- An unexpected 3xx is now diagnosed instead of collapsing to `HTTP 302`. The error names the redirect target, reports
  `context.reason` (`redirect_without_location`, `too_many_redirects`, `redirect_not_followed`), and hints at proxy
  configuration. Exit codes are unchanged in every case: a redirect that used to fall through to the generic handler
  still exits `1`, an off-origin redirect still exits `7` (`unsafe_redirect`), and a login redirect still exits `6`.
- **JSON envelope change for 3xx only:** `error.code` is now `"REDIRECT"` instead of the generic `"ATLAS_ERROR"`.
  `exit_code` is unchanged at `1`, so anything branching on exit codes is unaffected — but a consumer matching on the
  `code` string for redirects will see the new value. No other status changes its `code`.
- `Location` is now collected for redirects on every method. Following is still restricted to GET/HEAD — a write is
  never replayed against a new target — but a bounced POST reports where the server tried to send it.
- Human-readable errors now carry a `Request: <method> <url> -> <status>` line when the failure has HTTP context. This
  information was only present in the JSON envelope before, which is why diagnosing #19 required switching formats.
- `ca_bundle` is converted to an `ssl.SSLContext` instead of being passed to httpx as a string. httpx 0.28 deprecated the
  string form; since the dependency has no upper bound, only users with a `ca_bundle` would have broken when it is removed.
- A `ca_bundle` that is missing, empty, or not PEM now fails as a validation error (exit 7) with a hint, instead of
  surfacing a raw `ssl.SSLError` from inside httpx.
- Redirect and login-redirect errors now carry top-level `http_status` / `http_url` / `http_method`.

### Fixed
- `atls --profile NAME doctor` reported the `default` profile regardless of `--profile`. The profile name was hardcoded.

## [0.3.0] - 2026-07-24

### Added
- Portable Confluence managed Markdown v2 manifests that bind page/site/version/source, Markdown, asset-set,
  converter/profile, and passthrough hashes without a machine-local authority database.
- Informed migration preflight for managed push and Markdown page create/update. Lossy writes return a source-bound
  fingerprint and safe `next_actions[].argv`; the fingerprint is never persisted or automatically approved.
- Proof ordering for `no_change`, exact remote-prefix EOF append, and full source-bound migration. Exact append converts
  only the added suffix and preserves all existing remote storage bytes.
- State-free body and attachment recovery using bounded operation comments, exact upload/PUT intent, fresh remote
  reconciliation, and explicit `upload_unknown`, `body_put_not_observed`, `readback_pending`, `reconciled`, and
  `conflict` states. Successful operations remove their comments.
- `confluence page inspect`, content-only readable Markdown, exact server-rendered view HTML, and repeatable canonical
  `--passthrough-prefix` support.
- Exact-leaf `patch-text` with versioned batch selectors, reason/minor-edit metadata, stale checks, response-loss
  reconciliation, and distinct absent/duplicate/boundary/unsupported-context diagnostics.
- Verified client-side `page copy` with source invariants, capability-confined attachment staging, response-loss
  reconciliation, exact read-back, and cleanup restricted to a proved run-owned destination.
- File-only `validate-local`, smart attachment synchronization, local asset containment, and body/asset dirty dimensions.
- Jira R4 converter regression coverage through the required local cfxmark 0.5.0 artifact.

### Changed
- `pull-md --output` is mandatory and always writes a portable artifact, including when informed migration losses are
  present. Status is `pulled` or `pulled_with_migrations`.
- Managed push requires `--md-file PATH`; legacy `--attachment`, `--asset-dir`, and
  `--attachment-if-exists` push flags are removed. Asset identity comes from manifest records plus a fresh remote
  inventory.
- Markdown `page create` and `page update` use the same source-conversion loss report, exact consent, fresh
  revalidation, response-loss reconciliation, and read-back policy as managed publication.
- Managed files can be copied or moved. There is no one-checkout-per-page registry. Byte-identical re-pull preserves file
  identity and mtime.
- Table backgrounds and other unrepresentable presentation are reported as conversion loss/diagnostics instead of hidden
  protected edit state.
- The package requires `cfxmark>=0.5.0,<0.6`. Release locks must resolve the published package; local validation uses a
  provenance-recorded wheel without committing a local path or fictitious registry version. An exact converter-version
  change invalidates pending migration fingerprints and requires managed files to be refreshed or revalidated.
- Deprecated `setup codex|claude|all|paths|status` compatibility shims remain available through 0.3.x.

### Removed
- Withdrawn-candidate global SQLite publication authority, bindings, per-machine migration approvals, presentation state,
  protected-region edit bans, and state lifecycle commands.
- `confluence page migration list|accept|revoke`, `page table-style`, `--allow-stale-managed`, and the `state`
  command group.
- Runtime imports of SQLite state/schema/operation-journal modules. `setup uninstall --state` remains only as explicit,
  header-verified cleanup of a legacy candidate artifact and never opens it as runtime authority.

### Fixed
- Source-bound ownership rejects unclassified, multiply owned, migration/edit overlap, ambiguous source-map, duplicate
  identity, collateral storage, and move ambiguity before PUT.
- Full-migration crash recovery accepts only converter-safe, source-bound Markdown-equivalent server reserialization at
  the expected version, matching the normal read-back policy without persisting raw storage or full Markdown.
- Recovery rechecks exact migration consent immediately before any retry that can upload or PUT. A pending operation
  comment is not consent authority; already-landed results may still be reconciled read-only without another mutation.
- Managed Markdown publication rechecks the exact migration fingerprint at the mutation boundary, while direct storage
  updates remain outside the Markdown-conversion consent contract.
- Human consent errors render bounded per-occurrence impact, before/after, and suggested-workflow details when supplied
  by cfxmark before showing any retry command.
- Remote version/hash and local file identity are checked again immediately before the first mutation; PUT results are
  independently read back.
- Upload and page-create response loss is adopted only from one exact remote identity, preventing duplicate or orphan
  operations from becoming silent success.
- Cross-origin and credential-bearing asset URLs are rejected before credentials can cross an origin boundary.
- Managed paths and assets reject symlink, ancestor-symlink, hardlink/reparse, destination replacement, and unsafe
  filename traversal at publication boundaries. Body publication and recovery retain one directory capability so a
  parent-symlink swap cannot split the journal, remote mutation, and finalization across different files.
- Readable Markdown diagnostics do not contaminate stdout; `view --format=raw` preserves exact server HTML.
- CLI inventory classifies every baseline addition/removal/parameter change and retains published 0.2.13 behavior unless
  the migration contract explicitly changes it.

## [0.2.13] - 2026-07-13

### Added
- **`atls confluence page pull-batch` pulls multiple pages in one process.** It
  reuses one authenticated client, stages referenced sidecar assets across all
  pages, publishes them as one batch, and writes page-ID-qualified directories
  only after asset publication succeeds.

### Changed
- **Native attachment writes remain the default on every platform.** Windows
  users can opt into a per-user compatibility writer, applied across all profiles,
  during `atls setup`. Compatibility mode checks Git Bash, Perl, and `Digest::SHA`
  dependencies during setup,
  then publishes and verifies all files in one external process per batch.
- Confluence single/bulk/`pull-md` downloads and Jira attachment downloads now
  share the same batch primitive. Bulk operations no longer start an external
  writer once per file.

### Fixed
- Compatibility writes use direct `Digest::SHA` file reads and a NUL-delimited
  manifest, so spaces, Unicode, dashes, parentheses, and backslashes cannot alter
  checksum parsing. Failed batches restore existing destinations and fail closed
  instead of silently switching writers.
- **`jira attachment download` now saves attachment files.** The command follows
  Jira's authenticated content URLs, sanitizes and de-duplicates filenames, and
  reports the downloaded paths in every output format.

## [0.2.12] - 2026-07-13

### Fixed
- **Version commands now report the installed package version correctly.** `atls
  version`, `atls --version`, update checks, and doctor output no longer use a stale
  package constant. Release regression coverage now verifies that the module and
  distribution metadata versions match.

## [0.2.11] - 2026-07-13

### Fixed
- **Confluence attachment downloads are now published atomically.** Attachments are
  written to a same-directory temporary file before atomically replacing the
  destination, preventing incomplete files from appearing at the requested path. If
  local publication fails, an existing destination remains unchanged and the CLI
  reports a standard error.
- Existing POSIX file permissions are preserved when replacing a regular file. This
  applies to single downloads, bulk attachment downloads, and `pull-md` sidecar assets.

## [0.2.10] - 2026-07-13

### Fixed
- **`confluence page search` now returns the full requested number of pages
  (follow-up to #14).** The `--limit` count was applied to the raw,
  heterogeneous CQL result set *before* space/user entries were filtered out,
  so a first page padded with non-content hits could yield fewer pages than
  requested even when more content existed further down. Pagination now counts
  filtered content pages and follows `_links.next` until `limit` pages are
  collected.
- **`bitbucket pull-request` list commands now honor `--limit` as a result
  cap.** `_get_paged` previously walked every page and returned all values,
  treating `limit` as the per-page size only. It now stops once `limit` results
  are collected and truncates to `limit`. `pull-request comments` filters
  `COMMENTED` activities *inside* pagination, so `--limit` counts actual
  comments rather than raw activities.

## [0.2.9] - 2026-07-12

### Fixed
- **`confluence page search` no longer crashes on non-page results** (closes #14).
  `/rest/api/search` is a universal CQL search whose results mix content, space, and
  user entities — a broad query such as `siteSearch ~ "..."` matches user profiles and
  spaces. Those results carry no `id`, so validating every result as a `Page` raised an
  unhandled `pydantic.ValidationError` (`id Field required`) that escaped the CLI's error
  handling and dumped a raw traceback. Search now unwraps the `content` wrapper and keeps
  only results that carry an `id`, silently skipping space/user entries.

## [0.2.8] - 2026-05-29

### Added
- **System keyring + shell-command credential storage** (closes #9). A profile's `storage`
  setting now functions beyond env vars:
  - `storage = "keyring"` reads tokens from the OS keyring (macOS Keychain, Windows
    Credential Manager, Linux Secret Service) under service `atls-<profile>`, account
    `<product>_token`. The `keyring` package is now a base dependency (bundled by default).
  - `storage = "command"` runs a shell command that prints the token to stdout
    (1Password `op`, `pass`, Bitwarden `bw`, PowerShell, …), with a 5-second timeout.
- **Per-product credential commands** — `jira_command` / `confluence_command` /
  `bitbucket_command` override a shared `credential_command`, so one profile can pull each
  product's token from a different vault entry.
- **`atls setup` is now keyring-only.** The wizard stores tokens in the OS keyring and nothing
  else — it no longer writes shell rc files, env vars, `~/.secrets`, or `command` config. env
  vars and `command` remain fully supported at runtime (resolver order is unchanged) but are
  configured by hand (see README → Manual setup). Because env outranks the keyring, the wizard
  **detects env-based tokens and skips those products** (a keyring entry would just be shadowed),
  telling you to unset the env var + open a new terminal to switch. It never deletes your env
  vars or shell rc. `storage` flips to keyring only when a token is actually stored, so an
  env-based setup that Enter-throughs the wizard is left untouched.
- **`atls doctor` shows a PyPI freshness banner at the top** — `✓ atls X (up to date)` or
  `⚠ Update available: atls X → Y. Run 'atls upgrade'.` The check is best-effort with a short
  timeout and degrades to a neutral line offline; `--no-update-check` skips the network call.
  (Reuses the same PyPI lookup as `atls version --check`.)
- **`atls auth status --resolve`** and **`atls doctor --resolve-credentials`** — actually
  probe the configured provider (may prompt for Touch ID / a passphrase, or run the shell
  command). Without the flag, both report the configured source only and never touch the
  provider — safe to run repeatedly.
- **Env-shadow warning** — when `storage` is `keyring`/`command` but a live env var resolves
  first (so the configured provider is silently never used), `auth status` / `doctor` now flag
  it explicitly and name the variable to unset.

### Notes
- Priority is **CLI flag > env var > the profile's configured provider**. `storage` selects a
  single provider, not a fallback chain — an env var always wins at resolution time.
- `keyring` is now bundled by default (promoted from optional extra). The `[keyring]` extra is
  retained as a no-op alias so the pre-0.2.8 `pip install "atlassian-skills[keyring]"` keeps working.

Co-authored-by: Doyle <873891+chrisdoyle@users.noreply.github.com>

## [0.2.7] - 2026-05-28

> ⚠️ **Heads-up for 0.3.0**: `atls setup all/codex/claude/paths/status` still work in
> 0.2.7 but emit a deprecation warning on stderr. Their removal was deferred to **0.4.0**
> so 0.3.x upgrades keep existing automation working.
> The replacement for all five is `atls setup` (interactive wizard) plus `atls doctor`
> (diagnostic). Migrate any automation now.

### Added
- **`atls setup` — single interactive wizard.** One command configures Jira / Confluence
  / Bitbucket URLs, Personal Access Tokens, and Claude / Codex / GitHub Copilot skill
  installation in one pass. Replaces the previous 5-step install → URL × 3 → token × 3 →
  `setup all` flow with `install → setup → go`.
- **GitHub Copilot Skill install target** — wizard step [4/4] now offers a third
  install target (`~/.copilot/skills/atls/SKILL.md`, default `Y` like Claude/Codex). Copilot
  auto-discovers `SKILL.md` files in `~/.copilot/skills` per the
  [Copilot Skills docs](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/add-skills),
  and the wizard also injects a routing block into
  [`~/.copilot/copilot-instructions.md`](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions)
  — the Copilot CLI equivalent of Claude's `CLAUDE.md` / Codex's `AGENTS.md`.
  `atls upgrade` (`--skills-only`) refreshes the Copilot skill only when it is
  already installed, so users who never opted in are never surprise-installed.
  Cross-platform: `Path.home()` resolves to `%USERPROFILE%\.copilot` on Windows
  natively, no extra branching. WSL is detected and a one-line note advises the
  user that `~/.copilot` lives in the WSL filesystem (invisible to a native
  Windows Copilot CLI install). `COPILOT_HOME` env var overrides the default.
  Closes #7; the original idea + first PR (#10) came from @akreit — thanks!
- **`atls setup --skills-only`** — silent non-interactive skill refresh. `atls upgrade`
  now invokes this instead of `atls setup all`, so upgrades no longer surface the
  deprecation warning.
- **`atls doctor`** — diagnostic command that prints platform, shell, all resolved
  install paths, skill version markers, legacy notices, and auth resolution in one
  screen. Replaces the diagnostic side of `setup status` / `setup paths`.
- **Token-exposure guards (4-layer)**:
  1. Wizard refuses to run when stdin is not a TTY — protects against AI-agent shell
     tools that would otherwise hang the prompt and surface a token through chat.
  2. `Credential.__repr__` / `__str__` now redact the raw token — tracebacks and logs
     can no longer leak it.
  3. `core/auth.py:Credential` repr/str tests guard against regression.
  4. Wizard explicitly never echoes a freshly-entered token to stdout/stderr; a
     dedicated test asserts the input string never appears in command output.
- **Cross-platform token storage** (configured automatically by the wizard):
  - Linux / macOS: `~/.secrets/{jira,confluence,bitbucket}_pat` (chmod 600) +
    idempotent `# >>> atls env >>>` block in `~/.zshrc` or `~/.bashrc`.
  - Windows: `HKCU\Environment` registry values + `WM_SETTINGCHANGE` broadcast +
    current-process `os.environ` update — works identically from cmd, PowerShell 5/7,
    and Git Bash.

### Changed
- **`atls upgrade`** now calls `atls setup --skills-only` (was: `atls setup all`).
  Same behaviour, no deprecation noise.
- **`Credential.__repr__`** is now redacted by default; any code relying on the old
  full-token repr must read `.token` explicitly.

### Deprecated
- `atls setup all` — use `atls setup` (wizard).
- `atls setup codex` — use `atls setup`.
- `atls setup claude` — use `atls setup`.
- `atls setup paths` — use `atls doctor`.
- `atls setup status` — use `atls doctor`.

All five remain functional in 0.2.7 and emit a stderr warning on each call. They will
be removed in 0.4.0.

### Notes
- **fish shell is detected but not yet supported** by the wizard's token-saving step
  (fish uses `set -gx` instead of `export`). The wizard prints a clear workaround and
  aborts before any prompt; `atls setup --skills-only` still works. Full fish support
  is planned for 0.4.0.

## [0.2.6] - 2026-05-28

> ℹ️ **Migration note**: `atls setup codex` now installs the skill **only** to the
> canonical Codex skills directory `~/.codex/skills/atls/` (`$CODEX_HOME/skills`) —
> the directory Codex shows in its Enable/Disable Skills view. Earlier versions also
> wrote a copy under `~/.agents/skills/atls/`, which Codex now treats as a legacy root
> and which shows up as a **duplicate** skill. The old copy is **not removed
> automatically**; run `atls setup status` to check, then remove it manually with
> `rm -rf ~/.agents/skills/atls` if you no longer need it.

### Fixed
- **Codex skill installs to the canonical `~/.codex/skills/atls/` only.** Previously
  `atls setup codex` mislabeled `~/.agents/skills` as the primary target and
  `~/.codex/skills` as legacy — the reverse of how Codex actually resolves user-level
  skills. Installing to both roots made Codex's Enable/Disable Skills list show `atls`
  twice. Setup now writes one canonical copy and leaves `~/.agents/skills` for
  detection/cleanup only.

### Changed
- `atls setup status` now warns when a legacy `~/.agents/skills/atls/` install is
  present (matching the existing legacy-slash-command warning), pointing at the
  canonical location and the manual removal command. No files are deleted automatically.

## [0.2.5] - 2026-05-08

> ℹ️ **Migration note**: `atls setup claude` no longer installs `~/.claude/commands/atls.md`
> — it now installs an auto-loaded Claude Skill at `~/.claude/skills/atls/SKILL.md`.
> The old slash-command file is **not removed automatically**. After `atls upgrade`, run
> `atls setup status` to check; remove manually with `rm ~/.claude/commands/atls.md`
> if you no longer need it.

### Changed
- **Claude integration is now Skill-first.** The atls guide installs as
  `~/.claude/skills/atls/SKILL.md` so Claude auto-loads it on Atlassian-related
  prompts, instead of requiring the user to type `/atls` each time.
- **Single canonical SKILL.md** at `_assets/skills/atls/SKILL.md` is now shared by
  both Claude and Codex setup, replacing the separate `_assets/claude/atls.md` and
  `_assets/codex/SKILL.md`.
- **Routing blocks in `CLAUDE.md` and `AGENTS.md` are now pure routers** — both
  shrunk to 2 lines that direct the agent to load the skill first and forbid
  inferring atls flags from the routing file. Inline examples (like `--format=md`,
  `-f` warnings) are removed, since they were giving agents false confidence and
  causing them to guess wrong flag names (e.g. `--jql` for `jira issue search`).
- **Skill `description` rewritten as a load-trigger** — failure-mode style ("you
  WILL guess wrong without this body") plus Korean trigger keywords (지라/
  컨플루언스/비트버킷/아틀라시안) so the skill auto-loads on Korean prompts too.

### Added
- `atls setup status` and `atls setup claude` now warn when a legacy
  `~/.claude/commands/atls.md` is detected, with guidance to remove it manually.
  Files with the `installed-by: atls` marker get a different message than
  user-modified files, so manual edits are not silently flagged for deletion.
- `atls setup paths` now shows both the new Claude skill target and the legacy
  command path side by side.

### Removed
- `_assets/claude/atls.md` and `_assets/codex/SKILL.md` are gone — replaced by
  the canonical `_assets/skills/atls/SKILL.md`.

## [0.2.4] - 2026-04-20

> ⚠️ **Upgrading from v0.2.3 on a uv tool install**: `atls upgrade` itself is the bug
> this release fixes, so it will fail with `No module named pip` on v0.2.3. Upgrade
> manually this one time with `uv tool upgrade atlassian-skills` (or `pipx upgrade
> atlassian-skills` for pipx). After v0.2.4 is installed, `atls upgrade` works again
> for future releases.

### Fixed
- **`atls upgrade` misdetected uv tool installs as pip** — `_detect_install_method` called `Path(sys.executable).resolve()`, which followed the uv tool venv's `python` symlink all the way to the uv-managed interpreter (`<data>/uv/python/cpython-.../bin/python3.x`). That resolved path no longer contains the `uv/tools` marker, so the detector fell through to the pip branch and ran `python -m pip install --upgrade atlassian-skills` inside a venv that has no `pip` module, failing with `No module named pip`. Fixed by dropping `.resolve()` — `sys.executable` is already absolute, and keeping the symlink means the `uv/tools/<package>/bin/python` layout stays visible to the detector.

### Added
- `tests/unit/test_upgrade.py::test_detects_uv_when_python_is_symlink_to_uv_managed_interpreter` — regression test that builds a real symlinked layout under `tmp_path` and asserts `_detect_install_method()` still returns `"uv"`.

## [0.2.3] - 2026-04-20

### Fixed
- **Windows cp949/cp932/gbk console crash (#5)** — `atls ... --format=md` and `--format=raw` could no longer run on Korean/Japanese/Chinese Windows locales once a Jira or Confluence body contained an em dash (U+2014), curly quotes, ellipsis, or emoji — the default console encoding (`cp949` on Korean Windows) cannot represent those characters, so `typer.echo` raised `UnicodeEncodeError`. Fixed at the CLI entry point: on Windows, `sys.stdout`, `sys.stderr`, and `sys.stdin` are reconfigured to UTF-8 with `errors="replace"` as a legacy-console safety net.
- A codebase-wide audit of the same pattern caught three additional places that inherited the locale encoding:
  - `core/format/markdown.py:105` and `core/client.py:205` — `print(..., file=sys.stderr)` for cfxmark warnings and HTTP retry notices. Covered by the same `sys.stderr` reconfigure above.
  - `core/stdin.py:24` — `sys.stdin.read()` for `--body-file=-` piping. Covered by the same `sys.stdin` reconfigure; piping a UTF-8 markdown file into `atls jira issue update KEY --body-file=-` no longer crashes on cp949 Git Bash.
  - `cli/upgrade.py:41` — `subprocess.run(..., text=True)` for `uv` / `pipx` / `pip` output. Now explicitly `encoding="utf-8", errors="replace"`, so a non-ASCII line in pip's output cannot break the upgrade flow.

### Added
- `tests/unit/test_windows_encoding.py` — regression coverage for the entry-point reconfigure (Windows vs Linux vs macOS, streams without `reconfigure()`, cp949-backed TextIOWrapper smoke test).

## [0.2.2] - 2026-04-20

### Added
- **`atls version [--check]` subcommand** — shows the installed version; with `--check`, queries PyPI and exits 1 if a newer release is available. Lets agents gate upgrade suggestions on a concrete signal rather than guessing.
- **`atls upgrade` auto-detects uv, pipx, and pip** via `sys.executable` layout and dispatches the right upgrade command. Previously uv-only; pip and pipx users now get a single command that does the right thing. Works identically on Windows, macOS, and Linux.
- `_assets/claude/atls.md` and `_assets/codex/SKILL.md` now include a "When to suggest `atls upgrade`" rule, so Claude/Codex route users through `atls version --check` + `atls upgrade` only when there is a concrete symptom (missing command, stale behavior).
- README documents `uv` installation for Windows (PowerShell) and Linux/macOS (curl), with `pipx` as an explicit alternative.

### Changed
- README Installation section recommends `uv tool install` and clarifies when to pick `pipx` vs plain `pip`.
- README Authentication section gains a Windows native-equivalents block (System Properties GUI, PowerShell `$env:` / `[Environment]::SetEnvironmentVariable`, cmd `setx`) and a previously-undocumented Basic auth block — the code already supported Basic auth via `ATLS_*_AUTH=basic` but it was missing from docs.

## [0.2.1] - 2026-04-18

### Added
- **Jira comment/worklog markdown conversion**: `jira comment add|edit` accepts `--body-format=md` and `jira worklog add` accepts `--comment-format=md` to convert Markdown to Jira wiki markup before POST. Previously the Markdown reached the server literally and rendered as plain text in the Jira UI.
- Compact output (`WriteResult`) for 11 write commands that previously dumped raw JSON under `--format=compact`: `jira comment edit`, `jira worklog add`, `jira link remote-create`, `jira sprint create|update`, `jira project versions-create`, `jira attachment upload`, `jira issue-batch create`, `confluence comment reply`, `confluence page move`, `confluence label add`, `confluence attachment upload`.
- 10 regression tests covering the md→wiki conversion and compact output paths.

### Fixed
- `src/atlassian_skills/__init__.py` `__version__` is now kept in sync with `pyproject.toml` (was stale at `0.1.1`).
- `tests/unit/test_config.py::test_no_legacy_var_for_bitbucket` was stale after `BITBUCKET_TOKEN` legacy fallback was added in 0.2.0; renamed to `test_bitbucket_legacy_token_fallback` and rewritten to assert the intended behavior.

## [0.2.0] - 2026-04-17

### Added
- **Bitbucket Server/DC support** — 33 CLI commands for PR workflow automation
- **PR read** (8 commands): `pr list|get|diff|comments|commits|activity`, `branch list`, `file get`
- **PR write** (10 commands): `pr create|update|merge|decline|approve|unapprove|needs-work|reopen`, `comment add|reply`
- **PR management** (15 commands): `comment update|delete|resolve|reopen`, `task list|get|create|update|delete`, `pr diffstat|statuses|pending-review`
- 13 pydantic models: PullRequest, PullRequestComment, PullRequestActivity, Branch, Commit, BitbucketUser, BitbucketRef, CommentAnchor, PullRequestParticipant, Task, BuildStatus, DiffStat, DiffStatPath
- 8 compact format renderers with PR reviewer summary (`2A/1NW/3R`)
- `BITBUCKET_TOKEN` env var as legacy fallback (compatible with existing Bitbucket MCP servers)
- `auth status` now displays Bitbucket URL and token alongside Jira/Confluence
- `BaseClient.delete()` now accepts `params` kwarg for version-based optimistic locking
- `file get` uses `/raw/{path}` for byte-preserving file content
- `pr diff` returns raw unified diff with `Accept: text/plain`
- `pr comments` extracts comments from `/activities` (Bitbucket Server requires `path` param on `/comments`)
- `_get_current_user_slug()` uses `X-AUSERNAME` header with caching
- Build status fetches from `/rest/build-status/1.0/` (separate API base)
- `pr pending-review` with `/inbox/pull-requests` + dashboard fallback
- Task CRUD via top-level `/rest/api/1.0/tasks` (requires Bitbucket Server 7.2+)
- All write commands support `--dry-run`

### Fixed
- `_safe_server_message` now handles Bitbucket's list-format `errors` field (was crashing on `.items()`)

## [0.1.4] - 2026-04-17

### Fixed
- Confluence `comment add` and `comment reply` now use `POST /rest/api/content` with a `container` field instead of `POST /rest/api/content/{id}/child/comment`, which returns HTTP 405 on Server/DC. `reply_to_comment` now fetches the parent comment to resolve its container page before creating the reply.

## [0.1.3] - 2026-04-17

### Fixed
- `auth status` now resolves URLs from env vars (`ATLS_{PROFILE}_{PRODUCT}_URL`), matching the behavior of actual Jira/Confluence commands. Previously showed "(not configured)" even when the URL was set via environment variable.
- `auth status` now displays the URL source (config vs env) for easier debugging
- `auth login` now includes the URL env var in the export snippet
- `auth list` now shows `confluence_url` alongside `jira_url` and checks env var fallback

### Changed
- Translate all Korean prose to English across CLAUDE.md, docstrings, comments, and fixture docs. Test fixtures and intentional CJK test data are preserved.

## [0.1.2] - 2026-04-16

### Changed
- Remove `[tool.uv.sources]` local cfxmark editable path. cfxmark>=0.4 is now resolved from PyPI in both local development and CI. No impact on published wheel metadata.

### CI
- Add GitHub Actions CI workflow: ruff lint/format + mypy + pytest matrix (Python 3.10-3.13)
- Add release workflow: tag `v*` push triggers `uv build` → PyPI publish → GitHub Release with CHANGELOG section extraction
- Add `tomli` to dev deps so mypy (configured for Python 3.10) can resolve the `sys.version_info` fallback branch in `core/config.py`
- Add PyPI version, Python versions, downloads, License, CI status, GitHub stars badges to README

## [0.1.1] - 2026-04-15

### Fixed
- `create_issue_link` now handles 201 No Content response from Jira Server (#3)

## [0.1.0] - 2026-04-13

### Added
- **Jira read** (23 commands): issue get/search/transitions/dates/sla/images, field search/options, project list/issues/versions/components, board list/issues, sprint list/issues, link list-types/remote-list, worklog list, watcher list, attachment download, dev-info get/get-many, service-desk list/queues/queue-issues, user get
- **Jira write** (23 commands): issue create/update/delete/transition, comment add/edit, worklog add, link create/remote-create/delete, epic link, watcher add/remove, sprint create/update/add-issues, version create/batch, attachment upload/delete, issue-batch create
- **Confluence read** (13 commands): page get/search/children/history/diff/images, space tree, comment list, label list, attachment list/download/download-all, user search
- **Confluence write** (10 commands): page create/update/delete/move, comment add/reply, label add, attachment upload/upload-batch/delete
- **High-level commands**: `confluence page push-md`, `pull-md`, `diff-local`
- **Output formats**: compact (default), json, md, raw (byte-preserving)
- **Authentication**: PAT (Bearer) and Basic auth with env > keyring > config priority
- **Token benchmarks**: L1 >=50% reduction, L2 <400 tokens, L3 91% workflow reduction
- **AI assistant integration**: `atls setup codex|claude` for skill asset installation
- **Write safety**: `--dry-run`, `--if-version`, `--if-updated`, `--body-file` stdin support
- **Jira wiki flags**: `--section`, `--heading-promotion`, `--drop-leading-notice`
- **cfxmark integration**: Jira wiki <-> Markdown <-> Confluence storage conversion
- **Body preprocessing**: Mention normalization, smart link cleanup for Jira Server
- **push-md optimistic locking**: `--if-version N` rejects push when server version differs (exit code 5 STALE)
- **push-md asset directory**: `--asset-dir DIR` uploads all files in a directory as attachments
- **push-md attachment policy**: `--attachment-if-exists skip|replace` controls duplicate handling
- **push-md JSON version**: JSON output always includes `version` field (update, no-change, dry-run)
- **pull-md JSON version**: `--format=json` output includes `version` and `title` fields
- **pull-md asset resolution**: `--resolve-assets=sidecar --asset-dir DIR` downloads attachments and rewrites image links to relative paths
- **diff-local passthrough**: `--passthrough-prefix` excludes metadata comments from diff comparison
- **issue update markdown flags**: `--heading-promotion` and `--passthrough-prefix` for md-to-wiki conversion

### Fixed
- Issue model now flattens nested `fields` from Jira REST API responses
- `get_issue_dates` correctly reads dates from nested `fields` object
- Renamed `PermissionError` to `ForbiddenError` to avoid shadowing Python builtin
- JQL injection prevented by quoting project key in `get_project_issues`
- `--format=md` alias now accepted (previously only `--format=markdown` worked)

### Dependencies
- httpx >=0.27, typer >=0.12, rich >=13, pydantic >=2.5, cfxmark >=0.4, platformdirs
