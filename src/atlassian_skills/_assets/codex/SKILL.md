---
name: atls
description: Use when the task involves Jira or Confluence and the preferred path is the atls CLI rather than Atlassian MCP tools. Covers command selection, output formats, dry-run safety, and markdown push/pull workflows.
---

# atls — Atlassian CLI Dispatcher

<!-- installed-by: atls 0.1.0 -->

## When to use
Use `atls` instead of `mcp__mcp-atlassian__*` tools for ALL Atlassian operations.

## Format selection
1. List/scan many items? → `--format=compact` (default, fewest tokens)
2. Parse fields programmatically? → `--format=json`
3. Read body as readable text? → `--format=md`
4. Preserve byte-exact server response? → `--format=raw`

## Format placement
- Global: `atls --format=json jira issue get KEY`
- Local: `atls jira issue get KEY --format=json`
- Prefer local `--format=...` after the subcommand.
- If a command already uses `-f` for a file input (`page create`, `page update`, `page push-md`, comment body flags), use the long form `--format`, not `-f`.

## Common patterns
```bash
# Jira
atls jira issue get KEY                    # compact view
atls jira issue search "project=PROJ"      # JQL search
atls jira issue create --project PROJ --type Story --summary "..." --body-file=-
atls jira issue transition KEY --transition-id ID
atls jira issue update KEY --body-file=- --body-format=md --heading-promotion=jira

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
| `--resolve-assets=sidecar` | pull-md | Download attachments, rewrite image links |
| `--passthrough-prefix P` | push-md, pull-md, diff-local | Preserve `<!-- P:... -->` comments |
| `--md-file -` | push-md | Read markdown from stdin |
| `--heading-promotion jira` | issue update | Heading level adjust for md→wiki |

## Exit codes
| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Not found — check key/ID |
| 3 | Permission denied — check PAT scopes |
| 4 | Conflict — fetch current version, use `--if-version` |
| 6 | Auth failure — check ATLS_*_TOKEN env var |
| 11 | Rate limited — wait and retry |

## Jira wiki flags (--format=md)
```bash
atls jira issue get KEY -f md --section "Acceptance Criteria"
atls jira issue get KEY -f md --drop-leading-notice "Auto-generated"
```

## JSON output parsing
```bash
atls confluence page push-md ID --md-file p.md --format=json  # push-md uses -f for --md-file
atls confluence page pull-md ID --format=json                 # → {"markdown":"...","version":15,"title":"..."}
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
