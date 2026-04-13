# atlassian-skills

A token-efficient CLI that brings [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) functionality to the command line — optimized for LLM agent workflows on Atlassian Server/DC.

mcp-atlassian is great for Cloud setups, but on Server/DC its MCP protocol overhead and verbose JSON responses consume tokens fast. It also lacks lossless Confluence markup round-tripping — edits via MCP can silently alter page content.

**atlassian-skills** re-implements the same Jira and Confluence operations as a lightweight CLI with compact output, achieving **≥50% token reduction**. It uses [cfxmark](https://github.com/eunsanMountain/cfxmark) for **lossless Confluence XHTML ↔ Markdown conversion**, enabling agents to pull a page as Markdown, edit it, and push it back without any content loss.

First-class integration with **Claude Code** and **Codex**. `atls setup all` registers atls as the default Atlassian tool for both — a comprehensive usage guide (command tree, format decision rules, write safety) is loaded via Claude's `/atls` slash command or Codex's auto-loaded skill, while a short preference directive in `CLAUDE.md` / `AGENTS.md` keeps the routing rule active in every conversation. Your agent translates "read PROJ-123" or "update the API page" into the right CLI call without further configuration.

## Why atlassian-skills?

| | mcp-atlassian (MCP) | atlassian-skills (CLI) |
|---|---|---|
| Interface | MCP protocol (JSON-RPC) | Shell CLI (`atls`) |
| Schema overhead per session | ~15,000 tokens | <400 tokens |
| Response payload size | Full JSON | 7–34% of MCP |
| Full workflow (end-to-end) | Baseline | 91% reduction |
| Confluence markup round-trip | Lossy (XHTML re-serialization) | Lossless via cfxmark (XHTML ↔ Markdown) |
| Jira body preservation | Drops special chars | Byte-preserving |
| Server/DC support | Partial | Full (primary target) |
| AI agent setup | Manual MCP config | One-line `atls setup all` for Claude Code + Codex |
| Bitbucket/Bamboo | Not supported | Planned (0.2.0) |

## Installation

```bash
# Install as a CLI tool (after PyPI publish)
uv tool install atlassian-skills

# Or via pip
pip install atlassian-skills

# Verify
atls --version

# Upgrade the CLI and refresh assistant setup assets
atls upgrade
```

## Authentication

Server/DC only. Personal Access Token (PAT / Bearer) is the default auth method.

### 1. Create Personal Access Tokens

Generate PATs from your Atlassian instance:
- Jira: `https://your-jira.example.com` → Profile → Personal Access Tokens → Create
- Confluence: `https://your-confluence.example.com` → Profile → Personal Access Tokens → Create

### 2. Configure server URLs

```bash
atls config set profiles.default.jira_url https://your-jira.example.com
atls config set profiles.default.confluence_url https://your-confluence.example.com
```

Or via environment variables:

```bash
export ATLS_DEFAULT_JIRA_URL="https://your-jira.example.com"
export ATLS_DEFAULT_CONFLUENCE_URL="https://your-confluence.example.com"
```

For non-default profiles, use `ATLS_<PROFILE>_JIRA_URL` and `ATLS_<PROFILE>_CONFLUENCE_URL`.

### 3. Set tokens

**Environment variables (recommended)**

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

<details>
<summary>Secure file-based storage (alternative to plain env vars)</summary>

```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
printf '%s' 'YOUR_JIRA_PAT'       > ~/.secrets/jira_pat
printf '%s' 'YOUR_CONFLUENCE_PAT' > ~/.secrets/confluence_pat
chmod 600 ~/.secrets/jira_pat ~/.secrets/confluence_pat

# Then in ~/.zshrc or ~/.bashrc:
[ -f ~/.secrets/jira_pat ]       && export JIRA_PERSONAL_TOKEN="$(cat ~/.secrets/jira_pat)"
[ -f ~/.secrets/confluence_pat ] && export CONFLUENCE_PERSONAL_TOKEN="$(cat ~/.secrets/confluence_pat)"
```
</details>

**Priority**: CLI flags > `ATLS_*` vars > `JIRA_PERSONAL_TOKEN`/`CONFLUENCE_PERSONAL_TOKEN` > config file

### 4. Verify

```bash
atls auth status
```

## Quick Start

### 1. Set up your AI agent

```bash
atls setup all        # installs atls skill for Claude Code + Codex
atls auth status      # verify connection
```

**What gets installed:**
- **Claude Code**: `~/.claude/commands/atls.md` (full usage guide, load with `/atls`) + preference directive in `~/.claude/CLAUDE.md` (active every conversation, tells Claude to route Atlassian work through atls and navigate with `--help`).
- **Codex**: `~/.agents/skills/atls/SKILL.md` (auto-loaded skill with command tree, format rules, write-safety protocol) + routing directive in `~/.codex/AGENTS.md`.
- Run `atls setup status` to check what is installed.
- Run `atls setup paths` to see every resolved install path for your platform.

**Install paths (Windows / macOS / Linux):**

`atls` resolves install paths in this order — interactive override → environment variable → platform default. No extra configuration is required on any OS; `Path.home()` expands to `%USERPROFILE%` on Windows and `$HOME` on macOS/Linux.

| Target | Env var override | Default (Windows) | Default (macOS / Linux) |
|---|---|---|---|
| Claude config dir | `CLAUDE_CONFIG_DIR` | `C:\Users\<you>\.claude` | `~/.claude` |
| Codex config dir | `CODEX_HOME` | `C:\Users\<you>\.codex` | `~/.codex` |
| Agents skill dir | `AGENTS_HOME` | `C:\Users\<you>\.agents` | `~/.agents` |

To customize at install time, run any setup command with `--interactive` (or `-i`):

```bash
atls setup all --interactive
# Detected platform: windows
# Claude config dir: C:\Users\you\.claude  (source: default)
#   Press Enter to accept, or paste a custom path: D:\Tools\Claude
#   → using: D:\Tools\Claude
# Codex config dir: C:\Users\you\.codex  (source: default)
#   ...
```

Alternatively, export environment variables before running setup:

```bash
# Windows (PowerShell)
$env:CLAUDE_CONFIG_DIR = "D:\Tools\Claude"
$env:CODEX_HOME = "D:\Tools\Codex"
atls setup all

# macOS / Linux
export CLAUDE_CONFIG_DIR=~/work/claude
atls setup all
```

**Codex users note:** the Codex skill installs to `<AGENTS_HOME>/skills/atls/` (primary) and `<CODEX_HOME>/skills/atls/` (legacy compatibility). The routing directive in `AGENTS.md` keeps `atls` as the default Atlassian tool across conversations. Codex's session-start mechanism auto-loads `SKILL.md` — no manual load required.

### 2. Talk to your agent in natural language

Once set up, your AI agent knows how to use atls automatically. Just ask:

> "Read PROJ-123 and summarize the acceptance criteria."
>
> "Search for open bugs in the PLATFORM project assigned to me."
>
> "Pull the API Overview page from Confluence, add a rate-limiting section, and push it back."
>
> "Create a Story in PROJ: title 'Add retry logic to payment service', and paste the description from desc.md."
>
> "What changed on the Release Notes page since last week?"

The agent translates these into `atls` CLI calls, picks the right output format, and handles pagination and error codes for you.

### 3. Or use the CLI directly

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

# Jira description from markdown
atls jira issue update PROJ-1 --body-file=desc.md --body-format=md --heading-promotion=jira
```

### Agent usage tips

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

## Output Formats

| Format | Flag | Use case |
|---|---|---|
| compact | default | LLM scanning, minimal tokens |
| json | `--format=json` | Automation, structured parsing |
| md | `--format=md` | Body/description reading |
| raw | `--format=raw` | Byte-preserving body access |

`--format` can be placed globally or locally on subcommands:

```bash
# Global placement
atls --format=json jira issue get PROJ-1

# Local placement (preferred for readability)
atls jira issue get PROJ-1 --format=json
```

> Some commands use `-f` for file input (e.g. `push-md`). After the subcommand, always use the long form `--format=` to avoid ambiguity.

## Command Reference

### Jira (45 commands: 22 read + 23 write)
- `jira issue get|search|create|update|delete|transition|transitions|dates|sla|images`
- `jira comment add|edit`
- `jira field search|options`
- `jira project list|issues|versions|components|versions-create`
- `jira board list|issues`
- `jira sprint list|issues|create|update|add-issues`
- `jira link list-types|create|remote-list|remote-create|delete`
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

> `--passthrough-prefix` is supported on Confluence markdown round-trip commands only: `push-md`, `pull-md`, `diff-local`.

### Utility
- `auth login|status|list`
- `config get|set|path`
- `setup codex|claude|all|status|paths` (add `--interactive` to customize install paths per-platform)
- `upgrade`

## Write Safety

All write commands support:
- `--dry-run`: Preview without executing
- `--body-file=-`: Pipe body content via stdin
- `--if-version N`: Optimistic concurrency (Confluence page update & push-md)
- `--if-updated ISO`: Stale check (Jira)
- `--attachment-if-exists skip|replace`: Duplicate attachment handling (push-md)
- `--asset-dir DIR`: Batch upload all files in a directory (push-md)

## Jira Custom Fields

For scripting, explicitly requested `customfield_*` keys are preserved in JSON output:

```bash
atls jira issue get PROJ-1 --fields=summary,customfield_10100 --format=json
atls jira issue search "project=PROJ" --fields=summary,customfield_10100 --format=json
```

For writes, `--set-customfield` verifies the result with a read-back check and exits with a validation error if Jira accepts the request but does not apply the value:

```bash
atls jira issue update PROJ-1 --set-customfield customfield_10100=EPIC-1
```

If the field expects a structured payload instead of a plain string/key, use `--fields-json` instead of `--set-customfield`.

## Migrating from mcp-atlassian

atlassian-skills is a CLI re-implementation of mcp-atlassian's Jira and Confluence operations. If you are currently using mcp-atlassian, here is what changes:

| mcp-atlassian | atlassian-skills |
|---|---|
| MCP protocol (JSON-RPC over stdio) | Shell CLI (`atls <command>`) |
| Full JSON responses every call | `compact` by default, `json`/`md`/`raw` on demand |
| ~15k token schema overhead per session | <400 tokens (CLI help only when needed) |
| `JIRA_PERSONAL_TOKEN` env var | Same env var works, plus `ATLS_*` for multi-profile |
| Cloud + Server/DC | Server/DC only (primary target) |
| Separate Jira wiki / Confluence XHTML handling | Unified via `cfxmark` — single dependency for all markup |
| Confluence edits can silently alter content | Lossless XHTML ↔ Markdown round-trip via cfxmark |
| Silent character dropping in Jira descriptions | Byte-preserving `--format=raw` mode |

**Token-compatible auth**: If you already have `JIRA_PERSONAL_TOKEN` and `CONFLUENCE_PERSONAL_TOKEN` set for mcp-atlassian, atls picks them up automatically — no reconfiguration needed.

## Architecture

- **CLI-first**: All functionality accessible via the `atls` binary. AI agent skills are thin wrappers that invoke CLI commands.
- **Single HTTP client**: `httpx`-based `BaseClient` with retry (429/5xx), pagination, and auth.
- **cfxmark integration**: Lossless Confluence XHTML ↔ Markdown ↔ Jira wiki conversion via a single dependency. Pages survive unlimited round-trips (`pull-md` → edit → `push-md`) with zero content drift.
- **Pydantic v2 models**: Strict response parsing for stable fields, with Jira `customfield_*` passthrough in JSON output.

## Key Dependencies

| Package | Purpose |
|---|---|
| httpx | REST client (sync) |
| typer + rich | CLI framework |
| pydantic | Response models |
| cfxmark ≥ 0.4 | Markup conversion (Jira wiki + Confluence XHTML) |
| platformdirs | Config path resolution |

## Development

```bash
# Setup
uv sync

# Local install (editable)
uv tool install -e .              # from repo root
uv tool install --force -e .      # reinstall after entrypoint changes

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

[MIT](./LICENSE)
