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
  reconciliation, and explicit `upload_unknown`, `body_put_failed`, `readback_pending`, `reconciled`, and
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
