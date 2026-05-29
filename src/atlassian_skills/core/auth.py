from __future__ import annotations

import base64
import subprocess
from dataclasses import dataclass
from typing import Literal

from atlassian_skills.core.config import Profile, get_env_auth_method, get_env_token, get_env_user
from atlassian_skills.core.errors import AuthError


@dataclass
class Credential:
    method: Literal["pat", "basic"]
    token: str
    username: str | None = None

    def __repr__(self) -> str:
        """Redact the raw token in repr / debug output to avoid leaks via tracebacks / logs."""
        return f"Credential(method={self.method!r}, token=***redacted***, username={self.username!r})"

    __str__ = __repr__

    def to_header(self) -> dict[str, str]:
        """Return the Authorization header dict for this credential."""
        if self.method == "pat":
            return {"Authorization": f"Bearer {self.token}"}
        encoded = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}


def _resolve_token_from_provider(profile_name: str, product: str, profile: Profile) -> str | None:
    """Resolve token via keyring or shell command, based on profile.storage."""
    if profile.storage == "keyring":
        try:
            import keyring
        except ImportError as exc:
            raise AuthError(
                f"storage='keyring' configured for profile '{profile_name}' but the keyring package "
                "could not be imported (it ships with atls by default — your install may be broken).",
                hint="Reinstall, e.g.: uv tool install --force 'atlassian-skills[keyring]' "
                "(pipx: pipx inject atlassian-skills keyring; pip: pip install 'atlassian-skills[keyring]')",
            ) from exc
        try:
            return keyring.get_password(f"atls-{profile_name}", f"{product}_token")
        except Exception as exc:  # noqa: BLE001 — keyring.errors.KeyringError (no backend / locked / init)
            # No usable backend / locked store (headless Linux without D-Bus, locked Keychain,
            # SSH/Docker/WSL). The only work here is the keyring lookup, so any failure means the
            # store is unreachable — surface it as AuthError so callers get a clean message + exit
            # code instead of a raw traceback. (Catching the base Exception rather than
            # keyring.errors.KeyringError avoids a second lazy import just to name the class.)
            raise AuthError(
                f"the OS keyring is unavailable or locked for profile '{profile_name}', product '{product}'.",
                hint="If this is a headless/SSH/Docker/WSL session the keyring often can't unlock — "
                "use environment variables instead (README → Manual setup), or unlock the keyring.",
            ) from exc

    if profile.storage == "command":
        # Per-product command wins over the shared credential_command.
        command = getattr(profile, f"{product}_command", None) or profile.credential_command
        if not command:
            raise AuthError(
                f"storage='command' configured for profile '{profile_name}' but no command is set "
                f"for product '{product}'.",
                hint=f'Add {product}_command = "<shell command>" (or a shared credential_command) to the profile.',
            )
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise AuthError(
                f"credential command timed out for profile '{profile_name}', product '{product}'.",
                hint=f"Check that this command completes promptly: {command}",
            ) from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or command
            raise AuthError(
                f"credential command exited {result.returncode} for profile '{profile_name}', product '{product}'.",
                hint=f"stderr: {detail}",
            )
        return result.stdout.strip() or None

    return None


def resolve_credential(
    profile_name: str,
    product: str,
    profile: Profile,
    *,
    cli_token: str | None = None,
    cli_user: str | None = None,
    cli_auth: str | None = None,
) -> Credential:
    """Resolve credentials with priority: CLI flags > env vars > provider (keyring/command).

    Args:
        profile_name: Profile name (e.g. "corp") — used to build env var names.
        product: Product name (e.g. "jira", "confluence") — used to build env var names.
        profile: The resolved Profile object from config.
        cli_token: Token provided via CLI flag (highest priority).
        cli_user: Username provided via CLI flag (highest priority).
        cli_auth: Auth method override via CLI flag ("pat" or "basic").

    Returns:
        A resolved Credential.

    Raises:
        AuthError: If token is missing or basic auth is missing a username.
    """
    env_auth = get_env_auth_method(profile_name, product)
    raw_method = cli_auth or env_auth or getattr(profile.auth, product, "pat")
    method: Literal["pat", "basic"] = "basic" if raw_method == "basic" else "pat"

    env_token = get_env_token(profile_name, product)
    token = cli_token or env_token

    if not token and profile.storage in ("keyring", "command"):
        token = _resolve_token_from_provider(profile_name, product, profile)

    if not token:
        env_key = f"ATLS_{profile_name.upper()}_{product.upper()}_TOKEN"
        raise AuthError(
            f"No token found for profile '{profile_name}', product '{product}'.",
            hint=f"export {env_key}=<your-token>",
        )

    # Determine username for basic auth: CLI flag > env var
    env_user = get_env_user(profile_name, product)
    username = cli_user or env_user

    if method == "basic" and not username:
        env_user_key = f"ATLS_{profile_name.upper()}_{product.upper()}_USER"
        raise AuthError(
            f"Basic auth requires a username for profile '{profile_name}', product '{product}'.",
            hint=f"export {env_user_key}=<your-username>",
        )

    return Credential(method=method, token=token, username=username)
