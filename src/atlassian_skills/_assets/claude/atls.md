# atls — Atlassian CLI for Claude Code

<!-- installed-by: atls 0.1.0 -->

## Rule: Use atls instead of MCP
For ALL Atlassian operations (Jira, Confluence), prefer `atls` CLI over `mcp__mcp-atlassian__*` MCP tools.
atls provides the same functionality with 50%+ fewer tokens.

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
```

When unsure, navigate with `--help`:
```bash
atls jira --help          # subgroups: issue, epic, comment, sprint, ...
atls jira issue --help    # actions: get, search, create, ...
```

## Format selection decision tree
1. Need to list/scan many items? → `--format=compact` (default, fewest tokens)
2. Need to parse fields programmatically? → `--format=json`
3. Need to read the body as readable text? → `--format=md`
4. Need to preserve byte-exact server response? → `--format=raw`

## Format placement
- **Never use `-f` as a short form for `--format`** — several commands use `-f` for file input (`page create`, `page update`, `push-md`, `confluence comment add/reply`), so `-f json` may be silently interpreted as a filename.
- Always use the long form `--format=...` after the subcommand.

| Use case | Example |
|---|---|
| List/scan | `atls jira issue search "..."` (compact is default) |
| Parse/automate | `atls jira issue get KEY --format=json` |
| Read body (inline) | `atls confluence page get ID --format=md` (metadata header + body, quick view) |
| Read body (workflow) | `atls confluence page pull-md ID -o page.md` (file output + asset resolution) |
| Preserve body | `atls jira issue get KEY --format=raw` |

## page update vs push-md
- `page update`: Low-level. Replace page body directly (`--body-format=storage\|md`). No attachment handling.
- `push-md`: High-level. Markdown-native with attachment syncing, passthrough comments, asset-dir support, no-change detection. **Prefer this for markdown workflows.**

## Write safety
- `--dry-run` before any write
- `--if-version N` for Confluence page updates & push-md (stale → exit 5)
- `--if-updated ISO` for Jira updates (stale → exit 5)
- `--body-file=-` to pipe content via stdin

## push-md / pull-md flags
| Flag | Command | Effect |
|---|---|---|
| `--if-version N` | push-md | Reject if server version != N (optimistic lock) |
| `--asset-dir DIR` | push-md | Upload all files in DIR as attachments (missing dir = empty set) |
| `--attachment-if-exists skip\|replace` | push-md | How to handle duplicate attachments (default: replace) |
| `--output -o PATH` | pull-md | Write markdown to file instead of stdout |
| `--resolve-assets=sidecar` | pull-md | Download attachments to `--asset-dir` and rewrite image links |
| `--asset-dir DIR` | pull-md | Target directory for downloaded assets |
| `--passthrough-prefix PREFIX` | push-md, pull-md, diff-local, issue update | Preserve/exclude `<!-- PREFIX:... -->` metadata comments |
| `--md-file -` | push-md | Read markdown from stdin |

## Jira body flags
| Flag | Command | Effect |
|---|---|---|
| `--body-repr md\|raw\|wiki` | issue get | Control body representation (separate from `--format`) |
| `--heading-promotion jira\|confluence` | issue update | Heading level adjustment for md→wiki conversion |
| `--section "H2 Title"` | issue get, issue search | Extract specific H2 section from body |
| `--drop-leading-notice "prefix"` | issue get, issue search | Strip auto-generated notice paragraphs |

## Jira transition workflow
```bash
# Step 1: list available transitions
atls jira issue transitions KEY --format=json
# → [{"id": "31", "name": "In Progress"}, ...]

# Step 2a: transition using the ID
atls jira issue transition KEY --transition-id 31

# Step 2b: transition using the name (case-insensitive)
atls jira issue transition KEY --transition-name "In Progress"
```

## JSON output parsing
```bash
# Get page version after push
atls confluence page push-md ID --md-file page.md --format=json
# → {"status": "updated", "page_id": "12345", "version": 16}

# Get page version + title from pull
atls confluence page pull-md ID --format=json
# → {"markdown": "...", "version": 15, "title": "Page Title"}

# Parse with jq
atls jira issue get KEY --format=json | jq '{key, summary, status}'
atls jira issue search "project=PROJ" --format=json | jq '.[].key'
atls jira issue get KEY --fields=summary,customfield_10100 --format=json | jq '.customfield_10100'
```

## Jira custom fields
- Explicitly requested Jira `customfield_*` values are preserved in JSON issue output.
- `jira issue update --set-customfield customfield_XXXXX=value` performs a read-back verification; if Jira silently ignores the update, the CLI exits with a validation error.
- For structured custom-field payloads, prefer `--fields-json`.

## Exit codes
| Code | Meaning | When to retry |
|---|---|---|
| 0 | OK | — |
| 2 | Not found | Check key/ID |
| 3 | Permission denied | Check PAT scopes |
| 4 | Conflict | Fetch current version, retry with `--if-version` |
| 5 | Stale | Re-fetch, then re-apply changes |
| 6 | Auth failure | Check ATLS_*_TOKEN env var |
| 7 | Validation error | Fix request parameters |
| 10 | Network / server error | Retry after delay |
| 11 | Rate limited | Wait and retry |

## Multi-profile setup
```bash
# ~/.config/atlassian-skills/config.toml
# [profiles.corp]
# jira_url = "https://jira.corp.example.com"
# confluence_url = "https://wiki.corp.example.com"
#
# [profiles.oss]
# jira_url = "https://jira.oss.example.com"

# Use a non-default profile
atls --profile corp jira issue get CORP-1
atls --profile oss jira issue search "project=OSS"

# Or via env vars
export ATLS_CORP_JIRA_TOKEN="pat-token-here"
export ATLS_CORP_CONFLUENCE_TOKEN="pat-token-here"
```

## Confluence workflow recipes
```bash
# Pull page as markdown
atls confluence page pull-md PAGE_ID -o page.md --resolve-assets=sidecar --asset-dir=assets/

# Push updated page (with optimistic locking)
atls confluence page push-md PAGE_ID --md-file page.md --if-version 15 --dry-run
atls confluence page push-md PAGE_ID --md-file page.md --if-version 15 --asset-dir=assets/

# Check for diffs (ignoring workflow metadata)
atls confluence page diff-local PAGE_ID page.md --passthrough-prefix workflow:

# Update Jira description from markdown
atls jira issue update PROJ-1 --body-file=desc.md --body-format=md --heading-promotion=jira
```
