# atlassian-skills

Token-efficient CLI for Atlassian Server/DC (Jira, Confluence, Bitbucket, Bamboo).

Designed as a drop-in replacement for mcp-atlassian MCP server with **>=50% token reduction** for LLM agent workflows.

## Why atlassian-skills?

| | mcp-atlassian (MCP) | atlassian-skills (CLI) |
|---|---|---|
| Token overhead (L2 schema) | ~15,000 tokens | <400 tokens |
| Response size (L1 payload) | Full JSON | 7-34% of MCP |
| Workflow (L3 end-to-end) | Baseline | 91% reduction |
| Jira body preservation | Drops special chars | Byte-preserving |
| Server/DC support | Partial | Full (primary target) |
| Bitbucket/Bamboo | No | Planned (0.2.0) |

## Installation

```bash
# After publishing to PyPI: install as a CLI tool
uv tool install atlassian-skills

# Or via pip
pip install atlassian-skills

# Verify
atls --version
```

### Local install before PyPI publish

```bash
# From the repo root
uv tool install -e .

# Or from anywhere
uv tool install -e /path/to/atlassian-skills

# Reinstall after entrypoint/metadata changes
uv tool install --force -e .
```

`uv tool install atlassian-skills` only works after the package is available from a package index (for example PyPI). Before publish, install from the local path.

## Authentication

Server/DC only. PAT (Bearer) is default.

### 1. Create Personal Access Tokens

Generate PATs from your Atlassian instance:
- Jira: `https://your-jira.example.com` -> Profile -> Personal Access Tokens -> Create
- Confluence: `https://your-confluence.example.com` -> Profile -> Personal Access Tokens -> Create

### 2. Configure server URLs

```bash
atls config set profiles.default.jira_url https://your-jira.example.com
atls config set profiles.default.confluence_url https://your-confluence.example.com
```

### 3. Set tokens

Choose one of:

**Option A: Environment variables (recommended)**

Add to `~/.zshrc` or `~/.bashrc`:

```bash
# mcp-atlassian compatible (works with both mcp-atlassian and atls)
export JIRA_PERSONAL_TOKEN="your-jira-pat"
export CONFLUENCE_PERSONAL_TOKEN="your-confluence-pat"
```

For multi-profile setups:
```bash
export ATLS_CORP_JIRA_TOKEN="..."
export ATLS_CORP_CONFLUENCE_TOKEN="..."
```

**Option B: Secure file storage**

```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
printf '%s' 'YOUR_JIRA_PAT'       > ~/.secrets/jira_pat
printf '%s' 'YOUR_CONFLUENCE_PAT' > ~/.secrets/confluence_pat
chmod 600 ~/.secrets/jira_pat ~/.secrets/confluence_pat

# Then in ~/.zshrc or ~/.bashrc:
[ -f ~/.secrets/jira_pat ]       && export JIRA_PERSONAL_TOKEN="$(cat ~/.secrets/jira_pat)"
[ -f ~/.secrets/confluence_pat ] && export CONFLUENCE_PERSONAL_TOKEN="$(cat ~/.secrets/confluence_pat)"
```

**Priority**: CLI flags > `ATLS_*` vars > `JIRA_PERSONAL_TOKEN`/`CONFLUENCE_PERSONAL_TOKEN` > config file

### 4. Verify

```bash
atls auth status
```

## AI Agent Integration

atls is a CLI replacement for the MCP server. To let your AI agent discover and use atls automatically, run the setup command.

### Setup

```bash
# Install for both Claude Code and Codex
atls setup all

# Or install individually
atls setup claude   # → ~/.claude/commands/atls.md (slash command) + CLAUDE.md block
atls setup codex    # → ~/.agents/skills/atls/ + ~/.codex/AGENTS.md block

# Check installation status
atls setup status
```

### Claude Code

`atls setup claude` installs the `/atls` slash command and injects a directive block into `~/.claude/CLAUDE.md`. Two ways to use it:

**Option 1: Slash command (manual)**

Type `/atls` during a conversation to load the full atls usage guide into context. Run it once before starting Atlassian work.

