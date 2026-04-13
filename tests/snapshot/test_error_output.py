from __future__ import annotations

import json

from syrupy.assertion import SnapshotAssertion

from atlassian_skills.core.errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

BASE_URL = "https://jira.example.com/rest/api/2/issue/PROJ-1"


def test_not_found_error_json_snapshot(snapshot: SnapshotAssertion) -> None:
    """NotFoundError.to_dict() JSON output matches snapshot."""
    err = NotFoundError(
        "Issue PROJ-1 not found",
        http_status=404,
        http_url=BASE_URL,
        http_method="GET",
    )
    assert json.dumps(err.to_dict(), indent=2) == snapshot


def test_auth_error_json_snapshot(snapshot: SnapshotAssertion) -> None:
    """AuthError.to_dict() JSON output matches snapshot."""
    err = AuthError(
        "Unauthorized",
        hint="Check your personal access token",
        http_status=401,
        http_url=BASE_URL,
        http_method="GET",
    )
    assert json.dumps(err.to_dict(), indent=2) == snapshot


def test_validation_error_json_snapshot(snapshot: SnapshotAssertion) -> None:
    """ValidationError.to_dict() JSON output matches snapshot."""
    err = ValidationError(
        "Bad request: field 'summary' is required",
        http_status=400,
        http_url=BASE_URL,
        http_method="POST",
        context={"field": "summary"},
    )
    assert json.dumps(err.to_dict(), indent=2) == snapshot


def test_conflict_error_json_snapshot(snapshot: SnapshotAssertion) -> None:
    """ConflictError.to_dict() JSON output matches snapshot."""
    err = ConflictError(
        f"Conflict: {BASE_URL}",
        hint="Use --if-version to check current version before updating",
        http_status=409,
        http_url=BASE_URL,
        http_method="PUT",
        context={"server_message": "Version mismatch"},
    )
    assert json.dumps(err.to_dict(), indent=2) == snapshot


def test_rate_limit_error_json_snapshot(snapshot: SnapshotAssertion) -> None:
    """RateLimitError.to_dict() JSON output matches snapshot."""
    err = RateLimitError(
        f"Rate limited: {BASE_URL}",
        hint="Retry after the indicated delay",
        http_status=429,
        http_url=BASE_URL,
        http_method="GET",
    )
    assert json.dumps(err.to_dict(), indent=2) == snapshot
