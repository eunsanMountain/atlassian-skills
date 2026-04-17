# Changelog

All notable changes to this project will be documented in this file.

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