**Option 2: CLAUDE.md directive (automatic, recommended)**

`atls setup claude` automatically adds a block to `~/.claude/CLAUDE.md` so Claude recognizes atls in every conversation. You can also add a project-level directive to your project's `CLAUDE.md`:

```markdown
## Atlassian
- Use `atls` CLI for all Atlassian (Jira, Confluence) operations instead of mcp-atlassian MCP.
- Reference: `/atls` command or `atls <command> --help`.
- Always run `--dry-run` before write operations.
```

### Codex

`atls setup codex` installs the skill at `~/.agents/skills/atls/SKILL.md` and injects a short routing block into `~/.codex/AGENTS.md`.
For compatibility with older OMX/Codex setups, it also refreshes `~/.codex/skills/atls/SKILL.md`.
The injected global rule keeps AGENTS short and lets the full workflow live in the skill:

```markdown
## Atlassian via atls
- Use `atls` CLI for Jira/Confluence work before Atlassian MCP tools.
- Use `$atls` when you need the full format-selection and write-safety workflow.
- Always run `--dry-run` before write operations.
```

### Agent Usage Tips

```bash
# 1. Token-efficient: compact format is the default (no extra flags needed)
atls jira issue search "project=PROJ AND status=Open"

# 2. Use md format only when you need to read the body
atls jira issue get PROJ-1 -f md

# 3. Use json format for automation/parsing
atls jira issue get PROJ-1 -f json | jq '{key, summary, status}'

# 4. Confluence page editing workflow
atls confluence page pull-md PAGE_ID -o page.md --resolve-assets=sidecar --asset-dir=assets/
# ... edit locally ...
atls confluence page push-md PAGE_ID --md-file page.md --if-version 15 --dry-run
atls confluence page push-md PAGE_ID --md-file page.md --if-version 15

# 5. Branch on exit codes
# 0=OK, 2=not found, 5=stale version, 6=auth failure, 11=rate limited
```

## Format Placement

`--format` can be set either globally before the subcommand or locally on Jira/Confluence commands:

```bash
# Global placement
atls --format=json jira issue get PROJ-1

# Local placement
atls jira issue get PROJ-1 --format=json
atls confluence page search "space=DOCS" --format=json
```

Prefer the long local form `--format=...` after the subcommand for readability.

Some commands already use `-f` for file input, so after the subcommand you should use the long form instead of `-f`:

```bash
# `-f` is global here, before the subcommand
atls -f json jira issue get PROJ-1

# After the subcommand, use the long flag on commands that reserve -f for files
atls confluence page push-md 12345 --md-file page.md --format=json
atls confluence page create --body-file=- --format=json
```

## Quick Start

```bash
# Jira
atls jira issue get PROJ-1
atls jira issue search "project=PROJ AND status=Open" --limit=20
atls jira issue create --project PROJ --type Story --summary "New feature" --body-file=story.md --body-format=md

# Confluence
atls confluence page get 12345
atls confluence page search "space=DOCS AND title=API"
atls confluence page push-md 12345 --md-file=page.md --if-version 15
atls confluence page pull-md 12345 --output=page.md --resolve-assets=sidecar --asset-dir=assets/
atls confluence page diff-local 12345 local.md --passthrough-prefix workflow:

# Jira description from markdown
atls jira issue update PROJ-1 --body-file=desc.md --body-format=md --heading-promotion=jira
```

## Output Formats

| Format | Flag | Use case |
|---|---|---|
| compact | default | LLM scanning, minimal tokens |
| json | `--format=json` | Automation, structured parsing |
| md | `--format=md` | Body/description reading |
| raw | `--format=raw` | Byte-preserving body access |

## Passthrough Prefix Support

`--passthrough-prefix` is only supported on Confluence markdown round-trip commands:

| Command | Supports `--passthrough-prefix` |
|---|---|
| `confluence page push-md` | yes |
| `confluence page pull-md` | yes |
| `confluence page diff-local` | yes |
| `confluence page get` | no |
| `confluence page create` | no |
| `confluence page update` | no |

## Command Reference

