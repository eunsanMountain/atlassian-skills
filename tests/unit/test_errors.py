from __future__ import annotations

import pytest

from atlassian_skills.core.errors import (
    AtlasError,
    AuthError,
    ConflictError,
    ExitCode,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    StaleError,
    ValidationError,
    http_error_to_atlas,
)


class TestExitCode:
    def test_values(self) -> None:
        assert ExitCode.OK == 0
        assert ExitCode.GENERIC == 1
        assert ExitCode.NOT_FOUND == 2
        assert ExitCode.PERMISSION == 3
        assert ExitCode.CONFLICT == 4
        assert ExitCode.STALE == 5
        assert ExitCode.AUTH == 6
        assert ExitCode.VALIDATION == 7
        assert ExitCode.NETWORK == 10
        assert ExitCode.RATE_LIMITED == 11


class TestAtlasErrorHierarchy:
    @pytest.mark.parametrize(
        "cls, expected_exit_code",
        [
            (NotFoundError, ExitCode.NOT_FOUND),
            (ForbiddenError, ExitCode.PERMISSION),
            (ConflictError, ExitCode.CONFLICT),
            (StaleError, ExitCode.STALE),
            (AuthError, ExitCode.AUTH),
            (ValidationError, ExitCode.VALIDATION),
            (NetworkError, ExitCode.NETWORK),
            (RateLimitError, ExitCode.RATE_LIMITED),
        ],
    )
    def test_exit_code(self, cls: type[AtlasError], expected_exit_code: ExitCode) -> None:
        err = cls("test message")
        assert err.exit_code == expected_exit_code

    def test_base_exit_code(self) -> None:
        err = AtlasError("generic error")
        assert err.exit_code == ExitCode.GENERIC

    def test_is_exception(self) -> None:
        err = NotFoundError("not found")
        assert isinstance(err, Exception)
        assert isinstance(err, AtlasError)


class TestAtlasErrorToDict:
    def test_minimal(self) -> None:
        err = AtlasError("something broke")
        d = err.to_dict()
        assert d["error"]["code"] == "ATLAS_ERROR"
        assert d["error"]["exit_code"] == ExitCode.GENERIC
        assert d["error"]["message"] == "something broke"
        assert "hint" not in d["error"]
        assert "http_status" not in d["error"]

    def test_full_fields(self) -> None:
        err = NotFoundError(
            "issue not found",
            hint="check the issue key",
            http_status=404,
            http_url="https://jira.example.com/rest/api/2/issue/PROJ-1",
            http_method="GET",
            context={"issue_key": "PROJ-1"},
        )
        d = err.to_dict()
        inner = d["error"]
        assert inner["code"] == "NOT_FOUND"
        assert inner["exit_code"] == ExitCode.NOT_FOUND
        assert inner["hint"] == "check the issue key"
        assert inner["http_status"] == 404
        assert inner["http_url"] == "https://jira.example.com/rest/api/2/issue/PROJ-1"
        assert inner["http_method"] == "GET"
        assert inner["context"] == {"issue_key": "PROJ-1"}

    def test_json_serializable(self) -> None:
        import json

        err = AuthError("unauthorized", hint="check your PAT", http_status=401)
        d = err.to_dict()
        # Should not raise
        json.dumps(d)


