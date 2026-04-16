from __future__ import annotations

import base64
import os
from unittest.mock import patch

import pytest

from atlassian_skills.core.auth import Credential, resolve_credential
from atlassian_skills.core.config import Profile, get_env_auth_method, get_env_token, get_env_user
from atlassian_skills.core.errors import AuthError


class TestCredentialToHeader:
    def test_pat_returns_bearer(self) -> None:
        cred = Credential(method="pat", token="mytoken123")
        header = cred.to_header()
        assert header == {"Authorization": "Bearer mytoken123"}

    def test_basic_returns_base64(self) -> None:
        cred = Credential(method="basic", token="mypassword", username="alice")
        header = cred.to_header()
        expected = base64.b64encode(b"alice:mypassword").decode()
        assert header == {"Authorization": f"Basic {expected}"}

    def test_basic_encoding_is_correct(self) -> None:
        cred = Credential(method="basic", token="p@ss:word!", username="user@corp")
        header = cred.to_header()
        raw = base64.b64decode(header["Authorization"].replace("Basic ", ""))
        assert raw == b"user@corp:p@ss:word!"


class TestResolveCredential:
    def _profile(self) -> Profile:
        return Profile()

    def test_pat_from_env(self) -> None:
        profile = self._profile()
        env = {"ATLS_CORP_JIRA_TOKEN": "env-pat-token"}
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile)
        assert cred.method == "pat"
        assert cred.token == "env-pat-token"
        assert cred.username is None

    def test_cli_token_overrides_env(self) -> None:
        profile = self._profile()
        env = {"ATLS_CORP_JIRA_TOKEN": "env-token"}
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile, cli_token="cli-token")
        assert cred.token == "cli-token"

    def test_basic_from_env(self) -> None:
        profile = self._profile()
        env = {
            "ATLS_CORP_BAMBOO_TOKEN": "bamboo-pass",
            "ATLS_CORP_BAMBOO_USER": "bamboo-user",
            "ATLS_CORP_BAMBOO_AUTH": "basic",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "bamboo", profile)
        assert cred.method == "basic"
        assert cred.token == "bamboo-pass"
        assert cred.username == "bamboo-user"

    def test_cli_auth_overrides_env_method(self) -> None:
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "some-token",
            "ATLS_CORP_JIRA_USER": "some-user",
            "ATLS_CORP_JIRA_AUTH": "pat",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile, cli_auth="basic")
        assert cred.method == "basic"

    def test_cli_user_overrides_env_user(self) -> None:
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "tok",
            "ATLS_CORP_JIRA_USER": "env-user",
            "ATLS_CORP_JIRA_AUTH": "basic",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile, cli_user="cli-user")
        assert cred.username == "cli-user"

    def test_missing_token_raises_auth_error(self) -> None:
        profile = self._profile()
        env: dict[str, str] = {}
        remove_keys = ["ATLS_CORP_JIRA_TOKEN", "JIRA_PERSONAL_TOKEN"]
        cleaned = {k: v for k, v in os.environ.items() if k not in remove_keys}
        with patch.dict("os.environ", {**cleaned, **env}, clear=True), pytest.raises(AuthError) as exc_info:
            resolve_credential("corp", "jira", profile)
        err = exc_info.value
        assert "ATLS_CORP_JIRA_TOKEN" in (err.hint or "")

    def test_basic_without_username_raises_auth_error(self) -> None:
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "some-token",
            "ATLS_CORP_JIRA_AUTH": "basic",
        }
        with patch.dict("os.environ", env, clear=False), pytest.raises(AuthError) as exc_info:
            resolve_credential("corp", "jira", profile)
        err = exc_info.value
        assert "ATLS_CORP_JIRA_USER" in (err.hint or "")

    def test_profile_auth_method_used_as_fallback(self) -> None:
        """Profile auth config is used when no env/CLI auth method is set."""
        profile = self._profile()
        # bamboo defaults to "basic" in AuthConfig
        env = {
            "ATLS_CORP_BAMBOO_TOKEN": "bam-token",
            "ATLS_CORP_BAMBOO_USER": "bam-user",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "bamboo", profile)
        assert cred.method == "basic"

    def test_pat_no_username_needed(self) -> None:
        profile = self._profile()
        env = {"ATLS_CORP_JIRA_TOKEN": "pat-token"}
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile)
        assert cred.method == "pat"
        assert cred.username is None

    # -------------------------------------------------------------------
    # New tests (+8)
    # -------------------------------------------------------------------

    def test_pat_takes_precedence_when_both_token_and_user_set(self) -> None:
        """PAT is the default method; even if USER is present, method stays pat."""
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "pat-tok",
            "ATLS_CORP_JIRA_USER": "someone",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile)
        assert cred.method == "pat"
        assert cred.token == "pat-tok"

    def test_legacy_jira_personal_token(self) -> None:
        """JIRA_PERSONAL_TOKEN falls back when ATLS_* is absent."""
        profile = self._profile()
        remove_keys = ["ATLS_CORP_JIRA_TOKEN"]
        cleaned = {k: v for k, v in os.environ.items() if k not in remove_keys}
        env = {**cleaned, "JIRA_PERSONAL_TOKEN": "legacy-jira-token"}
        with patch.dict("os.environ", env, clear=True):
            cred = resolve_credential("corp", "jira", profile)
        assert cred.token == "legacy-jira-token"

    def test_legacy_confluence_personal_token(self) -> None:
        """CONFLUENCE_PERSONAL_TOKEN falls back when ATLS_* is absent."""
        profile = self._profile()
        remove_keys = ["ATLS_CORP_CONFLUENCE_TOKEN"]
        cleaned = {k: v for k, v in os.environ.items() if k not in remove_keys}
        env = {**cleaned, "CONFLUENCE_PERSONAL_TOKEN": "legacy-conf-token"}
        with patch.dict("os.environ", env, clear=True):
            cred = resolve_credential("corp", "confluence", profile)
        assert cred.token == "legacy-conf-token"

    def test_atls_takes_precedence_over_legacy(self) -> None:
        """ATLS_* token wins over JIRA_PERSONAL_TOKEN when both are present."""
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "atls-token",
            "JIRA_PERSONAL_TOKEN": "legacy-token",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile)
        assert cred.token == "atls-token"

    def test_cli_auth_basic_override(self) -> None:
        """cli_auth='basic' forces basic even when env sets pat."""
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "tok",
            "ATLS_CORP_JIRA_USER": "alice",
            "ATLS_CORP_JIRA_AUTH": "pat",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile, cli_auth="basic")
        assert cred.method == "basic"

    def test_cli_auth_pat_override(self) -> None:
        """cli_auth='pat' forces pat even when env sets basic."""
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "tok",
            "ATLS_CORP_JIRA_AUTH": "basic",
            "ATLS_CORP_JIRA_USER": "alice",
        }
        with patch.dict("os.environ", env, clear=False):
            cred = resolve_credential("corp", "jira", profile, cli_auth="pat")
        assert cred.method == "pat"

    def test_credential_to_header_empty_token(self) -> None:
        """Credential.to_header() with empty string token does not crash."""
        cred = Credential(method="pat", token="")
        header = cred.to_header()
        assert "Authorization" in header
        assert header["Authorization"] == "Bearer "

    def test_multiple_profiles_different_tokens(self) -> None:
        """Different env tokens per profile are resolved independently."""
        profile = self._profile()
        env = {
            "ATLS_CORP_JIRA_TOKEN": "corp-token",
            "ATLS_STAGING_JIRA_TOKEN": "staging-token",
        }
        with patch.dict("os.environ", env, clear=False):
            corp = resolve_credential("corp", "jira", profile)
            staging = resolve_credential("staging", "jira", profile)
        assert corp.token == "corp-token"
        assert staging.token == "staging-token"


