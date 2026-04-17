from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Literal

import tomli_w
from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class AuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    jira: Literal["pat", "basic"] = "pat"
    confluence: Literal["pat", "basic"] = "pat"
    bitbucket: Literal["pat", "basic"] = "pat"
    bamboo: Literal["pat", "basic"] = "basic"


class Profile(BaseModel):
    model_config = ConfigDict(frozen=False)

    jira_url: str | None = None
    confluence_url: str | None = None
    bitbucket_url: str | None = None
    bamboo_url: str | None = None
    auth: AuthConfig = AuthConfig()
    storage: Literal["env", "keyring", "plaintext"] = "env"
    ca_bundle: str | None = None


class Config(BaseModel):
    model_config = ConfigDict(frozen=False)

    default_profile: str = "default"
    profiles: dict[str, Profile] = {}


def config_path() -> Path:
    """Return the default config file path."""
    return Path(user_config_dir("atlassian-skills")) / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file. Returns empty Config if file does not exist."""
    target = path if path is not None else config_path()
    if not target.exists():
        return Config()
    with target.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)


def save_config(config: Config, path: Path | None = None) -> None:
    """Serialize config to TOML and write to disk with restricted permissions."""
    target = path if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(exclude_none=True)
    # Write with restricted permissions (owner read/write only)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        tomli_w.dump(data, f)
    # Ensure permissions even if file existed with broader perms
    os.chmod(str(target), stat.S_IRUSR | stat.S_IWUSR)


def get_profile(config: Config, name: str | None = None) -> Profile:
    """Return the named profile, or the default profile if name is None."""
    profile_name = name if name is not None else config.default_profile
    return config.profiles.get(profile_name, Profile())


# Legacy mcp-atlassian env var names (fallback)
_LEGACY_TOKEN_VARS: dict[str, str] = {
    "jira": "JIRA_PERSONAL_TOKEN",
    "confluence": "CONFLUENCE_PERSONAL_TOKEN",
    "bitbucket": "BITBUCKET_TOKEN",
}

_LEGACY_USER_VARS: dict[str, str] = {
    "bitbucket": "BITBUCKET_USERNAME",
}


def get_env_token(profile_name: str, product: str) -> str | None:
    """Read token from env. Priority: ATLS_{PROFILE}_{PRODUCT}_TOKEN > legacy vars."""
    # New format first
    key = f"ATLS_{profile_name.upper()}_{product.upper()}_TOKEN"
    val = os.environ.get(key)
    if val:
        return val
    # Legacy mcp-atlassian format
    legacy_key = _LEGACY_TOKEN_VARS.get(product.lower())
    if legacy_key:
        return os.environ.get(legacy_key)
    return None


def get_env_user(profile_name: str, product: str) -> str | None:
    """Read user from env. Priority: ATLS_{PROFILE}_{PRODUCT}_USER > legacy vars."""
    key = f"ATLS_{profile_name.upper()}_{product.upper()}_USER"
    val = os.environ.get(key)
    if val:
        return val
    legacy_key = _LEGACY_USER_VARS.get(product.lower())
    if legacy_key:
        return os.environ.get(legacy_key)
    return None


def get_env_auth_method(profile_name: str, product: str) -> str | None:
    """Read ATLS_{PROFILE}_{PRODUCT}_AUTH from environment."""
    key = f"ATLS_{profile_name.upper()}_{product.upper()}_AUTH"
    return os.environ.get(key)
