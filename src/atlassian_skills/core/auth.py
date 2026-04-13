from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

from atlassian_skills.core.config import Profile, get_env_auth_method, get_env_token, get_env_user
from atlassian_skills.core.errors import AuthError


@dataclass
class Credential:
    method: Literal["pat", "basic"]
    token: str
    username: str | None = None

    def to_header(self) -> dict[str, str]:
        """Return the Authorization header dict for this credential."""
        if self.method == "pat":
            return {"Authorization": f"Bearer {self.token}"}
        encoded = base64.b64encode(f"{self.username}:{self.token}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}


def resolve_credential(
    profile_name: str,
    product: str,
    profile: Profile,
    *,
    cli_token: str | None = None,
    cli_user: str | None = None,
    cli_auth: str | None = None,
) -> Credential:
    """Resolve credentials with priority: CLI flags > env vars > config plaintext.

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
    # Determine auth method: CLI flag > env var > profile config
    env_auth = get_env_auth_method(profile_name, product)
    raw_method = cli_auth or env_auth or getattr(profile.auth, product, "pat")
    method: Literal["pat", "basic"] = "basic" if raw_method == "basic" else "pat"

    # Determine token: CLI flag > env var > (config plaintext not yet supported)
    env_token = get_env_token(profile_name, product)
    token = cli_token or env_token

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