class TestCredentialToHeaderExpanded:
    """Additional Credential.to_header() coverage."""

    def test_pat_header_format(self) -> None:
        """PAT header is exactly 'Bearer {token}'."""
        cred = Credential(method="pat", token="abc123")
        header = cred.to_header()
        assert header == {"Authorization": "Bearer abc123"}

    def test_basic_header_format(self) -> None:
        """Basic header is exactly 'Basic base64(user:token)'."""
        cred = Credential(method="basic", token="mypassword", username="bob")
        expected = base64.b64encode(b"bob:mypassword").decode()
        assert cred.to_header() == {"Authorization": f"Basic {expected}"}

    def test_basic_special_chars_in_token(self) -> None:
        """Token with ':', '=', and unicode characters encodes correctly."""
        token = "p:ass=wörd"
        cred = Credential(method="basic", token=token, username="user")
        header = cred.to_header()
        raw = base64.b64decode(header["Authorization"].removeprefix("Basic "))
        assert raw == f"user:{token}".encode()

    def test_basic_special_chars_in_username(self) -> None:
        """Username with '@', '.', and spaces encodes correctly."""
        username = "john.doe@corp example"
        cred = Credential(method="basic", token="tok", username=username)
        header = cred.to_header()
        raw = base64.b64decode(header["Authorization"].removeprefix("Basic "))
        assert raw == f"{username}:tok".encode()

    def test_basic_header_has_single_authorization_key(self) -> None:
        """to_header() returns exactly one key regardless of method."""
        for method, kw in [("pat", {}), ("basic", {"username": "u"})]:
            cred = Credential(method=method, token="t", **kw)  # type: ignore[arg-type]
            assert list(cred.to_header().keys()) == ["Authorization"]


