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
atls setup                          # interactive wizard — URLs, tokens (OS keyring), Claude/Codex/Copilot skill
atls doctor                         # verify configuration + auth
```

That's it. The wizard stores tokens in your OS keyring and installs the agent skills in one pass — no shell restart needed. Prefer environment variables or a secret-manager command instead? See **Manual setup** below; the wizard is keyring-only.

> ⚠️ Run `atls setup` **directly in your terminal** — never through an AI agent's shell tool. The wizard refuses non-TTY stdin and prompts hide token input from terminal echo; running it through an agent would force the agent to fulfil the token prompt from chat, leaking the value into LLM context.

<details>
<summary><b>Don't have a package manager yet? (Linux / macOS / Windows)</b></summary>

If you'll use plain `pip`, skip this entirely.

**uv (recommended)**
```bash
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
Alternatives: `brew install uv` (macOS), `winget install astral-sh.uv` (Windows), `pipx install uv` (cross-platform). Full options in the [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/).

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

**File-based storage (manual — for the security-conscious without a keyring)**

The wizard no longer manages `~/.secrets` — it stores tokens in the OS keyring only. If you'd rather keep each token in a `0600`-mode file and source it yourself — independent of the wizard — set it up by hand:
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
- Tokens — CLI flags > `ATLS_*` env > `JIRA_PERSONAL_TOKEN` / `CONFLUENCE_PERSONAL_TOKEN` / `BITBUCKET_TOKEN` > the profile's `storage` provider (keyring / command)

> Prefer not to keep tokens in env vars? See **System keyring and shell-command providers** below
> to store them in the OS keyring or fetch them from 1Password / `pass` / Bitwarden on demand.

</details>

<details>
<summary><b>System keyring and shell-command providers (no persistent env vars)</b></summary>

atls resolves a token from the first source that has one: **CLI flag → env var → the profile's `storage` provider**. The `atls setup` wizard only ever writes to the **keyring**; the other two providers are configured by hand (this section + *Manual setup*).

| `storage` | set up by | platform | when to use |
|---|---|---|---|
| `keyring` | the wizard (or by hand) | macOS, Linux desktop, Windows | personal machine, dotfile-synced configs |
| env vars | you (export / `atls config` / Manual setup) | all | simple; the only option that works headless / CI / Docker |
| `command` | you (edit `config.toml`) | all (bring your own tool) | already using 1Password / `pass` / `bw` / PowerShell |

`storage` selects a **single** provider — it is not a fallback chain. An env var always wins at resolution time, so a quick `export` overrides the keyring without touching config (and the wizard will skip a product whose token is already in the environment).

**System keyring** — uses the platform's native credential store (macOS Keychain, Windows Credential Manager, Linux Secret Service). The `keyring` package ships with atls by default (no extra needed). Save tokens once, then point the profile at the keyring:

```bash
# Save tokens to the system keyring (run once per token — works on all platforms)
python -c "import keyring; keyring.set_password('atls-default', 'jira_token', 'your-jira-pat')"
python -c "import keyring; keyring.set_password('atls-default', 'confluence_token', 'your-confluence-pat')"
```

The keyring service name is `atls-<profile>` and the account is `<product>_token` (e.g. service `atls-default`, account `jira_token`).

```toml
# ~/.config/atlassian-skills/config.toml
[profiles.default]
jira_url = "https://your-jira.example.com"
storage = "keyring"
```

**Shell command** — run any command that prints the token to stdout. The feature is cross-platform; the command itself is whatever your OS and secret manager support:

```toml
# ~/.config/atlassian-skills/config.toml
[profiles.default]
jira_url = "https://your-jira.example.com"
storage = "command"

# One command for all products (1Password CLI)
credential_command = "op read op://vault/atlassian/token"

# …or a different command per product (takes priority over credential_command)
jira_command       = "op read op://vault/jira/token"
confluence_command = "op read op://vault/confluence/token"
bitbucket_command  = "op read op://vault/bitbucket/token"

# macOS Keychain    : security find-generic-password -s my-jira-token -w
# Linux (pass)      : pass show jira/pat
# Bitwarden CLI     : bw get password jira
# Windows PowerShell: powershell -NoProfile -Command "(Get-StoredCredential -Target jira-pat).GetNetworkCredential().Password"
```

