# atlassian-skills

[![PyPI version](https://img.shields.io/pypi/v/atlassian-skills?color=blue)](https://pypi.org/project/atlassian-skills/)
[![Python versions](https://img.shields.io/pypi/pyversions/atlassian-skills)](https://pypi.org/project/atlassian-skills/)
[![PyPI downloads](https://img.shields.io/pypi/dm/atlassian-skills)](https://pypi.org/project/atlassian-skills/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/eunsanMountain/atlassian-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/eunsanMountain/atlassian-skills/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/eunsanMountain/atlassian-skills?style=flat&color=yellow)](https://github.com/eunsanMountain/atlassian-skills/stargazers)

A token-efficient CLI that brings [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) functionality to the command line — optimized for LLM agent workflows on Atlassian Server/DC.

mcp-atlassian is great for Cloud setups, but on Server/DC its MCP protocol overhead and verbose JSON responses consume tokens fast. It also lacks lossless Confluence markup round-tripping — edits via MCP can silently alter page content.

**atlassian-skills** re-implements the same Jira and Confluence operations as a lightweight CLI with compact output, achieving **≥50% token reduction**. It uses [cfxmark](https://github.com/eunsanMountain/cfxmark) for **lossless Confluence XHTML ↔ Markdown conversion**, enabling agents to pull a page as Markdown, edit it, and push it back without any content loss.

First-class integration with **Claude Code**, **Codex**, and **GitHub Copilot**. A single `atls setup` wizard configures URLs, tokens, and the auto-loaded Skill for all three agents in one pass.

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
| AI agent setup | Manual MCP config | One interactive wizard (`atls setup`) for Claude Code + Codex + GitHub Copilot |
| Bitbucket Server | Not supported | Full (0.2.0) — PR workflow, comments, tasks, build status |
| Bamboo | Not supported | Planned (0.3.0) |

## Quick install

```bash
uv tool install atlassian-skills    # or: pipx install atlassian-skills / pip install atlassian-skills
atls setup                          # interactive wizard — URLs, tokens, Claude/Codex/Copilot skill
atls doctor                         # verify configuration + auth
```

That's it. The wizard detects your platform/shell and writes config + secrets + shell rc + agent skills in one pass.

> ⚠️ Run `atls setup` **directly in your terminal** — never through an AI agent's shell tool. The wizard refuses non-TTY stdin and prompts hide token input from terminal echo; running it through an agent would force the agent to fulfil the token prompt from chat, leaking the value into LLM context.

<details>
<summary><b>Don't have a package manager yet? (Linux / macOS / Windows)</b></summary>

Pick **one**: `uv` (recommended) or `pipx`. If you'll use plain `pip`, skip this entirely.

**uv (recommended)**
```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
Alternatives: `brew install uv` (macOS), `winget install astral-sh.uv` (Windows), `pipx install uv` (cross-platform). Full options in the [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/).

**pipx**
```bash
# Linux / macOS
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Windows (PowerShell)
python -m pip install --user pipx
python -m pipx ensurepath
```
After `ensurepath`, open a new terminal so the updated `PATH` is picked up.

</details>

<details>
<summary><b>What the wizard does, step by step</b></summary>

1. **TTY guard** — refuses to run if stdin isn't a real terminal (protects tokens from being fed in through AI-agent shell tools).
2. **fish shell guard** — fish uses `set -gx` instead of `export`; the wizard exits cleanly with a workaround and points you at `atls setup --skills-only` for the skill-only path.
3. **Steps [1/4] – [3/4] — one block per product (Jira, Confluence, Bitbucket).** Each step prints the current URL + PAT state (with source `(config)` or `(env: ATLS_DEFAULT_JIRA_URL)`), then asks:
   - When something is already configured: `[k]eep / [e]dit / [r]emove / [s]kip` (default `k`)
   - When nothing is configured yet: `[a]dd / [s]kip` (default `s`)

   Choose `a` or `e` and the wizard prompts for the URL, then prints the PAT issuer link inline (e.g. `Generate a PAT at: <jira-host>/plugins/personalaccesstokens/usertokens.action`) before the hidden PAT prompt. So you never need the README open while running it.

   - **URL** input is saved to `~/.config/atlassian-skills/config.toml`.
   - **PAT** input:
     - **Linux / macOS**: written to `~/.secrets/{product}_pat` (mode `0600`) + an idempotent `# >>> atls env >>>` block in `~/.zshrc` or `~/.bashrc` (rebuilt from every existing `~/.secrets/*_pat`, so a fresh Bitbucket token never wipes a previously-saved Jira one). Current-process `os.environ` is updated immediately so the verify step sees it.
     - **Windows (cmd / PowerShell / Git Bash)**: written to `HKCU\Environment` via `winreg` + `WM_SETTINGCHANGE` broadcast + current `os.environ`.
   - `r` (remove) on a `config`-sourced URL clears it from `config.toml` and leaves the token file in place with a manual-removal hint. On an `env`-sourced URL it warns that the wizard cannot permanently unset shell env vars.
4. **Orphan file banner** (Linux / macOS) — env vars are the single source of truth. If the wizard finds a `~/.secrets/{p}_pat` file whose corresponding env var isn't loaded in the current shell, it prints a banner up front explaining the three ways to resolve it: `source ~/.zshrc` to activate, re-enter a PAT to overwrite, or pick `r` to delete the stale file. Picking `r` always deletes the file too, so the orphan state is easy to clean up.
5. **Shadowing warning** (Linux / macOS) — if the wizard finds a manual `export JIRA_PERSONAL_TOKEN=…` outside the atls block in your shell rc, it warns at the end: depending on line order the manual export can override the wizard-managed token. (Multi-profile `ATLS_DEFAULT_*_TOKEN` shadowing is intentionally not checked — that's an advanced setup; see the priority table under *Manual setup*.)
6. **[4/4] AI agent skills** — `[Y/n]` prompt for each:
   - Claude Code (default `Y`): `~/.claude/skills/atls/SKILL.md` + routing block in `~/.claude/CLAUDE.md`
   - Codex (default `Y`): `~/.codex/skills/atls/SKILL.md` + routing block in `~/.codex/AGENTS.md`
   - GitHub Copilot (default `Y`): `~/.copilot/skills/atls/SKILL.md` + routing block in [`~/.copilot/copilot-instructions.md`](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions). Cross-platform via `Path.home()` — works identically on Linux, macOS, and Windows (`%USERPROFILE%\.copilot\...`). WSL note: `~/.copilot` here lives in the WSL filesystem and is invisible to a native Windows Copilot CLI install; the wizard prints a one-line warning when this is detected.
7. **Verify** — runs `auth status` inline so you see whether URL + token resolution is working before you exit.

Re-run `atls setup` any time. Every step's default is non-destructive (`k` for existing, `s` for not-yet-configured, `Y` for agent install), so a pure-Enter run preserves whatever you already had.

</details>

<details>
<summary><b>Manual setup (env vars / config.toml / multi-profile)</b></summary>

If you'd rather skip the wizard and set everything by hand:

**1. Create access tokens**
- **Jira**: Profile → Personal Access Tokens → Create
- **Confluence**: Profile → Personal Access Tokens → Create
- **Bitbucket**: Profile → Manage Account → HTTP access tokens → Create (permissions: project read, repository read/write)

**2. Configure server URLs**
```bash
atls config set profiles.default.jira_url https://your-jira.example.com
atls config set profiles.default.confluence_url https://your-confluence.example.com
atls config set profiles.default.bitbucket_url https://your-bitbucket.example.com
```
Or via environment variables:
```bash
export ATLS_DEFAULT_JIRA_URL="https://your-jira.example.com"
export ATLS_DEFAULT_CONFLUENCE_URL="https://your-confluence.example.com"
export ATLS_DEFAULT_BITBUCKET_URL="https://your-bitbucket.example.com"
```
For non-default profiles, replace `DEFAULT` with the profile name (e.g. `ATLS_CORP_JIRA_URL`).

**3. Set tokens (Linux / macOS — `~/.zshrc` / `~/.bashrc`)**
```bash
# Standard names (compatible with existing MCP servers)
export JIRA_PERSONAL_TOKEN="your-jira-pat"
export CONFLUENCE_PERSONAL_TOKEN="your-confluence-pat"
export BITBUCKET_TOKEN="your-bitbucket-http-access-token"

# Multi-profile
export ATLS_CORP_JIRA_TOKEN="..."
export ATLS_CORP_CONFLUENCE_TOKEN="..."
export ATLS_CORP_BITBUCKET_TOKEN="..."
```

**Secure file-based storage (the wizard's default)**
```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
printf '%s' 'YOUR_JIRA_PAT'       > ~/.secrets/jira_pat       && chmod 600 ~/.secrets/jira_pat
printf '%s' 'YOUR_CONFLUENCE_PAT' > ~/.secrets/confluence_pat && chmod 600 ~/.secrets/confluence_pat
printf '%s' 'YOUR_BITBUCKET_PAT'  > ~/.secrets/bitbucket_pat  && chmod 600 ~/.secrets/bitbucket_pat

# Then in ~/.zshrc or ~/.bashrc:
# >>> atls env >>>
[ -f ~/.secrets/jira_pat ]       && export JIRA_PERSONAL_TOKEN="$(cat ~/.secrets/jira_pat)"
[ -f ~/.secrets/confluence_pat ] && export CONFLUENCE_PERSONAL_TOKEN="$(cat ~/.secrets/confluence_pat)"
[ -f ~/.secrets/bitbucket_pat ]  && export BITBUCKET_TOKEN="$(cat ~/.secrets/bitbucket_pat)"
# <<< atls env <<<
```

**Set tokens (Windows)**
`atls` runs natively on Windows; pick whichever method you prefer — all produce the same result.

- **System Properties GUI**: `Win + R` → `sysdm.cpl` → Advanced → Environment Variables → New (under User variables): `JIRA_PERSONAL_TOKEN`, `CONFLUENCE_PERSONAL_TOKEN`, `BITBUCKET_TOKEN`, plus `ATLS_DEFAULT_*_URL`. Open a *new* terminal afterwards.
- **PowerShell** (permanent, picked up by new sessions):
  ```powershell
  [Environment]::SetEnvironmentVariable("JIRA_PERSONAL_TOKEN", "your-jira-pat", "User")
  [Environment]::SetEnvironmentVariable("ATLS_DEFAULT_JIRA_URL", "https://your-jira.example.com", "User")
  ```
- **cmd / `setx`** (permanent):
  ```cmd
  setx JIRA_PERSONAL_TOKEN "your-jira-pat"
  setx ATLS_DEFAULT_JIRA_URL "https://your-jira.example.com"
  ```

> `atls config set ...` works identically on Windows — config is stored at `%APPDATA%\atlassian-skills\config.toml` via `platformdirs`.

**Basic auth (legacy instances without PAT support)**

Older Jira (< 8.14) and Confluence (< 7.9) predate Personal Access Tokens. For those:
```bash
export ATLS_DEFAULT_JIRA_AUTH=basic
export ATLS_DEFAULT_JIRA_USER=myname
export ATLS_DEFAULT_JIRA_TOKEN=<password-or-api-token>
```
The same `*_AUTH=basic` / `*_USER` / `*_TOKEN` triple works for `jira`, `confluence`, and `bitbucket`.

**4. Verify**
```bash
atls auth status        # equivalent to the Auth section of `atls doctor`
```

**Priority**
- URLs — CLI flags > `ATLS_*` env > config.toml
- Tokens — CLI flags > `ATLS_*` env > `JIRA_PERSONAL_TOKEN` / `CONFLUENCE_PERSONAL_TOKEN` / `BITBUCKET_TOKEN`

</details>

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

# Jira description from markdown
atls jira issue update PROJ-1 --body-file=desc.md --body-format=md --heading-promotion=jira

# Jira comment / worklog from markdown
atls jira comment add PROJ-1 --body-file=comment.md --body-format=md
atls jira comment edit PROJ-1 12345 --body-file=comment.md --body-format=md
atls jira worklog add PROJ-1 --time-spent-seconds 1800 --comment "$(cat note.md)" --comment-format=md
```

### Talking to your AI agent in natural language

Once `atls setup` has installed the Skill, your AI agent translates plain language into the right CLI call automatically:

> "Read PROJ-123 and summarize the acceptance criteria."
>
> "Search for open bugs in the PLATFORM project assigned to me."
>
> "Pull the API Overview page from Confluence, add a rate-limiting section, and push it back."
>
> "Create a Story in PROJ: title 'Add retry logic to payment service', and paste the description from desc.md."

The agent picks the right output format and handles pagination + error codes for you.

### Agent usage tips

```bash
# 1. Token-efficient: compact format is the default
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

### Bitbucket (33 commands: 11 read + 22 write)
- `bitbucket project list`
- `bitbucket repo list|get`
- `bitbucket pr list|get|diff|comments|commits|activity|create|update|merge|decline|approve|unapprove|needs-work|reopen|diffstat|statuses|pending-review`
- `bitbucket branch list`
- `bitbucket file get`
- `bitbucket comment add|reply|update|delete|resolve|reopen`
- `bitbucket task list|get|create|update|delete`

> All write commands support `--dry-run`. PR diff and file get treat `--format=md` as raw text passthrough.

### Utility
- `setup` — interactive wizard (URLs, tokens, Claude/Codex skill, auto-verify)
- `setup --skills-only` — silent skill refresh, used by `atls upgrade`
- `doctor` — diagnose installation: platform, paths, skill version markers, auth resolution
- `auth login|status|list`
- `config get|set|path`
- `upgrade` — auto-detects uv / pipx / pip and refreshes skill assets
- `version [--check]` — show installed version; `--check` exits 1 if outdated vs PyPI
- `setup codex|claude|all|paths|status` **(deprecated — removed in 0.3.0)** — replaced by `setup` (wizard) and `doctor`

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

atlassian-skills is a CLI re-implementation of mcp-atlassian's Jira and Confluence operations. If you are currently using mcp-atlassian:

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

- **0.1.x** — Jira + Confluence read/write, push-md/pull-md/diff-local, benchmarks, GitHub Actions CI/release
- **0.2.x** — Bitbucket Server/DC PR workflow + Skill-first Claude/Codex integration
- **0.2.7 (current)** — `atls setup` interactive wizard + `atls doctor`; `setup all/codex/claude/paths/status` deprecated
- **0.3.0** — Bamboo + workflow skills; remove deprecated `setup` subcommands
- **0.4.0+** — Async client, caching, non-interactive `atls setup`, fish shell support, keyring-backed tokens

## License

[MIT](./LICENSE)
