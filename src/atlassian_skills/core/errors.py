from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


class InternalError(AtlasError):
    """Redacted emergency error emitted only by the console entrypoint boundary."""

    code = "INTERNAL_ERROR"
    exit_code = ExitCode.GENERIC


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


class MigrationConsentRequiredError(ValidationError):
    code = "MIGRATION_CONSENT_REQUIRED"


class ConversionConsentRequiredError(ValidationError):
    code = "CONVERSION_CONSENT_REQUIRED"


def consent_retry_action(
    argv: tuple[str, ...],
    *,
    option: str,
    fingerprint: str,
    description_code: str,
) -> dict[str, Any]:
    """Build the one approval-gated retry action allowed by consent errors."""

    return {
        "id": "retry_with_consent",
        "requires_user_approval": True,
        "description_code": description_code,
        "argv": [*argv, option, fingerprint],
    }


class NetworkError(AtlasError):
    code = "NETWORK"
    exit_code = ExitCode.NETWORK


class RateLimitError(AtlasError):
    code = "RATE_LIMITED"
    exit_code = ExitCode.RATE_LIMITED


class RedirectError(AtlasError):
    """An unexpected 3xx that atls did not follow.

    Keeps ``ExitCode.GENERIC`` on purpose: before this class existed a 3xx fell
    through to the base ``AtlasError`` and exited 1, and agents branch on exit
    codes. Only the message, ``code`` and ``context`` get richer.
    """

    code = "REDIRECT"
    exit_code = ExitCode.GENERIC


@dataclass(frozen=True)
class RedirectInfo:
    """Structured redirect facts handed to :func:`http_error_to_atlas`.

    Deliberately not the raw header mapping: only the few fields needed for
    diagnosis cross the boundary, so a server-controlled header set can never be
    dumped wholesale into an error an agent reads.
    """

    reason: str
    location: str | None = None
    redirects: int | None = None


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_DISPLAY_URL_LEN = 300
MAX_HEADER_VALUE_LEN = 256


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[:max_len] + "..."


def safe_display_url(url: str | None) -> str:
    """Return a URL that is safe to print in an error line or a verbose log.

    Strips userinfo, query and fragment. All three can carry a credential: an
    expired session redirects to ``/login.action?os_destination=…``, and some
    proxies bounce through URLs with a token in the query. Both error output and
    verbose logs end up in agent transcripts and in bug reports users paste, so
    the same scrubbing applies to every display path (that is why callers share
    this one function instead of each rolling their own).
    """
    if not url:
        return ""
    cleaned = _CONTROL_RE.sub("", url).strip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        # Malformed authority (bad port, stray brackets): keep the scheme-less
        # remainder rather than leaking the original string unfiltered.
        return _truncate(cleaned.split("?", 1)[0].split("#", 1)[0], MAX_DISPLAY_URL_LEN)
    if parts.scheme or parts.netloc:
        if ":" in host:  # IPv6 literal — urlsplit strips the brackets
            host = f"[{host}]"
        netloc = f"{host}:{port}" if port is not None else host
        cleaned = urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    else:
        cleaned = parts.path
    return _truncate(cleaned, MAX_DISPLAY_URL_LEN)


def safe_header_value(value: str | None, max_len: int = MAX_HEADER_VALUE_LEN) -> str:
    """Strip control characters from a server-supplied header and cap its length.

    Server headers reach an LLM agent through error messages and verbose logs;
    this is the header-side counterpart of :func:`_safe_server_message`.
    """
    if not value:
        return ""
    return _truncate(_CONTROL_RE.sub("", value).strip(), max_len)


def request_context_line(err: AtlasError) -> str | None:
    """Render the ``Request: GET https://… -> 302`` line, or None if unknown.

    Without this, the human-readable error shows only the message while the JSON
    envelope carries ``http_url`` — the asymmetry that made GitHub #19 hard to
    diagnose ("by using --format json, I got more of a clue").
    """
    if err.http_url is None and err.http_status is None and err.http_method is None:
        return None
    method = err.http_method or "?"
    url = safe_display_url(err.http_url) or "(unknown url)"
    status = f" -> {err.http_status}" if err.http_status is not None else ""
    return f"Request: {method} {url}{status}"


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


# Delivered at failure time rather than in SKILL.md: the skill file has a hard
# 2000-token budget with ~4 tokens of headroom, and a hint costs nothing until
# something actually breaks.
PROXY_HINT = (
    "If that target is not your Atlassian host, a proxy is probably intercepting the request. "
    "Check HTTPS_PROXY/NO_PROXY — NO_PROXY entries must be written as '.example.com' or "
    "'example.com', never '*.example.com'. Run `atls doctor --check-auth` for a classified diagnosis."
)
CONNECTION_HINT = (
    "Run `atls doctor --check-auth` to tell a bad token apart from a proxy, a TLS trust "
    "problem, or an unreachable host. For a private CA, set ca_bundle in the profile or "
    "SSL_CERT_FILE to a PEM bundle."
)


def http_error_to_atlas(
    status: int,
    url: str,
    method: str,
    body: Any = None,
    *,
    redirect: RedirectInfo | None = None,
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
    if 300 <= status < 400:
        # Before this branch a 3xx fell through to the generic fallthrough below
        # and surfaced as a bare "HTTP 302" with the Location header discarded.
        redirect_context: dict[str, Any] = {}
        location = ""
        if redirect is not None:
            redirect_context["reason"] = redirect.reason
            location = safe_display_url(redirect.location)
            if location:
                redirect_context["location"] = location
            if redirect.redirects is not None:
                redirect_context["redirects"] = redirect.redirects
        if location:
            message = f"Server returned {status} redirect to {location}"
        elif redirect is not None:
            message = f"Server returned {status} with no usable Location header"
        else:
            message = f"Server returned {status} redirect"
        return RedirectError(message, hint=PROXY_HINT, context=redirect_context or None, **kwargs)
    if 500 <= status < 600:
        return NetworkError(safe_msg or f"Server error {status}", **kwargs)
    return AtlasError(safe_msg or f"HTTP {status}", **kwargs)
