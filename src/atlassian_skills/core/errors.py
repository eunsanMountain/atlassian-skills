from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    GENERIC = 1
    NOT_FOUND = 2
    PERMISSION = 3
    CONFLICT = 4
    STALE = 5
    AUTH = 6
    VALIDATION = 7
    NETWORK = 10
    RATE_LIMITED = 11


class AtlasError(Exception):
    """Base error for all atlassian-skills errors."""

    code: str = "ATLAS_ERROR"
    exit_code: int = ExitCode.GENERIC

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        http_status: int | None = None,
        http_url: str | None = None,
        http_method: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.http_status = http_status
        self.http_url = http_url
        self.http_method = http_method
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "error": {
                "code": self.code,
                "exit_code": self.exit_code,
                "message": self.message,
            }
        }
        if self.hint is not None:
            d["error"]["hint"] = self.hint
        if self.http_status is not None:
            d["error"]["http_status"] = self.http_status
        if self.http_url is not None:
            d["error"]["http_url"] = self.http_url
        if self.http_method is not None:
            d["error"]["http_method"] = self.http_method
        if self.context:
            d["error"]["context"] = self.context
        return d


class NotFoundError(AtlasError):
    code = "NOT_FOUND"
    exit_code = ExitCode.NOT_FOUND


class ForbiddenError(AtlasError):
    code = "PERMISSION"
    exit_code = ExitCode.PERMISSION


class ConflictError(AtlasError):
    code = "CONFLICT"
    exit_code = ExitCode.CONFLICT


class StaleError(AtlasError):
    code = "STALE"
    exit_code = ExitCode.STALE


class AuthError(AtlasError):
    code = "AUTH"
    exit_code = ExitCode.AUTH


class ValidationError(AtlasError):
    code = "VALIDATION"
    exit_code = ExitCode.VALIDATION


class NetworkError(AtlasError):
    code = "NETWORK"
    exit_code = ExitCode.NETWORK


class RateLimitError(AtlasError):
    code = "RATE_LIMITED"
    exit_code = ExitCode.RATE_LIMITED


def _safe_server_message(body: Any, max_len: int = 500) -> str:
    """Truncate and sanitize server error body to prevent prompt injection."""
    if body is None:
        return ""
    if isinstance(body, dict):
        msg = body.get("message", "")
        if not msg:
            error_messages = body.get("errorMessages", [])
            msg = error_messages[0] if error_messages else ""
        if not msg:
            errors = body.get("errors", {})
            if isinstance(errors, list):
                msg = "; ".join(e.get("message", str(e)) for e in errors if isinstance(e, dict))
            elif isinstance(errors, dict) and errors:
                msg = "; ".join(f"{k}: {v}" for k, v in errors.items())
        text = str(msg)
    else:
        text = str(body)
    # Strip control characters and truncate
    text = text.replace("\n", " ").replace("\r", "").strip()
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def http_error_to_atlas(
    status: int,
    url: str,
    method: str,
    body: Any = None,
) -> AtlasError:
    """Map HTTP status code to an appropriate AtlasError subclass."""
    import json as _json

    kwargs: dict[str, Any] = {
        "http_status": status,
        "http_url": url,
        "http_method": method,
    }

    # Parse body into dict if it's a JSON string
    body_dict: dict[str, Any] | None = None
    if isinstance(body, dict):
        body_dict = body
    elif isinstance(body, str):
        try:
            parsed = _json.loads(body)
            if isinstance(parsed, dict):
                body_dict = parsed
        except Exception:
            pass

    safe_msg = _safe_server_message(body_dict or body)

    if status == 400:
        return ValidationError(safe_msg or "Bad request", **kwargs)
    if status == 401:
        return AuthError(safe_msg or "Unauthorized", **kwargs)
    if status == 403:
        return ForbiddenError(safe_msg or "Forbidden", **kwargs)
    if status == 404:
        return NotFoundError(safe_msg or "Not found", **kwargs)
    if status == 409:
        context: dict[str, Any] = {}
        if body_dict:
            server_msg = _safe_server_message(body_dict)
            if server_msg:
                context["server_message"] = server_msg
        return ConflictError(
            f"Conflict: {url}",
            hint="Use --if-version to check current version before updating",
            context=context or None,
            **kwargs,
        )
    if status == 429:
        return RateLimitError(
            f"Rate limited: {url}",
            hint="Retry after the indicated delay",
            **kwargs,
        )
    if 500 <= status < 600:
        return NetworkError(safe_msg or f"Server error {status}", **kwargs)
    return AtlasError(safe_msg or f"HTTP {status}", **kwargs)
