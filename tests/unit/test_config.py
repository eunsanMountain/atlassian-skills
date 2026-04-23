from __future__ import annotations

from pathlib import Path

import pytest

from atlassian_skills.core.config import (
    AuthConfig,
    Config,
    Profile,
    get_env_auth_method,
    get_env_token,
    get_env_user,
    get_profile,
    load_config,
    save_config,
)


class TestConfigDefaults:
    def test_auth_config_defaults(self) -> None:
        auth = AuthConfig()
        assert auth.jira == "pat"
        assert auth.confluence == "pat"
        assert auth.bitbucket == "pat"
        assert auth.zephyr == "pat"
        assert auth.bamboo == "basic"

    def test_profile_defaults(self) -> None:
        profile = Profile()
        assert profile.jira_url is None
        assert profile.confluence_url is None
        assert profile.bitbucket_url is None
        assert profile.zephyr_url is None
        assert profile.bamboo_url is None
        assert profile.storage == "env"
        assert isinstance(profile.auth, AuthConfig)

    def test_config_defaults(self) -> None:
        config = Config()
        assert config.default_profile == "default"
        assert config.profiles == {}


class TestLoadConfig:
    def test_missing_file_returns_empty_config(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.toml")
        assert isinstance(config, Config)
        assert config.default_profile == "default"
        assert config.profiles == {}

    def test_load_minimal_toml(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('default_profile = "work"\n')
        config = load_config(toml_file)
        assert config.default_profile == "work"

    def test_load_profile_from_toml(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "config.toml"
        toml_file.write_text('[profiles.corp]\njira_url = "https://jira.corp.example.com"\n')
        config = load_config(toml_file)
        assert "corp" in config.profiles
        assert config.profiles["corp"].jira_url == "https://jira.corp.example.com"


class TestSaveConfig:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        original = Config(
            default_profile="corp",
            profiles={
                "corp": Profile(
                    jira_url="https://jira.corp.example.com",
                    confluence_url="https://confluence.corp.example.com",
                    storage="keyring",
                )
            },
        )
        save_config(original, path)
        loaded = load_config(path)
        assert loaded.default_profile == "corp"
        assert loaded.profiles["corp"].jira_url == "https://jira.corp.example.com"
        assert loaded.profiles["corp"].confluence_url == "https://confluence.corp.example.com"
        assert loaded.profiles["corp"].storage == "keyring"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "config.toml"
        save_config(Config(), path)
        assert path.exists()

    def test_empty_config_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        save_config(Config(), path)
        loaded = load_config(path)
        assert loaded.default_profile == "default"
        assert loaded.profiles == {}


class TestGetProfile:
    def test_returns_named_profile(self) -> None:
        config = Config(
            profiles={
                "corp": Profile(jira_url="https://jira.corp.example.com"),
            }
        )
        profile = get_profile(config, "corp")
        assert profile.jira_url == "https://jira.corp.example.com"

    def test_returns_default_profile_when_name_is_none(self) -> None:
        config = Config(
            default_profile="corp",
            profiles={
                "corp": Profile(jira_url="https://jira.corp.example.com"),
            },
        )
        profile = get_profile(config)
        assert profile.jira_url == "https://jira.corp.example.com"

    def test_returns_empty_profile_for_missing_name(self) -> None:
        config = Config()
        profile = get_profile(config, "nonexistent")
        assert isinstance(profile, Profile)
        assert profile.jira_url is None

    def test_returns_empty_profile_when_default_not_found(self) -> None:
        config = Config(default_profile="missing")
        profile = get_profile(config)
        assert isinstance(profile, Profile)
        assert profile.jira_url is None


class TestEnvVars:
    def test_get_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "secret-pat")
        assert get_env_token("corp", "jira") == "secret-pat"

    def test_get_env_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_CORP_JIRA_TOKEN", raising=False)
        monkeypatch.delenv("JIRA_PERSONAL_TOKEN", raising=False)
        assert get_env_token("corp", "jira") is None

    def test_get_env_token_case_insensitive_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_CONFLUENCE_TOKEN", "conf-token")
        assert get_env_token("Corp", "Confluence") == "conf-token"

    def test_get_env_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_JIRA_USER", "jdoe")
        assert get_env_user("corp", "jira") == "jdoe"

    def test_get_env_user_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_CORP_JIRA_USER", raising=False)
        assert get_env_user("corp", "jira") is None

    def test_get_env_auth_method(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_BITBUCKET_AUTH", "basic")
        assert get_env_auth_method("corp", "bitbucket") == "basic"

    def test_get_env_auth_method_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_CORP_BITBUCKET_AUTH", raising=False)
        assert get_env_auth_method("corp", "bitbucket") is None

    def test_get_env_zephyr_token_and_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_ZEPHYR_TOKEN", "zephyr-token")
        monkeypatch.setenv("ATLS_CORP_ZEPHYR_AUTH", "pat")
        assert get_env_token("corp", "zephyr") == "zephyr-token"
        assert get_env_auth_method("corp", "zephyr") == "pat"


class TestLegacyEnvVarFallback:
    """Legacy mcp-atlassian env var compatibility (JIRA_PERSONAL_TOKEN, etc.)."""

    def test_jira_legacy_token_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_DEFAULT_JIRA_TOKEN", raising=False)
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "legacy-jira-pat")
        assert get_env_token("default", "jira") == "legacy-jira-pat"

    def test_confluence_legacy_token_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_DEFAULT_CONFLUENCE_TOKEN", raising=False)
        monkeypatch.setenv("CONFLUENCE_PERSONAL_TOKEN", "legacy-conf-pat")
        assert get_env_token("default", "confluence") == "legacy-conf-pat"

    def test_atls_var_takes_priority_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLS_CORP_JIRA_TOKEN", "new-pat")
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "legacy-pat")
        assert get_env_token("corp", "jira") == "new-pat"

    def test_bitbucket_legacy_token_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_DEFAULT_BITBUCKET_TOKEN", raising=False)
        monkeypatch.setenv("BITBUCKET_TOKEN", "legacy-bb-pat")
        assert get_env_token("default", "bitbucket") == "legacy-bb-pat"

    def test_legacy_fallback_case_insensitive_product(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_DEFAULT_JIRA_TOKEN", raising=False)
        monkeypatch.setenv("JIRA_PERSONAL_TOKEN", "legacy-pat")
        assert get_env_token("default", "Jira") == "legacy-pat"


class TestLoadConfigExpanded:
    def test_load_config_valid_toml(self, tmp_path: Path) -> None:
        """Write a valid config.toml to tmp, load it and verify fields."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_bytes(
            b'default_profile = "corp"\n'
            b"[profiles.corp]\n"
            b'jira_url = "https://jira.example.com"\n'
            b'confluence_url = "https://confluence.example.com"\n'
            b'storage = "keyring"\n'
        )
        config = load_config(toml_file)
        assert config.default_profile == "corp"
        assert "corp" in config.profiles
        assert config.profiles["corp"].jira_url == "https://jira.example.com"
        assert config.profiles["corp"].confluence_url == "https://confluence.example.com"
        assert config.profiles["corp"].storage == "keyring"

    def test_load_config_missing_file_returns_default(self, tmp_path: Path) -> None:
        """Nonexistent path returns a default Config with sensible defaults."""
        config = load_config(tmp_path / "does_not_exist.toml")
        assert isinstance(config, Config)
        assert config.default_profile == "default"
        assert config.profiles == {}

    def test_load_config_invalid_toml_raises(self, tmp_path: Path) -> None:
        """Malformed TOML should raise an error (not return silently)."""
        toml_file = tmp_path / "bad.toml"
        toml_file.write_bytes(b"default_profile = [unclosed\n")
        with pytest.raises((ValueError, OSError)):
            load_config(toml_file)

    def test_load_config_multiple_profiles(self, tmp_path: Path) -> None:
        """Config with 2+ profiles loads all profiles correctly."""
        toml_file = tmp_path / "config.toml"
        toml_file.write_bytes(
            b'default_profile = "work"\n'
            b"[profiles.work]\n"
            b'jira_url = "https://jira.work.com"\n'
            b"[profiles.personal]\n"
            b'jira_url = "https://jira.personal.com"\n'
            b'confluence_url = "https://confluence.personal.com"\n'
        )
        config = load_config(toml_file)
        assert "work" in config.profiles
        assert "personal" in config.profiles
        assert config.profiles["work"].jira_url == "https://jira.work.com"
        assert config.profiles["personal"].confluence_url == "https://confluence.personal.com"


class TestGetProfileExpanded:
    def test_get_profile_default_key(self) -> None:
        """No profile name specified → falls back to config.default_profile."""
        config = Config(
            default_profile="corp",
            profiles={"corp": Profile(jira_url="https://jira.corp.com")},
        )
        profile = get_profile(config)
        assert profile.jira_url == "https://jira.corp.com"

    def test_get_profile_cli_override(self) -> None:
        """Explicit name argument overrides the default_profile."""
        config = Config(
            default_profile="default",
            profiles={
                "default": Profile(jira_url="https://jira.default.com"),
                "staging": Profile(jira_url="https://jira.staging.com"),
            },
        )
        profile = get_profile(config, "staging")
        assert profile.jira_url == "https://jira.staging.com"


class TestConfigModelFields:
    def test_config_model_defaults(self) -> None:
        """Config() has sensible defaults without any arguments."""
        config = Config()
        assert config.default_profile == "default"
        assert isinstance(config.profiles, dict)
        assert len(config.profiles) == 0

    def test_profile_model_fields(self) -> None:
        """Profile exposes all expected URL and auth fields."""
        profile = Profile()
        assert hasattr(profile, "jira_url")
        assert hasattr(profile, "confluence_url")
        assert hasattr(profile, "bitbucket_url")
        assert hasattr(profile, "bamboo_url")
        assert hasattr(profile, "auth")
        assert hasattr(profile, "storage")
        assert hasattr(profile, "ca_bundle")
        assert profile.ca_bundle is None

    def test_profile_storage_field_accepts_env(self) -> None:
        profile = Profile(storage="env")
        assert profile.storage == "env"

    def test_profile_storage_field_accepts_keyring(self) -> None:
        profile = Profile(storage="keyring")
        assert profile.storage == "keyring"

    def test_profile_storage_field_accepts_plaintext(self) -> None:
        profile = Profile(storage="plaintext")
        assert profile.storage == "plaintext"

    def test_auth_config_nested_in_profile(self) -> None:
        """Profile.auth is an AuthConfig with correct defaults."""
        profile = Profile()
        assert isinstance(profile.auth, AuthConfig)
        assert profile.auth.jira == "pat"
        assert profile.auth.bamboo == "basic"


class TestEnvVarPatterns:
    def test_get_env_token_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATLS_{PROFILE}_{PRODUCT}_TOKEN pattern is respected."""
        monkeypatch.setenv("ATLS_STAGING_CONFLUENCE_TOKEN", "staging-token")
        assert get_env_token("staging", "confluence") == "staging-token"

    def test_get_env_user_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATLS_{PROFILE}_{PRODUCT}_USER pattern is respected."""
        monkeypatch.setenv("ATLS_PROD_JIRA_USER", "produser")
        assert get_env_user("prod", "jira") == "produser"

    def test_get_env_auth_method_pattern(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATLS_{PROFILE}_{PRODUCT}_AUTH pattern is respected."""
        monkeypatch.setenv("ATLS_CORP_BAMBOO_AUTH", "basic")
        assert get_env_auth_method("corp", "bamboo") == "basic"

    def test_get_env_base_url_pattern_naming(self) -> None:
        """URL env var naming convention: ATLS_{PROFILE}_{PRODUCT}_URL.

        Documents the expected naming pattern even though no dedicated
        get_env_base_url helper exists yet — callers use os.environ directly.
        """

        profile = "corp"
        product = "jira"
        expected_key = f"ATLS_{profile.upper()}_{product.upper()}_URL"
        assert expected_key == "ATLS_CORP_JIRA_URL"

    def test_get_env_token_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_STAGING_BAMBOO_TOKEN", raising=False)
        assert get_env_token("staging", "bamboo") is None

    def test_get_env_user_missing_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLS_STAGING_BAMBOO_USER", raising=False)
        assert get_env_user("staging", "bamboo") is None


class TestSaveConfigExpanded:
    def test_config_set_creates_key(self, tmp_path: Path) -> None:
        """save_config creates a new profile key when it did not exist before."""
        path = tmp_path / "config.toml"
        config = Config(profiles={"corp": Profile(jira_url="https://jira.corp.com")})
        save_config(config, path)
        loaded = load_config(path)
        assert "corp" in loaded.profiles
        assert loaded.profiles["corp"].jira_url == "https://jira.corp.com"

    def test_config_set_nested_key(self, tmp_path: Path) -> None:
        """save_config persists nested auth config correctly."""
        path = tmp_path / "config.toml"
        from atlassian_skills.core.config import AuthConfig

        config = Config(
            profiles={
                "corp": Profile(
                    jira_url="https://jira.corp.com",
                    auth=AuthConfig(jira="basic", confluence="basic"),
                )
            }
        )
        save_config(config, path)
        loaded = load_config(path)
        assert loaded.profiles["corp"].auth.jira == "basic"
        assert loaded.profiles["corp"].auth.confluence == "basic"