class TestResolveCredentialTokenPriority:
    """Priority matrix: CLI token > env token."""

    @pytest.mark.parametrize(
        "cli_token,env_token,expected",
        [
            ("cli-tok", "env-tok", "cli-tok"),  # CLI wins
            (None, "env-tok", "env-tok"),  # env fallback
        ],
    )
    def test_token_priority(
        self, monkeypatch: pytest.MonkeyPatch, cli_token: str | None, env_token: str, expected: str
    ) -> None:
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", env_token)
        profile = Profile()
        cred = resolve_credential("corp", "jira", profile, cli_token=cli_token)
        assert cred.token == expected

    def test_no_token_raises_auth_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing token raises AuthError with a helpful hint."""
        monkeypatch.delenv("ATLS_CORP_JIRA_TOKEN", raising=False)
        monkeypatch.delenv("JIRA_PERSONAL_TOKEN", raising=False)
        profile = Profile()
        with pytest.raises(AuthError) as exc_info:
            resolve_credential("corp", "jira", profile)
        assert "ATLS_CORP_JIRA_TOKEN" in (exc_info.value.hint or "")


class TestResolveCredentialMethodPriority:
    """Priority matrix: CLI auth > env auth > profile config > default 'pat'."""

    @pytest.mark.parametrize(
        "cli_auth,env_auth,config_auth,expected",
        [
            ("basic", None, "pat", "basic"),  # CLI > env > config
            (None, "basic", "pat", "basic"),  # env > config
            (None, None, "basic", "basic"),  # config
            (None, None, "pat", "pat"),
            (None, None, None, "pat"),  # default
        ],
    )
    def test_method_priority(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_auth: str | None,
        env_auth: str | None,
        config_auth: str | None,
        expected: str,
    ) -> None:
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "some-token")
        monkeypatch.setenv("ATLS_CORP_JIRA_USER", "some-user")
        monkeypatch.delenv("ATLS_CORP_JIRA_AUTH", raising=False)
        if env_auth is not None:
            monkeypatch.setenv("ATLS_CORP_JIRA_AUTH", env_auth)

        from atlassian_skills.core.config import AuthConfig

        auth_cfg = AuthConfig(jira=config_auth) if config_auth else AuthConfig()  # type: ignore[arg-type]
        profile = Profile(auth=auth_cfg)

        cred = resolve_credential("corp", "jira", profile, cli_auth=cli_auth)
        assert cred.method == expected


class TestResolveCredentialBasicUsername:
    """Username resolution and validation for basic auth."""

    def test_basic_no_username_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """basic auth without any username → AuthError with USER hint."""
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "tok")
        monkeypatch.setenv("ATLS_CORP_JIRA_AUTH", "basic")
        monkeypatch.delenv("ATLS_CORP_JIRA_USER", raising=False)
        profile = Profile()
        with pytest.raises(AuthError) as exc_info:
            resolve_credential("corp", "jira", profile)
        assert "ATLS_CORP_JIRA_USER" in (exc_info.value.hint or "")

    def test_basic_cli_user_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cli_user takes precedence over env USER."""
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "tok")
        monkeypatch.setenv("ATLS_CORP_JIRA_AUTH", "basic")
        monkeypatch.setenv("ATLS_CORP_JIRA_USER", "env-user")
        profile = Profile()
        cred = resolve_credential("corp", "jira", profile, cli_user="cli-user")
        assert cred.username == "cli-user"

    def test_basic_env_user_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """env USER is used when no cli_user provided."""
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "tok")
        monkeypatch.setenv("ATLS_CORP_JIRA_AUTH", "basic")
        monkeypatch.setenv("ATLS_CORP_JIRA_USER", "env-user")
        profile = Profile()
        cred = resolve_credential("corp", "jira", profile)
        assert cred.username == "env-user"


class TestEnvVarNaming:
    """Verify env var key construction: ATLS_{PROFILE}_{PRODUCT}_*"""

    def test_env_var_naming_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_env_token reads ATLS_{PROFILE}_{PRODUCT}_TOKEN."""
        monkeypatch.setenv("ATLS_STAGING_CONFLUENCE_TOKEN", "staging-conf-tok")
        assert get_env_token("staging", "confluence") == "staging-conf-tok"

    def test_env_var_naming_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_env_user reads ATLS_{PROFILE}_{PRODUCT}_USER."""
        monkeypatch.setenv("ATLS_PROD_BAMBOO_USER", "bamboo-admin")
        assert get_env_user("prod", "bamboo") == "bamboo-admin"

    def test_env_var_naming_auth_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_env_auth_method reads ATLS_{PROFILE}_{PRODUCT}_AUTH."""
        monkeypatch.setenv("ATLS_DEV_BITBUCKET_AUTH", "basic")
        assert get_env_auth_method("dev", "bitbucket") == "basic"

    def test_env_var_absent_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Helper functions return None when the env var is not set."""
        monkeypatch.delenv("ATLS_GHOST_JIRA_TOKEN", raising=False)
        monkeypatch.delenv("ATLS_GHOST_JIRA_USER", raising=False)
        monkeypatch.delenv("ATLS_GHOST_JIRA_AUTH", raising=False)
        # Also remove legacy fallback vars so get_env_token truly returns None
        monkeypatch.delenv("JIRA_PERSONAL_TOKEN", raising=False)
        assert get_env_token("ghost", "jira") is None
        assert get_env_user("ghost", "jira") is None
        assert get_env_auth_method("ghost", "jira") is None

    def test_env_var_profile_case_insensitive_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Profile/product names are uppercased when building the key."""
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "upper-tok")
        # Pass lowercase — the helper must uppercase them
        assert get_env_token("corp", "jira") == "upper-tok"