class TestHttpErrorToAtlas:
    @pytest.mark.parametrize(
        "status, expected_cls",
        [
            (400, ValidationError),
            (401, AuthError),
            (403, ForbiddenError),
            (404, NotFoundError),
            (409, ConflictError),
            (429, RateLimitError),
            (500, NetworkError),
            (502, NetworkError),
            (503, NetworkError),
        ],
    )
    def test_known_statuses(self, status: int, expected_cls: type[AtlasError]) -> None:
        err = http_error_to_atlas(status, "https://example.com/api", "GET")
        assert isinstance(err, expected_cls)
        assert err.http_status == status
        assert err.http_url == "https://example.com/api"
        assert err.http_method == "GET"

    def test_unknown_status_fallback(self) -> None:
        err = http_error_to_atlas(418, "https://example.com/api", "POST")
        assert type(err) is AtlasError
        assert err.http_status == 418

    def test_body_used_as_message(self) -> None:
        err = http_error_to_atlas(404, "https://example.com/api", "GET", body="Issue does not exist")
        assert err.message == "Issue does not exist"

    def test_default_message_when_no_body(self) -> None:
        err = http_error_to_atlas(404, "https://example.com/api", "GET")
        assert "not found" in err.message.lower()

    def test_body_json_with_error_message_field(self) -> None:
        import json

        body = json.dumps({"errorMessages": ["Issue not found"], "errors": {}})
        err = http_error_to_atlas(404, "https://example.com/api", "GET", body=body)
        # body string is used as the message
        assert isinstance(err, NotFoundError)
        assert err.http_status == 404

    def test_rate_limit_error_retry_after_context(self) -> None:
        err = http_error_to_atlas(429, "https://example.com/api", "GET")
        assert isinstance(err, RateLimitError)
        assert err.hint is not None
        assert "retry" in err.hint.lower()


# ---------------------------------------------------------------------------
# Exception chaining
# ---------------------------------------------------------------------------


class TestExceptionChaining:
    def test_raise_from_preserves_cause(self) -> None:
        original = ValueError("original error")

        with pytest.raises(NotFoundError) as exc_info:
            try:
                raise original
            except ValueError as e:
                raise NotFoundError("wrapped") from e

        assert exc_info.value.__cause__ is original

    def test_base_class_catches_subclass(self) -> None:
        err = NotFoundError("missing issue")

        with pytest.raises(AtlasError):
            raise err

    def test_base_class_catches_auth_error(self) -> None:
        err = AuthError("bad token")

        with pytest.raises(AtlasError):
            raise err

    def test_chained_context_suppression(self) -> None:
        original = RuntimeError("root cause")

        with pytest.raises(NotFoundError) as exc_info:
            try:
                raise original
            except RuntimeError:
                raise NotFoundError("not found") from None

        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is original


# ---------------------------------------------------------------------------
# to_dict — all optional fields None
# ---------------------------------------------------------------------------


class TestToDictAllNone:
    def test_all_optional_fields_none(self) -> None:
        err = AtlasError("bare error")
        d = err.to_dict()
        inner = d["error"]
        assert "hint" not in inner
        assert "http_status" not in inner
        assert "http_url" not in inner
        assert "http_method" not in inner
        assert "context" not in inner

    def test_partial_optional_fields(self) -> None:
        err = NotFoundError("missing", http_status=404)
        d = err.to_dict()
        inner = d["error"]
        assert inner["http_status"] == 404
        assert "http_url" not in inner
        assert "hint" not in inner


# ---------------------------------------------------------------------------
# http_error_to_atlas — full status mapping matrix (including 504 and unknown)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_cls",
    [
        (400, ValidationError),
        (401, AuthError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
        (500, NetworkError),
        (502, NetworkError),
        (503, NetworkError),
        (504, NetworkError),
        (418, AtlasError),  # unknown → base class
    ],
)
def test_http_error_to_atlas_status_mapping(status: int, expected_cls: type[AtlasError]) -> None:
    err = http_error_to_atlas(status, "https://example.com/api", "GET")
    assert isinstance(err, expected_cls)
    assert err.http_status == status
    assert err.http_url == "https://example.com/api"
    assert err.http_method == "GET"


# ---------------------------------------------------------------------------
# _safe_server_message edge cases
# ---------------------------------------------------------------------------

from atlassian_skills.core.errors import _safe_server_message  # noqa: E402


