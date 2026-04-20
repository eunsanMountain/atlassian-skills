---
name: atls
description: Use when the task involves Jira, Confluence, or Bitbucket and the preferred path is the atls CLI rather than Atlassian MCP tools. Covers command selection, output formats, dry-run safety, and markdown push/pull workflows.
---

# atls — Atlassian CLI Dispatcher

<!-- installed-by: atls 0.1.0 -->

## When to use
Use `atls` instead of `mcp__mcp-atlassian__*` tools for ALL Atlassian operations (Jira, Confluence, Bitbucket).

## When to suggest `atls upgrade`
If the user hits `No such command` on a subcommand documented here, a flag that should exist but is rejected, or behavior the latest CHANGELOG lists as fixed, run `atls version --check` first (opt-in PyPI lookup). If it reports an outdated install (exit 1), suggest `atls upgrade`. Do not run the check on every invocation — only when there is a concrete symptom.

## Command tree
```
atls
├── jira
│   ├── issue        get, search, create, update, delete, transition, transitions, dates, sla, images
│   ├── issue-batch  create
│   ├── epic         link
│   ├── comment      list, add, edit, delete
│   ├── sprint       list, issues, create, update, add-issues
│   ├── board        list, issues
│   ├── field        search, options
│   ├── link         list-types, create, remote-list, remote-create, delete
│   ├── worklog      list, add
│   ├── watcher      list, add, remove
│   ├── attachment   list, upload, download, delete
│   ├── dev-info     get, get-many
│   ├── service-desk list, queues, queue-issues
│   ├── project      list, issues, versions, components, versions-create
│   └── user         get, me
└── confluence
    ├── page         get, search, children, history, diff, images, create, update, delete, move, push-md, pull-md, diff-local
    ├── space        tree
    ├── comment      list, add, reply
    ├── label        list, add
    ├── attachment   list, upload, upload-batch, download, download-all, delete
    └── user         search, me
├── bitbucket
│   ├── project      list
│   ├── repo         list, get
│   ├── pr           list, get, diff, comments, commits, activity, create, update, merge, decline, approve, unapprove, needs-work, reopen, diffstat, statuses, pending-review
│   ├── branch       list
│   ├── file         get
│   ├── comment      add, reply, update, delete, resolve, reopen
│   └── task         list, get, create, update, delete
```

When unsure, navigate with `--help`:
```bash
atls jira --help              # subgroups: issue, epic, comment, sprint, ...
atls jira issue --help        # actions: get, search, create, ...
atls bitbucket --help         # subgroups: project, repo, pr, branch, file, comment, task
atls bitbucket pr --help      # actions: list, get, diff, create, merge, ...
```

## Format selection
1. List/scan many items? → `--format=compact` (default, fewest tokens)
2. Parse fields programmatically? → `--format=json`
3. Read body as readable text? → `--format=md`
4. Preserve byte-exact server response? → `--format=raw`

## Format placement
- **Never use `-f` as a short form for `--format`** — several commands use `-f` for file input (`page create`, `page update`, `push-md`, `confluence comment add/reply`), so `-f json` may be silently interpreted as a filename.
- Always use the long form `--format=...` after the subcommand.

## page update vs push-md
- `page update`: Low-level. Replace page body directly (`--body-format=storage|md`). No attachment handling.
- `push-md`: High-level. Markdown-native with attachment syncing, passthrough comments, asset-dir, no-change detection. **Prefer this for markdown workflows.**

## Common patterns
```bash
# Jira
atls jira issue get KEY                    # compact view
atls jira issue search "project=PROJ"      # JQL search
atls jira issue create --project PROJ --type Story --summary "..." --body-file=-
atls jira issue update KEY --body-file=- --body-format=md --heading-promotion=jira
atls jira comment add KEY --body-file=- --body-format=md    # md → Jira wiki (also: comment edit, worklog add --comment-format=md)

# Jira transition (2-step: discover ID, then transition)
atls jira issue transitions KEY --format=json   # → [{"id":"31","name":"In Progress"},...]
atls jira issue transition KEY --transition-id 31
# Or by name (case-insensitive):
atls jira issue transition KEY --transition-name "In Progress"

# Confluence
atls confluence page get ID                # compact view
atls confluence page search "CQL query"
atls confluence page push-md ID --md-file page.md --if-version 15
atls confluence page push-md ID --md-file page.md --asset-dir=assets/
atls confluence page pull-md ID --output page.md --resolve-assets=sidecar --asset-dir=assets/
atls confluence page diff-local ID page.md --passthrough-prefix workflow:
```

## Write safety
- Always use `--dry-run` before write operations
- Use `--if-version N` for Confluence updates & push-md (reject if stale → exit 5)
- Use `--if-updated ISO` for Jira updates (stale check → exit 5)
- Use `--attachment-if-exists skip|replace` to control duplicate attachments (push-md)

## Key flags
| Flag | Commands | Effect |
|---|---|---|
| `--if-version N` | push-md, page update | Optimistic lock (exit 5 if stale) |
| `--asset-dir DIR` | push-md, pull-md | Batch attach / download assets (missing dir on push-md = empty set) |
| `--output -o PATH` | pull-md | Write markdown to file instead of stdout |
| `--resolve-assets=sidecar` | pull-md | Download attachments, rewrite image links |
| `--passthrough-prefix P` | push-md, pull-md, diff-local, issue update | Preserve `<!-- P:... -->` comments |
| `--md-file -` | push-md | Read markdown from stdin |
| `--body-repr md\|raw\|wiki` | issue get | Control body representation (separate from `--format`) |
| `--body-format md` / `--comment-format md` | jira issue/comment/worklog, confluence page/comment writes | md → server format (Jira wiki / Confluence storage) |
| `--heading-promotion jira` | issue update, issue get, issue search | Heading level adjust for md↔wiki |
| `--section "H2 Title"` | issue get, issue search | Extract specific H2 section from body |

## Exit codes
| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Not found — check key/ID |
| 3 | Permission denied — check PAT scopes |
| 4 | Conflict — fetch current version, use `--if-version` |
| 5 | Stale — re-fetch, then re-apply changes |
| 6 | Auth failure — check ATLS_*_TOKEN env var |
| 7 | Validation error — fix request parameters |
| 10 | Network / server error — retry after delay |
| 11 | Rate limited — wait and retry |

## Jira wiki flags (--format=md)
```bash
atls jira issue get KEY --format=md --section "Acceptance Criteria"
atls jira issue get KEY --format=md --drop-leading-notice "Auto-generated"
```

## JSON output parsing
```bash
atls confluence page push-md ID --md-file p.md --format=json  # push-md uses -f for --md-file
atls confluence page pull-md ID --format=json                  # → {"markdown":"...","version":15,"title":"..."}
atls jira issue get KEY --format=json | jq '{key, summary}'
atls jira issue search "project=PROJ" --format=json | jq '.[].key'
atls jira issue get KEY --fields=summary,customfield_10100 --format=json | jq '.customfield_10100'
```

## Custom fields
- Explicitly requested Jira `customfield_*` values are preserved in JSON issue output.
- `jira issue update --set-customfield customfield_XXXXX=value` performs a read-back verification; if Jira silently ignores the update, the CLI exits with a validation error.
- Use `--fields-json` instead of `--set-customfield` when the Jira field expects a structured payload.

## Multi-profile setup
```bash
# Use non-default profile
atls --profile corp jira issue get CORP-1

# Env vars
export ATLS_CORP_JIRA_TOKEN="pat-token-here"
export ATLS_CORP_CONFLUENCE_TOKEN="pat-token-here"
```