The command runs with a 5-second timeout; exit code must be 0 and stdout is used as the token.

Inspect what each product resolves to (probes keyring / runs the command — may prompt for Touch ID or a passphrase):

```bash
atls auth status            # configured-only (does not run keyring/command)
atls auth status --resolve  # actually probes each provider
```

</details>

<details>
<summary><b>What the wizard does, step by step</b></summary>

The wizard is **keyring-only**: it stores tokens in your OS keyring and nothing else. Environment variables and shell-command secret managers still work at call time (the resolver checks `env > keyring > command`), but you configure those by hand — see *Manual setup*. The wizard never edits your shell rc or env.

1. **TTY guard** — refuses to run if stdin isn't a real terminal (protects tokens from being fed in through AI-agent shell tools).
2. **Env-token detection** — if atls already finds a token in your environment (`ATLS_DEFAULT_<PRODUCT>_TOKEN` or `JIRA_PERSONAL_TOKEN` etc.), the wizard says so up front. Because env outranks the keyring, it **skips** those products (a keyring entry would just be shadowed) and leaves your env setup untouched. To move one to the keyring: unset its env var, remove it from your shell rc, open a **new** terminal, and re-run.
3. **Steps [1/4] – [3/4] — one block per product (Jira, Confluence, Bitbucket).** Each step prints the current URL + where the token lives (`environment variable (VAR)` or `keyring storage`), then asks `[s]kip / [e]dit / [r]emove` (default `s`, or `e` when there's nothing yet). `skip` leaves it as-is. `[e]dit` prompts for the URL (saved to `~/.config/atlassian-skills/config.toml`); then, unless the product's token is in the environment, prints the PAT issuer link and takes a hidden PAT prompt → `keyring.set_password("atls-<profile>", "<product>_token", …)`. `[r]emove` clears the URL and deletes the product's keyring entry (it never touches your env vars or shell rc).
4. **Keyring availability** — if the `keyring` package can't be imported the wizard aborts with a reinstall hint. After saving, if the session looks headless (Docker / WSL / no D-Bus / text-only SSH) it warns that the keyring may be locked and points you at env (Manual setup).
5. **[4/4] AI agent skills** — `[Y/n]` prompt for each:
   - Claude Code (default `Y`): `~/.claude/skills/atls/SKILL.md` + routing block in `~/.claude/CLAUDE.md`
   - Codex (default `Y`): `~/.codex/skills/atls/SKILL.md` + routing block in `~/.codex/AGENTS.md`
   - GitHub Copilot (default `Y`): `~/.copilot/skills/atls/SKILL.md` + routing block in [`~/.copilot/copilot-instructions.md`](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions). Cross-platform via `Path.home()` — works identically on Linux, macOS, and Windows (`%USERPROFILE%\.copilot\...`). WSL note: `~/.copilot` here lives in the WSL filesystem and is invisible to a native Windows Copilot CLI install; the wizard prints a one-line warning when this is detected.
6. **Verify** — probes each provider (`auth status --resolve`) so you see whether each product actually resolves (`source=env` / `keyring`) before you exit.

Re-run `atls setup` any time. Defaults are non-destructive (`s` skips a product, `Y` installs an agent skill), and storage flips to keyring only when you actually store a token — a pure Enter-through leaves an env-based setup exactly as it was.

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
- **0.2.7** — `atls setup` interactive wizard + `atls doctor`; `setup all/codex/claude/paths/status` deprecated
- **0.2.8 (current)** — OS keyring token storage (keyring-only wizard), shell-command provider for manual setups, `atls auth status --resolve`
- **0.3.0** — Bamboo + workflow skills; remove deprecated `setup` subcommands
- **0.4.0+** — Async client, caching, non-interactive `atls setup`, fish shell support, multi-profile wizard

## License

[MIT](./LICENSE)
