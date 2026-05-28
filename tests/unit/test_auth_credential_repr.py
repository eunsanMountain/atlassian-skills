"""Credential repr/str must redact the raw token (token-exposure guard #2)."""

from __future__ import annotations

from atlassian_skills.core.auth import Credential


class TestCredentialRepr:
    def test_repr_redacts_token(self) -> None:
        cred = Credential(method="pat", token="SECRET_TOKEN_VALUE_123")
        text = repr(cred)
        assert "SECRET_TOKEN_VALUE_123" not in text
        assert "***redacted***" in text
        assert "method='pat'" in text

    def test_str_redacts_token(self) -> None:
        cred = Credential(method="basic", token="SECRET_BASIC_PASSWORD", username="alice")
        text = str(cred)
        assert "SECRET_BASIC_PASSWORD" not in text
        assert "***redacted***" in text
        assert "alice" in text  # username intentionally visible

    def test_to_header_still_uses_real_token(self) -> None:
        """Redaction must NOT affect actual auth header generation."""
        cred = Credential(method="pat", token="real-pat-token")
        headers = cred.to_header()
        assert headers["Authorization"] == "Bearer real-pat-token"
