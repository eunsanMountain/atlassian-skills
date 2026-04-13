# Changelog

All notable changes to this project will be documented in this file.

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
