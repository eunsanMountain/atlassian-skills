# atlassian-skills

[![PyPI version](https://img.shields.io/pypi/v/atlassian-skills?color=blue)](https://pypi.org/project/atlassian-skills/)
[![Python versions](https://img.shields.io/pypi/pyversions/atlassian-skills)](https://pypi.org/project/atlassian-skills/)
[![PyPI downloads](https://img.shields.io/pypi/dm/atlassian-skills)](https://pypi.org/project/atlassian-skills/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/eunsanMountain/atlassian-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/eunsanMountain/atlassian-skills/actions/workflows/ci.yml)
[![GitHub stars](https://img.shields.io/github/stars/eunsanMountain/atlassian-skills?style=flat&color=yellow)](https://github.com/eunsanMountain/atlassian-skills/stargazers)

A token-efficient CLI that brings [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) functionality to the command line — optimized for LLM agent workflows on Atlassian Server/DC.

mcp-atlassian is great for Cloud setups, but on Server/DC its MCP protocol overhead and verbose JSON responses consume tokens fast. It also lacks lossless Confluence markup round-tripping — edits via MCP can silently alter page content.

**atlassian-skills** re-implements the same Jira and Confluence operations as a lightweight CLI with compact output, achieving **≥50% token reduction**. Its Confluence workflow uses portable managed Markdown plus fresh, source-bound cfxmark proofs; no machine-local database is publication authority.

First-class integration with **Claude Code**, **Codex**, and **GitHub Copilot**. A single `atls setup` wizard configures URLs, tokens, and the auto-loaded Skill for all three agents in one pass.

## Why atlassian-skills?

| | mcp-atlassian (MCP) | atlassian-skills (CLI) |
|---|---|---|
| Interface | MCP protocol (JSON-RPC) | Shell CLI (`atls`) |
| Schema overhead per session | ~15,000 tokens | <400 tokens |
| Response payload size | Full JSON | 7–34% of MCP |
| Full workflow (end-to-end) | Baseline | 91% reduction |
| Confluence markup round-trip | Lossy (XHTML re-serialization) | Source-bound proof + informed loss consent via cfxmark |
| Jira body preservation | Drops special chars | Byte-preserving |
| Server/DC support | Partial | Full (primary target) |
| AI agent setup | Manual MCP config | One interactive wizard (`atls setup`) for Claude Code + Codex + GitHub Copilot |
| Bitbucket Server | Not supported | Full (0.2.0) — PR workflow, comments, tasks, build status |
| Bamboo | Not supported | Not supported |

## Quick install

```bash
uv tool install atlassian-skills    # or: pipx install atlassian-skills / pip install atlassian-skills
atls setup                          # interactive wizard — URLs, tokens, local attachment writer, agent skills
atls doctor                         # verify configuration + auth
```

That's it. The wizard stores tokens in your OS keyring and installs the agent skills in one pass — no shell restart needed. On Windows it also offers an optional compatibility attachment writer; native writes remain the default. Prefer environment variables or a secret-manager command instead? See **Manual setup** below; the wizard is keyring-only for credentials.

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
attachment_writer = "native" # Windows also supports "compatible"; native is the default

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

The wizard is **keyring-only for credentials**: tokens go only to your OS keyring. It also stores non-secret settings such as the Windows attachment writer in `config.toml`. Environment variables and shell-command secret managers still work at call time (the resolver checks `env > keyring > command`), but you configure those by hand — see *Manual setup*. The wizard never edits your shell rc or env.

1. **TTY guard** — refuses to run if stdin isn't a real terminal (protects tokens from being fed in through AI-agent shell tools).
2. **Env-token detection** — if atls already finds a token in your environment (`ATLS_DEFAULT_<PRODUCT>_TOKEN` or `JIRA_PERSONAL_TOKEN` etc.), the wizard says so up front. Because env outranks the keyring, it **skips** those products (a keyring entry would just be shadowed) and leaves your env setup untouched. To move one to the keyring: unset its env var, remove it from your shell rc, open a **new** terminal, and re-run.
3. **Windows only: attachment writer [1/5].** Native is the visible default and starts no helper process. Keep it unless downloaded attachments are altered or blocked by Windows file-protection software. Compatibility is an explicit per-user setting that applies to all profiles; setup checks Git Bash, Perl, and `Digest::SHA` before saving it. Re-running setup shows and preserves the current choice when you press Enter.
4. **Product steps — [1/4]–[3/4] normally, [2/5]–[4/5] on Windows.** Each step prints the current URL + where the token lives (`environment variable (VAR)` or `keyring storage`), then asks `[s]kip / [e]dit / [r]emove` (default `s`, or `e` when there's nothing yet). `skip` leaves it as-is. `[e]dit` prompts for the URL (saved to `~/.config/atlassian-skills/config.toml`); then, unless the product's token is in the environment, prints the PAT issuer link and takes a hidden PAT prompt → `keyring.set_password("atls-<profile>", "<product>_token", …)`. `[r]emove` clears the URL and deletes the product's keyring entry (it never touches your env vars or shell rc).
5. **Keyring availability** — if the `keyring` package can't be imported the wizard aborts with a reinstall hint. After saving, if the session looks headless (Docker / WSL / no D-Bus / text-only SSH) it warns that the keyring may be locked and points you at env (Manual setup).
6. **AI agent skills — [4/4] normally, [5/5] on Windows.** `[Y/n]` prompt for each:
   - Claude Code (default `Y`): `~/.claude/skills/atls/SKILL.md` + routing block in `~/.claude/CLAUDE.md`
   - Codex (default `Y`): `~/.codex/skills/atls/SKILL.md` + routing block in `~/.codex/AGENTS.md`
   - GitHub Copilot (default `Y`): `~/.copilot/skills/atls/SKILL.md` + routing block in [`~/.copilot/copilot-instructions.md`](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions). Cross-platform via `Path.home()` — works identically on Linux, macOS, and Windows (`%USERPROFILE%\.copilot\...`). WSL note: `~/.copilot` here lives in the WSL filesystem and is invisible to a native Windows Copilot CLI install; the wizard prints a one-line warning when this is detected.
7. **Verify** — probes each provider (`auth status --resolve`) so you see whether each product actually resolves (`source=env` / `keyring`) before you exit.

Re-run `atls setup` any time. Defaults are non-destructive (`s` skips a product, `Y` installs an agent skill), and storage flips to keyring only when you actually store a token — a pure Enter-through leaves an env-based setup exactly as it was.

</details>

## Quick Start

```bash
# Jira
atls jira issue get PROJ-1
atls jira issue search "project=PROJ AND status=Open" --limit=20
atls jira issue create --project PROJ --type Story --summary "New feature" --body-file=story.md --body-format=md

# Confluence read and managed edit
atls confluence page get 12345
atls confluence page search "space=DOCS AND title=API"
atls confluence page get 12345 --body-repr=md --format=md
atls confluence page copy 12345 --parent-id 67890 --space DOCS --title "run-20260717-page-001" --include-attachments --verify --reason "Validation baseline" --dry-run --format=json
atls confluence page copy 12345 --parent-id 67890 --space DOCS --title "run-20260717-page-001" --include-attachments --verify --reason "Validation baseline" --format=json
atls confluence page md pull 12345 --output=page.md --resolve-assets=sidecar --asset-dir=assets/ --format=json
atls confluence page md validate page.md --format=json
atls confluence page md push 12345 --md-file=page.md --if-version 15 --dry-run --format=json

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
atls jira issue get PROJ-1 --format=md

# 3. Use json format for automation/parsing
atls jira issue get PROJ-1 --format=json | jq '{key, summary, status}'

# 4. Confluence managed page editing workflow
atls confluence page md pull PAGE_ID -o page.md --resolve-assets=sidecar --asset-dir=assets/ --format=json
# Edit page.md. The portable v3 manifest and adjacent asset comments are the only local baseline.
atls confluence page md validate page.md --format=json
atls confluence page md push PAGE_ID --md-file page.md --if-version 15 --dry-run --format=json
# If JSON reports migration_consent_required, show the loss summary first and obtain informed user approval.
# Then run exactly the returned next_actions[].argv; never synthesize or persist its fingerprint.
atls confluence page md push PAGE_ID --md-file page.md --if-version 15 --reason "Update documentation" --minor-edit

# 5. Branch on exit codes
# 0=OK, 2=not found/usage, 3=permission, 4=state/output conflict,
# 5=stale version, 6=auth failure, 7=validation/migration,
# 10=network, 11=rate limited
```

### Confluence pull-first Markdown

For a current-page baseline clone, use `confluence page copy` only with a verified run-owned destination parent and an
explicit unique run-scoped `--title`. It reads the source without modifying it, stages every
attachment before creating anything, creates a version-1 destination from the exact storage body, uploads each attachment
under its exact Confluence title, and verifies the destination storage and attachment bytes. If the source has attachments,
`--include-attachments` is required; use `--dry-run` first and keep `--verify` enabled. `--reason` adds a visible comment to
the new page. A failed copy removes only a destination whose exact title, space, parent, version, storage, and empty initial
attachment set prove that this command created it. An unknown create outcome reports recovery candidates in JSON and never
guesses an ID for upload or cleanup. Never copy into the source tree or treat a user-supplied arbitrary parent as run-owned.
Page history, comments, labels,
restrictions, likes, watchers, and attachment version history are intentionally not copied.

Choose the narrowest workflow that matches the intended change:

- Read: `page get PAGE_ID --body-repr=md` returns content-only readable Markdown. `--body-repr=view --format=raw` returns exact server-rendered HTML. Neither is publish input.
- Inspect: `page inspect PAGE_ID --format=json` reports conversion loss and recommends `patch-text`, `md pull`, or the exact-append proof path without writing.
- Small text correction: dry-run `page patch-text PAGE_ID --find ... --replace ... --if-version N --format=json`, then repeat only when exactly one decoded plain-text storage leaf is patchable. Batch patches use a versioned `--patch-file`. Attributes, macro/code bodies, duplicates, overlaps, boundary-spanning matches, and remote drift fail before PUT.
- Structure, formatting, table, or image work: `page md pull PAGE_ID --output page.md --format=json`. The output is portable managed Markdown with a v3 manifest and source-bound cfxmark migration comments. It may be copied or moved; there is no checkout registry or one-path restriction.
- Locally authored Markdown: `page create` or `page update --body-format=md` uses the same source-conversion loss report and informed-consent fingerprint as managed push.

**A pull writes a file only where `compatibility.canonical_write_permitted` is true.** Read that field; never infer it from the grade name. Where it is true the status is `pulled` or `pulled_with_migrations`; where it is false the pull returns `not_pulled` with `path: null` and a `next_actions` argv to run — it writes nothing, because a file that cannot be published is not a work product, and it is what the next person edits. A grade is permitted only where a preservation capability is registered *and* covers that page; `preservation_capability` and `identity_preservation_capability` name the one that applied. The file contains no raw storage XHTML or credentials. Unknown/opaque constructs are preserved only when the cfxmark artifact proves that behavior; otherwise they appear as migration loss.

Before publishing, run `md validate` and `md push --dry-run --format=json`. Local validation checks only the manifest, Markdown bytes, and referenced local assets, and reports `remote_freshness=not_checked`. Push fetches fresh storage/version, verifies page/site/resource identity, and evaluates proofs in this order:

1. `no_change`
2. `exact_remote_prefix_append`
3. `full_migration`

Exact EOF append converts only the appended fragment and preserves the complete existing remote storage prefix byte-for-byte. Any edit within existing Markdown, asset change, ambiguity, or new blocker disqualifies that path.

A lossy full migration never writes without the exact `migration_fingerprint` returned by the current dry-run. Show the loss summary before asking for approval. On approval, execute the response's `next_actions[].argv` exactly; the argv contains only CLI constants and arguments supplied by the caller. Do not auto-approve, retain the fingerprint in a file/config/cache, or reuse it after the remote source or local candidate changes. The final command repeats version/hash checks immediately before mutation and verifies a fresh read-back.

Table backgrounds omitted from readable Markdown are reported as conversion loss/presentation diagnostics. They are not hidden protected state, and there is no `table-style` command. Hard line breaks use literal `<br>`. Image presentation metadata remains adjacent to its asset identity comment, for example `![alt](assets/a.png)<!-- cfxmark:img w=320 h=200 thumbnail=1 align=center --><!-- cfxmark:asset src="assets/a.png" -->`.

Assets are matched by attachment ID, remote version, remote filename, and local hash. Body and asset dirtiness are independent: unchanged references are not reuploaded, unreferenced files are ignored, and removing a reference never deletes the remote attachment. Partial progress is recorded in operation comments inside the managed Markdown, without raw storage, full Markdown copies, or credentials. Success removes the operation comments. After a crash or response loss, rerun the same `md push`; it reconciles `upload_unknown`, `body_put_not_observed`, `readback_pending`, `reconciled`, or `conflict` from fresh remote evidence and never treats an unproved upload or PUT as success.

The durable operation journal belongs only to managed `md push`. `page create`, `page update`, `page copy`, and
`patch-text` use their documented fresh-read/read-back or idempotent-selector contracts but do not gain the managed-file
journal. Direct `page update --body-format=storage` accepts caller-authored storage bytes and is outside Markdown
conversion consent; it still performs version and read-back checks. A cfxmark version change invalidates pending
consent fingerprints and the converter binding in existing managed files.

Independent `confluence attachment upload` and `upload-batch` remain filename-oriented compatibility surfaces with explicit `--if-exists` policy. They do not participate in managed Markdown's attachment proof. `atls setup uninstall` preserves managed files, assets, configuration, credentials, and remote content by default. `--state --yes` only removes a verified legacy 0.3.0-candidate SQLite artifact and never opens it as runtime authority.

Attachment downloads use authenticated streaming with a 100 MiB per-response hard limit. Oversized or falsely declared responses fail before local publication instead of consuming unbounded memory or disk.

See [the 0.3 migration guide](docs/confluence-markdown-0.3-migration.md) for the complete lifecycle and recovery rules.

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

With `--format=json`, structured result and error envelopes are written to stdout and stderr remains empty. Human-readable warnings and diagnostics use stderr so Markdown or other primary stdout payloads stay clean.

> Some commands use `-f` for file input (e.g. `md push`). After the subcommand, always use the long form `--format=` to avoid ambiguity.

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

### Confluence
- `confluence page get|inspect|search|children|history|diff|images|create|copy|update|patch-text|delete|move|recover-assets`
- `confluence page md pull|push|validate|compare|prepare-reconcile|record-reconciled-against|rebaseline|prepare-merge|finalize-merge`
- `confluence page xhtml pull|push|validate|compare|set-authority`
- `confluence space tree`
- `confluence comment list|add|reply`
- `confluence label list|add`
- `confluence attachment list|download|download-all|upload|upload-batch|delete`
- `confluence user search`

> `--passthrough-prefix` is supported on Confluence Markdown conversion commands: readable `page get`, `md push`, and `md pull`.
>
> `compare` means "this local file against the page as it stands now"; `diff` means two things that already exist on the server — `page diff` between two versions, `bitbucket pr diff`.

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
- `setup uninstall [--dry-run] [--state|--config|--credentials --yes]` — remove only explicit atls-owned surfaces
- `setup --skills-only` — silent skill refresh, used by `atls upgrade`
- `doctor` — diagnose installation: platform, paths, skill version markers, auth resolution
- `auth login|status|list`
- `config get|set|path`
- `upgrade` — auto-detects uv / pipx / pip and refreshes skill assets
- `version [--check]` — show installed version; `--check` exits 1 if outdated vs PyPI
- `setup codex|claude|all|paths|status` **(deprecated compatibility shims in 0.3.x; removal planned for 0.4.0)** — replaced by `setup` (wizard) and `doctor`

## Corporate network (proxy & TLS)

If atls fails before it ever reaches your instance, start here:

```bash
atls doctor --check-auth          # classifies 401 / 403 / proxy interception / TLS / DNS
atls --verbose 2 jira user me     # per-request log on stderr (tokens are redacted)
```

`--verbose` writes to **stderr** only, so `--format=json` on stdout stays parseable. Levels are
`1` (one line per request), `2` (+ headers, proxy env), `3` (+ response shape — never the body).

### Proxy

atls uses the standard environment variables (`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) through httpx.

**`NO_PROXY` does not accept `*` wildcards.** Each entry is matched as a suffix, so:

| `NO_PROXY` value | matches `jira.corp.example.com`? |
|---|---|
| `*.corp.example.com` | **no** — the `*` is taken literally |
| `.corp.example.com` | yes (subdomains) |
| `corp.example.com` | yes (the domain and its subdomains) |

Listing every host individually is not necessary — drop the `*` and use the leading-dot form.

A proxy that intercepts the request usually shows up as a `3xx` or as a `200` with
`content-type: text/html`; both are reported explicitly by `atls doctor --check-auth`.

### TLS with a private / self-signed CA

Since 0.3.2, atls verifies TLS against the **OS trust store** by default (via
[truststore](https://github.com/sethmlarson/truststore), the same mechanism pip uses). If the
corporate root CA is installed in the operating system — it is, if the browser on the same
machine trusts your instance — atls needs **no TLS configuration at all**.

When you do need to override (precedence: `ca_bundle` → `SSL_CERT_FILE`/`SSL_CERT_DIR` → OS
trust store):

```toml
# Preferred: per-profile, applies to atls only — cannot break other tools
[profiles.default]
jira_url = "https://jira.corp.example.com"
ca_bundle = "/etc/ssl/corp/root-ca.pem"     # a PEM file (or an OpenSSL hashed directory)
```

```bash
# Environment — CAUTION: process-global. uv, pip, node and anything else that honours
# SSL_CERT_FILE reads it too, and a file they cannot parse breaks *them* as well
# (a broken SSL_CERT_FILE stops uv from building its HTTP client at all).
# The file must be strictly PEM (Base-64). Prefer ca_bundle above.
export SSL_CERT_FILE=/etc/ssl/corp/root-ca.pem
```

```bash
# Directory form — requires an OpenSSL *hashed* layout (c_rehash), not a plain
# folder of .pem files. A plain folder is accepted silently and adds no trust,
# then fails at handshake time. Prefer SSL_CERT_FILE unless you already run c_rehash.
export SSL_CERT_DIR=/etc/ssl/corp/certs.d
```

`atls doctor` prints which of these is actually in effect, checks that an `SSL_CERT_FILE`
actually loads as PEM, and warns about the hashed-layout trap.

**Exporting the CA on Windows.** The certificate has to be PEM (Base-64). `Export-Certificate`
writes DER (or SST for multiple certs), so either export the single corporate root from
`certmgr.msc` as *Base-64 encoded X.509 (.CER)*, or convert an existing DER file:

```powershell
certutil -encode corp-root-der.cer corp-root.pem
```

Do not create the file with PowerShell redirection (`>` / `Out-File`) — that writes UTF-16,
which OpenSSL (and uv) cannot parse. `certutil -encode` writes it correctly.

### Upgrading behind a TLS-inspecting proxy

The download itself can fail certificate verification, which is separate from atls's own TLS config:

```bash
atls upgrade --system-certs        # uv installs: trusts the OS certificate store
SSL_CERT_FILE=/etc/ssl/corp/root-ca.pem atls upgrade     # any installer
```

`--system-certs` is opt-in rather than automatic, because older `uv` builds reject the flag.

## Write Safety

- Use `--dry-run` where the command exposes it.
- Use `confluence page copy --parent-id RUN_PARENT --space SPACE --title UNIQUE_RUN_TITLE --include-attachments --verify --reason TEXT --dry-run --format=json` before creating a verified run-owned Server/DC baseline clone.
- Use `--if-version N` for Confluence page update and managed `md push`.
- Use `--if-updated ISO` for Jira updates.
- Managed `md push` requires `--md-file PATH` and does not accept legacy attachment upload flags. Its smart asset plan comes from the portable manifest, adjacent asset records, local hashes, and a fresh remote attachment inventory.
- Use `confluence attachment upload|upload-batch` for attachment operations outside the managed Markdown workflow.

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
| Confluence edits can silently alter content | Source-bound cfxmark proof and explicit informed loss consent |
| Silent character dropping in Jira descriptions | Byte-preserving `--format=raw` mode |

**Token-compatible auth**: If you already have `JIRA_PERSONAL_TOKEN` and `CONFLUENCE_PERSONAL_TOKEN` set for mcp-atlassian, atls picks them up automatically — no reconfiguration needed.

## Architecture

- **CLI-first**: All functionality accessible via the `atls` binary. AI agent skills are thin wrappers that invoke CLI commands.
- **Single HTTP client**: `httpx`-based `BaseClient` with retry (429/5xx), pagination, and auth.
- **cfxmark integration**: Confluence storage/Markdown artifacts carry typed diagnostics, source maps, preservation signatures, and presentation; Jira wiki conversion uses the same dependency.
- **Portable state-free control plane**: the managed Markdown manifest binds page/site/version/source hashes, while fresh remote reads and source-bound cfxmark proofs authorize writes. Recovery uses bounded operation comments that are removed after success; there is no global publication database.
- **Pydantic v2 models**: Strict response parsing for stable fields, with Jira `customfield_*` passthrough in JSON output.

## Key Dependencies

| Package | Purpose |
|---|---|
| httpx | REST client (sync) |
| typer + rich | CLI framework |
| pydantic | Response models |
| cfxmark >=0.6.1,<0.6.2 | Source-bound ownership proofs, managed Markdown projection, migration diagnostics, and Jira wiki conversion |
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

# Install the built wheel into a throwaway venv and exercise it end to end.
# `uv run pytest` measures the working tree, which is not what anyone installs:
# missing package data, a broken console entry point, or a runtime dependency
# declared only as a dev extra all pass the tree and fail the artifact.
bash scripts/wheel_smoke.sh 0.4.2
```

This release requires **cfxmark 0.6.1** exactly — `cfxmark>=0.6.1,<0.6.2`. The pin is deliberate: two preservation
capabilities are bound to that converter build so neither is inherited by a build it was
never measured against, and on a newer patch they stop matching silently.

## Roadmap

- **0.1.x** — Jira + Confluence read/write, md push/md pull/md diff, benchmarks, GitHub Actions CI/release
- **0.2.x** — Bitbucket Server/DC PR workflow + Skill-first Claude/Codex integration
- **0.2.7** — `atls setup` interactive wizard + `atls doctor`; `setup all/codex/claude/paths/status` deprecated
- **0.4.0** — portable Markdown-first Confluence workflow, source-bound informed consent, exact EOF-append preservation, state-free asset/body recovery, readable view/inspect, and smart asset synchronization
- **0.4.1** — an explicit, two-step approval for a deliberate whole-page rewrite; `attachment upload-batch --if-exists` actually implemented
- **0.4.3 (current)** — publishing a document with a picture a second time no longer fails on its first image; a stored filename gets a new attachment version instead of a refused create
- **0.4.2** — a full-replacement refusal prints the approval command it tells you to run, and only the command shape the CLI actually produces
- **0.4.0+** — typed table-style editing, Async client, caching, non-interactive `atls setup`, fish shell support, multi-profile wizard

## License

[MIT](./LICENSE)
