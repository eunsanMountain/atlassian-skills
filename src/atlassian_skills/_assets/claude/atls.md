# atls — Atlassian CLI for Claude Code

<!-- installed-by: atls 0.1.0 -->

## Rule: Use atls instead of MCP
For ALL Atlassian operations (Jira, Confluence, Bitbucket), prefer `atls` CLI over `mcp__mcp-atlassian__*` MCP tools.
atls provides the same functionality with 50%+ fewer tokens.

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

## JSON parsing
```bash
atls jira issue get KEY --format=json | jq '{key, summary, status}'
atls confluence page push-md ID --md-file page.md --format=json  # → {status, version}
```

## Jira custom fields
- `--fields=customfield_XXXXX` preserves custom fields in JSON output
- `--set-customfield customfield_XXXXX=value` with read-back verification; `--fields-json` for structured payloads

## Exit codes
0=OK, 2=not found, 3=permission, 4=conflict, 5=stale, 6=auth, 7=validation, 10=network, 11=rate-limited

## Multi-profile
```bash
atls --profile corp jira issue get CORP-1  # use named profile
# Env: ATLS_CORP_JIRA_TOKEN, ATLS_CORP_CONFLUENCE_TOKEN, ATLS_CORP_BITBUCKET_TOKEN
```

## Bitbucket recipes
```bash
atls bitbucket pr list PROJ repo --state=OPEN       # list PRs
atls bitbucket pr diff PROJ repo 42                  # unified diff
echo "LGTM" | atls bitbucket comment add PROJ repo 42 --body-file=-  # comment
atls bitbucket pr create PROJ repo --source feat --target main --title "X" --reviewers a,b
atls bitbucket pr merge PROJ repo 42                 # merge
atls bitbucket file get PROJ repo path --ref=dev     # read file without clone
atls bitbucket pr pending-review                     # PRs awaiting review
```

## Confluence recipes
```bash
atls confluence page pull-md ID -o page.md --resolve-assets=sidecar --asset-dir=assets/
atls confluence page push-md ID --md-file page.md --if-version 15 --dry-run
atls confluence page diff-local ID page.md --passthrough-prefix workflow:
atls jira issue update KEY --body-file=desc.md --body-format=md --heading-promotion=jira
```