class TestSafeServerMessage:
    def test_none_returns_empty_string(self) -> None:
        assert _safe_server_message(None) == ""

    def test_dict_with_message_key(self) -> None:
        result = _safe_server_message({"message": "Something went wrong"})
        assert result == "Something went wrong"

    def test_dict_with_error_messages(self) -> None:
        result = _safe_server_message({"errorMessages": ["First error", "Second error"], "errors": {}})
        assert result == "First error"

    def test_empty_error_messages_list_returns_empty(self) -> None:
        result = _safe_server_message({"errorMessages": [], "errors": {}})
        assert result == ""

    def test_truncation_at_500_chars(self) -> None:
        long_body = "x" * 600
        result = _safe_server_message(long_body)
        assert result == "x" * 500 + "..."
        assert len(result) == 503

    def test_control_chars_stripped(self) -> None:
        result = _safe_server_message("line1\nline2\rline3")
        assert "\n" not in result
        assert "\r" not in result
        assert "line1" in result
        assert "line2" in result

    def test_plain_string_returned_as_is(self) -> None:
        result = _safe_server_message("plain error message")
        assert result == "plain error message"


# ---------------------------------------------------------------------------
# ExitCode — verify each error subclass has the correct exit_code
# ---------------------------------------------------------------------------


class TestExitCodesMatchErrorClasses:
    @pytest.mark.parametrize(
        "cls,expected_exit_code",
        [
            (NotFoundError, ExitCode.NOT_FOUND),
            (ForbiddenError, ExitCode.PERMISSION),
            (ConflictError, ExitCode.CONFLICT),
            (StaleError, ExitCode.STALE),
            (AuthError, ExitCode.AUTH),
            (ValidationError, ExitCode.VALIDATION),
            (NetworkError, ExitCode.NETWORK),
            (RateLimitError, ExitCode.RATE_LIMITED),
        ],
    )
    def test_exit_codes_match_error_classes(self, cls: type[AtlasError], expected_exit_code: ExitCode) -> None:
        err = cls("test")
        assert err.exit_code == expected_exit_code


# ---------------------------------------------------------------------------
# AtlasError.to_dict — minimal and full-field variants
# ---------------------------------------------------------------------------


class TestAtlasToDictVariants:
    def test_atlas_error_to_dict_minimal(self) -> None:
        err = AtlasError("only message")
        d = err.to_dict()
        inner = d["error"]
        assert inner["message"] == "only message"
        assert "hint" not in inner
        assert "http_status" not in inner
        assert "http_url" not in inner
        assert "http_method" not in inner
        assert "context" not in inner

    def test_atlas_error_to_dict_with_all_fields(self) -> None:
        err = AtlasError(
            "full error",
            hint="try this",
            http_status=500,
            http_url="https://example.com/api",
            http_method="POST",
            context={"key": "value"},
        )
        d = err.to_dict()
        inner = d["error"]
        assert inner["message"] == "full error"
        assert inner["hint"] == "try this"
        assert inner["http_status"] == 500
        assert inner["http_url"] == "https://example.com/api"
        assert inner["http_method"] == "POST"
        assert inner["context"] == {"key": "value"}


# ---------------------------------------------------------------------------
# http_error_to_atlas — body parsing variants
# ---------------------------------------------------------------------------


class TestHttpErrorBodyParsing:
    def test_json_string_body_gets_parsed(self) -> None:
        import json

        body = json.dumps({"message": "Issue does not exist"})
        err = http_error_to_atlas(404, "https://example.com/api", "GET", body=body)
        assert err.message == "Issue does not exist"

    def test_dict_body_used_directly(self) -> None:
        err = http_error_to_atlas(404, "https://example.com/api", "GET", body={"message": "Not found via dict"})
        assert err.message == "Not found via dict"

    def test_invalid_json_string_body_used_as_plain_text(self) -> None:
        err = http_error_to_atlas(500, "https://example.com/api", "GET", body="not valid json {")
        assert "not valid json {" in err.message

    def test_409_includes_if_version_hint(self) -> None:
        err = http_error_to_atlas(409, "https://example.com/api", "PUT")
        assert isinstance(err, ConflictError)
        assert err.hint is not None
        assert "--if-version" in err.hint

    def test_429_includes_retry_after_hint(self) -> None:
        err = http_error_to_atlas(429, "https://example.com/api", "GET")
        assert isinstance(err, RateLimitError)
        assert err.hint is not None
        assert "Retry after" in err.hint
