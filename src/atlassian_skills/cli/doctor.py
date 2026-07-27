from __future__ import annotations

import re
import ssl
from pathlib import Path
from typing import Any

import typer

from atlassian_skills.cli.auth import _resolve_url, render_auth_status
from atlassian_skills.cli.setup import (
    _detect_platform,
    _detect_shell,
    _get_claude_command_target,
    _get_claude_config_dir,
    _get_claude_md_path,
    _get_claude_skill_target,
    _get_codex_agents_path,
    _get_codex_config_dir,
    _get_codex_legacy_target,
    _get_codex_skill_target,
    _get_copilot_config_dir,
    _get_copilot_instructions_path,
    _get_copilot_skill_target,
    _is_fish,
    _is_git_bash,
    _legacy_claude_command_notice,
    _legacy_codex_skill_notice,
)
from atlassian_skills.core.errors import (
    AtlasError,
    AuthError,
    ForbiddenError,
    NetworkError,
    RedirectError,
    safe_header_value,
)

# Probe endpoints, deliberately hit through the *low-level* client. The
# high-level helpers (`JiraClient.get_myself`, `ConfluenceClient.get_current_user`)
# call `.json()` and `model_validate` immediately, so an intercepting proxy's HTML
# would blow up as a parse error with no chance to read the content type — and
# Bitbucket's `_get_current_user_slug` would hand back the HTML as a user name.
_AUTH_PROBES: dict[str, tuple[str, dict[str, Any] | None]] = {
    "jira": ("/rest/api/2/myself", None),
    "confluence": ("/rest/api/user/current", None),
    # Bitbucket Server has no "current user" endpoint. This is the user
    # *directory*, so the body describes whoever happens to sort first, not the
    # caller — reading a name out of it would report a stranger as the
    # authenticated user. Identity comes from the X-AUSERNAME response header,
    # the same source BitbucketClient._get_current_user_slug uses.
    "bitbucket": ("/rest/api/1.0/users", {"limit": 1}),
}
_IDENTITY_FROM_HEADER = frozenset({"bitbucket"})
_PROBE_TIMEOUT = 10.0