### Jira (45 commands: 22 read + 23 write)
- `jira issue get|search|create|update|delete|transition|transitions|dates|sla|images`
- `jira comment add|edit`
- `jira field search|options`
- `jira project list|issues|versions|components|versions-create`
- `jira board list|issues`
- `jira sprint list|issues|create|update|add-issues`
- `jira link list-types|create|remote-create|delete`
- `jira epic link`
- `jira watcher list|add|remove`
- `jira worklog list|add`
- `jira attachment download|upload|delete`
- `jira dev-info get|get-many`
- `jira service-desk list|queues|queue-issues`
- `jira user get`

### Confluence (23 commands: 13 read + 10 write)
- `confluence page get|search|children|history|diff|images|create|update|delete|move|push-md|pull-md|diff-local`
- `confluence space tree`
- `confluence comment list|add|reply`
- `confluence label list|add`
- `confluence attachment list|download|download-all|upload|upload-batch|delete`
- `confluence user search`

### Utility
- `auth login|status|list`
- `config get|set|path`
- `setup codex|claude|all|status`

## Write Safety

All write commands support:
- `--dry-run`: Preview without executing
- `--body-file=-`: Pipe body content via stdin
- `--if-version N`: Optimistic concurrency (Confluence page update & push-md)
- `--if-updated ISO`: Stale check (Jira)
- `--attachment-if-exists skip|replace`: Duplicate attachment handling (push-md)
- `--asset-dir DIR`: Batch upload all files in a directory (push-md)

Additional convenience behavior:
- `confluence page push-md --md-file=-`: read markdown from stdin
- `confluence page push-md --asset-dir DIR`: if `DIR` does not exist, it is treated as an empty attachment set

## Jira Custom Fields

For scripting, explicitly requested `customfield_*` keys are preserved in JSON output:

```bash
atls jira issue get PROJ-1 --fields=summary,customfield_10100 --format=json
atls jira issue search "project=PROJ" --fields=summary,customfield_10100 --format=json
```

For writes, `--set-customfield` now verifies the result with a read-back check and exits with a validation error if Jira accepts the request but does not apply the value:

```bash
atls jira issue update PROJ-1 --set-customfield customfield_10100=EPIC-1
```

If the field expects a structured payload instead of a plain string/key, use `--fields-json` instead of `--set-customfield`.

## Architecture

- **CLI-first**: All functionality accessible via `atls` CLI. Skills are thin wrappers.
- **Single HTTP client**: `httpx`-based `BaseClient` with retry (429/5xx), pagination, auth.
- **cfxmark integration**: Jira wiki <-> Markdown <-> Confluence storage via single dependency.
- **Pydantic v2 models**: Strict response parsing for stable fields, with Jira `customfield_*` passthrough in JSON issue output.

## Key Dependencies

| Package | Purpose |
|---|---|
| httpx | REST client (sync) |
| typer + rich | CLI framework |
| pydantic | Response models |
| cfxmark >= 0.4 | Markup conversion (Jira wiki + Confluence XHTML) |
| platformdirs | Config path resolution |

## Differences from mcp-atlassian

1. **CLI, not MCP**: Invoked via shell, not MCP protocol. Zero schema overhead per call.
2. **Token-efficient by default**: `compact` format strips non-essential fields. Body fetched only when requested.
3. **Byte-preserving raw mode**: `--format=raw` returns server response verbatim. No silent character dropping.
4. **Server/DC primary**: Optimized for on-premise Atlassian. Cloud is non-goal.
5. **Unified client**: Single `httpx` client for all products vs. separate adapters.
6. **cfxmark for all markup**: Both Jira wiki and Confluence XHTML via one dependency.

## Development

```bash
# Setup
uv sync

# Test
uv run pytest

# Lint
uv run ruff check src/ tests/
uv run mypy src/

# Build
uv build
```

## Roadmap

- **0.1.0** (current): Jira + Confluence read/write, push-md/pull-md/diff-local, benchmarks, skills
- **0.2.0**: Bitbucket Server + Bamboo
- **0.3.0+**: Workflow skills, async client, caching

## License

MIT
