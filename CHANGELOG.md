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

After a manual upgrade, rerun `atls setup all` to refresh the bundled skill files
(`atls upgrade` already does this automatically). Windows, macOS, and Linux all use
the same commands — on Windows they run identically in PowerShell or cmd.

---

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