def _probe_name(payload: Any) -> str | None:
    """Pull the caller's name out of a 2xx probe body, or None if it isn't there.

    Only top-level fields are read. Descending into a collection would pick up
    some other account, which is worse than printing no name at all.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("displayName", "name", "username"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _classify_probe_error(error: AtlasError) -> str:
    """Turn an AtlasError from a probe into a corporate-network diagnosis.

    Everything except a 2xx arrives here as an exception: `BaseClient.get` raises
    instead of returning a non-2xx response, so this is the only classification
    path for 401/403/3xx/TLS/transport failures.
    """
    context = error.context or {}
    reason = context.get("reason")
    if reason == "redirected_to_login":
        return "redirected to a login page — the token is not accepted (or a SSO portal is in front)"
    if reason == "unsafe_redirect":
        target = context.get("target_host") or "another host"
        return f"redirected off-origin to {target} — a proxy is probably intercepting"
    if isinstance(error, RedirectError):
        return f"unexpected redirect ({error.message}) — check HTTPS_PROXY/NO_PROXY"
    if isinstance(error, AuthError):
        return "401 — token invalid or expired"
    if isinstance(error, ForbiddenError):
        return "403 — token is valid but lacks permission"
    if isinstance(error, NetworkError):
        ssl_error = _find_ssl_error(error)
        if ssl_error is not None or _looks_like_tls_failure(error):
            # Keep the OpenSSL text: "certificate verify failed: self-signed
            # certificate in certificate chain" tells the user *which* trust
            # problem they have; "TLS verification failed" alone tells them
            # nothing they can act on (GitHub #16 follow-up).
            detail = safe_header_value(str(ssl_error) if ssl_error is not None else error.message, 200)
            return (
                f"TLS verification failed ({detail}) — set ca_bundle in the profile, or point "
                "SSL_CERT_FILE at your corporate CA bundle (PEM)"
            )
        return f"connection failed — {error.message}"
    return f"{error.code}: {error.message}"


_TLS_MESSAGE_MARKERS = ("certificate_verify_failed", "ssl:", "certificate verify failed")


def _find_ssl_error(error: BaseException) -> ssl.SSLError | None:
    """Walk the exception chain and return the underlying ``ssl.SSLError``, if any.

    Production raises ``httpx.ConnectError`` from ``httpcore`` from ``ssl.SSLError``,
    so the chain is authoritative there.
    """
    node: BaseException | None = error
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        if isinstance(node, ssl.SSLError):
            return node
        node = node.__cause__ or node.__context__
    return None


def _looks_like_tls_failure(error: AtlasError) -> bool:
    """Chain walk first; message markers as fallback.

    Some layers replace ``__cause__`` with their own wrapper, which drops the ssl
    error from the chain entirely — the OpenSSL text survives in the message, so
    that is checked too.
    """
    if _find_ssl_error(error) is not None:
        return True
    lowered = error.message.lower()
    return any(marker in lowered for marker in _TLS_MESSAGE_MARKERS)


def _check_auth(profile_name: str) -> None:
    """Probe each configured product with the resolved credential.

    `--check-auth` itself is the opt-in: the user has asked atls to call their
    instance, so the credential is resolved from whatever the profile is
    configured with — env, keyring, or command — without a second flag. (0.3.1
    additionally required `--resolve-credentials`, which made every keyring user
    type two flags for one action — GitHub #16 follow-up.) Plain `doctor` still
    resolves nothing and calls nothing.
    """
    from atlassian_skills.core.auth import resolve_credential
    from atlassian_skills.core.client import BaseClient
    from atlassian_skills.core.config import get_profile, load_config
    from atlassian_skills.core.tls import build_ssl_context

    profile = get_profile(load_config(), profile_name)
    urls = {
        "jira": profile.jira_url,
        "confluence": profile.confluence_url,
        "bitbucket": profile.bitbucket_url,
    }
    for product, (path, params) in _AUTH_PROBES.items():
        url, _source = _resolve_url(profile_name, product, urls[product])
        if not url:
            continue
        try:
            credential = resolve_credential(profile_name, product, profile)
        except AtlasError as exc:
            typer.echo(f"  {product}: credential unavailable — {exc.message}")
            continue
        try:
            with BaseClient(
                url.rstrip("/"),
                credential,
                timeout=_PROBE_TIMEOUT,
                verify=build_ssl_context(profile.ca_bundle),
            ) as client:
                response = client.get(path, params=params)
        except AtlasError as exc:
            typer.echo(f"  {product}: {_classify_probe_error(exc)}")
            continue
        except Exception as exc:  # doctor must never abort on a probe
            typer.echo(f"  {product}: probe failed ({type(exc).__name__}: {safe_header_value(str(exc), 200)})")
            continue
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            typer.echo(
                f"  {product}: 200 but content-type is {content_type or 'unset'} — "
                "a proxy or SSO portal is answering instead of Atlassian"
            )
            continue
        try:
            payload = response.json()
        except ValueError:
            typer.echo(f"  {product}: 200 with an unparseable JSON body — not an Atlassian API response")
            continue
        if product in _IDENTITY_FROM_HEADER:
            name = safe_header_value(response.headers.get("X-AUSERNAME")) or None
        else:
            name = _probe_name(payload)
        typer.echo(f"  ✓ {product}: authenticated{f' as {name}' if name else ''}")


def _print_update_status(skip: bool = False) -> None:
    """Show installed version + whether a newer release is on PyPI. Shown at the top of `doctor`.

    Network is best-effort with a short timeout; offline / failures degrade to a neutral line so
    `doctor` never hangs or errors on the version check.
    """
    from atlassian_skills import __version__

    if skip:
        typer.echo(f"atls {__version__}  (update check skipped)")
        return
    from atlassian_skills.cli.version import is_outdated, latest_pypi_version

    latest = latest_pypi_version()
    if latest is None:
        typer.echo(f"atls {__version__}  (couldn't reach PyPI — update check skipped)")
    elif is_outdated(__version__, latest):
        typer.echo(f"⚠ Update available: atls {__version__} → {latest}.  Run 'atls upgrade'.")
    else:
        typer.echo(f"✓ atls {__version__} (up to date)")


def _print_skill_status(label: str, target: Path, hide_when_absent: bool = False) -> None:
    """Print one skill install line — version marker if present, else 'not installed'.

    `hide_when_absent=True` keeps legacy paths off the output for the 95% of users who
    installed cleanly post-0.2.5; they only matter when the file actually exists, in
    which case the legacy-notice helpers (`_legacy_*_notice`) describe the migration.
    """
    if not target.exists():
        if hide_when_absent:
            return
        typer.echo(f"  {label}: not installed ({target})")
        return
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        typer.echo(f"  {label}: present at {target} (could not read)")
        return
    if "installed-by: atls" in content:
        m = re.search(r"installed-by: atls (\S+)", content)
        ver = m.group(1) if m else "unknown"
        typer.echo(f"  {label}: installed (v{ver}) at {target}")
    else:
        typer.echo(f"  {label}: found at {target} (no version marker)")


def _print_routing_block_version(label: str, path: Path) -> None:
    if not path.exists():
        typer.echo(f"  {label}: not found ({path})")
        return
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        typer.echo(f"  {label}: present at {path} (could not read)")
        return
    m = re.search(r"ATLS:VERSION:(\S+)", content)
    if m:
        typer.echo(f"  {label}: ATLS block v{m.group(1)} at {path}")
    else:
        typer.echo(f"  {label}: no ATLS block at {path}")


def doctor(
    ctx: typer.Context,
    resolve_credentials: bool = typer.Option(
        False,
        "--resolve-credentials",
        help="Probe keyring/command providers when reporting auth (may prompt or run a shell command).",
    ),
    check_auth: bool = typer.Option(
        False,
        "--check-auth",
        help=(
            "Call each configured product with the profile's credential and classify the result. "
            "Resolves the credential from env, keyring, or command as configured (may prompt)."
        ),
    ),
    no_update_check: bool = typer.Option(
        False,
        "--no-update-check",
        help="Skip the PyPI latest-version check (use offline / to avoid the network call).",
    ),
) -> None:
    """Diagnose atls installation: version freshness, platform, paths, skill status, auth resolution."""
    ctx.ensure_object(dict)
    _print_update_status(no_update_check)
    typer.echo("")

    platform_name = _detect_platform()
    shell = _detect_shell()
    typer.echo(f"Platform: {platform_name} (shell: {shell})")
    if _is_fish():
        typer.echo("  Note: fish shell detected — env vars use `set -gx`, not `export`. The keyring-only")
        typer.echo("        wizard runs fine; only manual env-var setup differs (see README → Manual setup).")
    if _is_git_bash():
        typer.echo("  Note: Git Bash detected — env vars are still read from HKCU\\Environment.")
    from atlassian_skills.core.config import load_config

    writer_mode = load_config().attachment_writer
    typer.echo(f"  Attachment writer: {writer_mode}")
    if writer_mode == "compatible":
        if platform_name != "windows":
            typer.echo("  Attachment compatibility: inactive outside Windows")
        else:
            from atlassian_skills.core.attachment_io import verify_compatible_attachment_writer
            from atlassian_skills.core.errors import AtlasError

            try:
                bash_path = verify_compatible_attachment_writer()
            except AtlasError as exc:
                typer.echo(f"  Attachment compatibility: unavailable ({exc.message})")
            else:
                typer.echo(f"  Attachment compatibility dependencies: available ({bash_path})")
    typer.echo("")

    typer.echo("Paths:")
    typer.echo(f"  Claude config dir   : {_get_claude_config_dir()}")
    typer.echo(f"  Claude skill target : {_get_claude_skill_target()}")
    typer.echo(f"  CLAUDE.md path      : {_get_claude_md_path()}")
    typer.echo(f"  Codex config dir    : {_get_codex_config_dir()}")
    typer.echo(f"  Codex AGENTS.md path: {_get_codex_agents_path()}")
    typer.echo(f"  Codex skill target  : {_get_codex_skill_target()}  (canonical)")
    typer.echo(f"  Copilot config dir  : {_get_copilot_config_dir()}")
    typer.echo(f"  Copilot skill target: {_get_copilot_skill_target()}")
    typer.echo(f"  Copilot instructions: {_get_copilot_instructions_path()}")
    if _get_claude_command_target().exists() or _get_codex_legacy_target().exists():
        typer.echo("  legacy paths (only shown when present):")
        if _get_claude_command_target().exists():
            typer.echo(f"    Claude command (legacy)   : {_get_claude_command_target()}")
        if _get_codex_legacy_target().exists():
            typer.echo(f"    Codex legacy skill target : {_get_codex_legacy_target()}")
    typer.echo("")

    typer.echo("Skill installation status:")
    _print_skill_status("Claude skill", _get_claude_skill_target())
    _print_skill_status("Claude command (legacy)", _get_claude_command_target(), hide_when_absent=True)
    _print_skill_status("Codex skill", _get_codex_skill_target())
    _print_skill_status("Codex legacy skill", _get_codex_legacy_target(), hide_when_absent=True)
    _print_skill_status("Copilot skill", _get_copilot_skill_target())

    legacy_cmd = _legacy_claude_command_notice()
    if legacy_cmd:
        typer.echo(legacy_cmd)
    legacy_codex = _legacy_codex_skill_notice()
    if legacy_codex:
        typer.echo(legacy_codex)

    _print_routing_block_version("Codex AGENTS.md", _get_codex_agents_path())
    _print_routing_block_version("CLAUDE.md", _get_claude_md_path())
    _print_routing_block_version("Copilot instructions", _get_copilot_instructions_path())
    typer.echo("")

    # `--profile` lives on the root callback; doctor used to hardcode "default"
    # here, so `atls --profile corp doctor` reported the wrong profile entirely.
    profile_name = str(ctx.obj.get("profile") or "default")

    typer.echo("Auth:")
    render_auth_status(profile_name, resolve=resolve_credentials)

    from atlassian_skills.core.config import get_profile
    from atlassian_skills.core.tls import describe_verify_source

    verify_source, verify_warning = describe_verify_source(get_profile(load_config(), profile_name).ca_bundle)
    typer.echo(f"  TLS verify:     {verify_source}")
    if verify_warning:
        typer.echo(f"    ⚠ {verify_warning}")

    if check_auth:
        typer.echo("")
        typer.echo(f"Auth probe (profile: {profile_name}):")
        _check_auth(profile_name)
