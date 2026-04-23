from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

import typer
from pydantic import BaseModel

from atlassian_skills.core.auth import resolve_credential
from atlassian_skills.core.config import get_profile, load_config
from atlassian_skills.core.dryrun import format_dry_run
from atlassian_skills.core.errors import AtlasError, ValidationError
from atlassian_skills.core.format import OutputFormat, format_output
from atlassian_skills.core.models import WriteResult
from atlassian_skills.zephyr.client import ZephyrClient
from atlassian_skills.zephyr.models import TestStepRequest

zephyr_app = typer.Typer(help="Zephyr Scale Server/DC commands", no_args_is_help=True)

testcase_app = typer.Typer(help="Test case commands", no_args_is_help=True)
testplan_app = typer.Typer(help="Test plan commands", no_args_is_help=True)
testrun_app = typer.Typer(help="Test run commands", no_args_is_help=True)
testresult_app = typer.Typer(help="Test result commands", no_args_is_help=True)
environment_app = typer.Typer(help="Environment commands", no_args_is_help=True)
issuelink_app = typer.Typer(help="Issue-link commands", no_args_is_help=True)

zephyr_app.add_typer(testcase_app, name="testcase")
zephyr_app.add_typer(testplan_app, name="testplan")
zephyr_app.add_typer(testrun_app, name="testrun")
zephyr_app.add_typer(testresult_app, name="testresult")
zephyr_app.add_typer(environment_app, name="environment")
zephyr_app.add_typer(issuelink_app, name="issuelink")


def _make_client(ctx_obj: dict[str, Any]) -> ZephyrClient:
    profile_name: str = ctx_obj.get("profile", "default")
    timeout: float = ctx_obj.get("timeout", 30.0)
    config = load_config()
    profile = get_profile(config, profile_name)
    url = profile.zephyr_url or os.environ.get(f"ATLS_{profile_name.upper()}_ZEPHYR_URL")
    if not url:
        typer.echo(
            f"No Zephyr URL for profile '{profile_name}'. "
            f"Set zephyr_url in config or ATLS_{profile_name.upper()}_ZEPHYR_URL env var.",
            err=True,
        )
        raise typer.Exit(1)
    credential = resolve_credential(profile_name, "zephyr", profile)
    verify: str | bool = profile.ca_bundle if profile.ca_bundle else True
    return ZephyrClient(url.rstrip("/"), credential, timeout=timeout, verify=verify)


def _fmt(ctx_obj: dict[str, Any]) -> OutputFormat:
    fmt = ctx_obj.get("format", OutputFormat.COMPACT)
    return OutputFormat(fmt) if not isinstance(fmt, OutputFormat) else fmt


def _resolve_fmt(ctx_obj: dict[str, Any], local_format: str | None) -> OutputFormat:
    if local_format:
        try:
            return OutputFormat(local_format)
        except ValueError:
            valid = ", ".join(f.value for f in OutputFormat)
            typer.echo(f"Error: Invalid format '{local_format}'. Valid: {valid}", err=True)
            raise typer.Exit(1) from None
    return _fmt(ctx_obj)


def _handle_error(err: AtlasError, fmt: OutputFormat) -> None:
    if fmt == OutputFormat.JSON:
        typer.echo(json.dumps(err.to_dict()))
    else:
        typer.echo(f"Error: {err.message}", err=True)
        if err.hint:
            typer.echo(f"Hint:  {err.hint}", err=True)
    raise typer.Exit(err.exit_code)


def _parse_json_object(value: str, label: str = "--data-json") -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{label} must be a JSON object")
    return data


def _parse_json_array(value: str, label: str = "--data-json") -> list[dict[str, Any]]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid {label}: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValidationError(f"{label} must be a JSON array of objects")
    return data


def _render_model(model: BaseModel, fmt: OutputFormat) -> str:
    if fmt == OutputFormat.COMPACT and hasattr(model, "to_compact_dict"):
        return format_output(model.to_compact_dict(), fmt)
    return format_output(model.model_dump(), fmt)


def _render_models(models: Sequence[BaseModel], fmt: OutputFormat) -> str:
    if fmt == OutputFormat.COMPACT:
        return format_output([m.to_compact_dict() if hasattr(m, "to_compact_dict") else m.model_dump() for m in models], fmt)
    return format_output([m.model_dump() for m in models], fmt)


def _write_result(action: str, key: str, fmt: OutputFormat, *, summary: str | None = None, id: str | None = None) -> None:
    typer.echo(format_output(WriteResult(action=action, key=key, summary=summary, id=id), fmt))


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@testcase_app.command("get")
def testcase_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test case key, e.g. JQA-T1234"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(_render_model(_make_client(ctx.obj).get_testcase(key, fields=fields), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("search")
def testcase_search(
    ctx: typer.Context,
    query: str | None = typer.Option(None, "--query", help="TQL query"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    start_at: int = typer.Option(0, "--start-at", help="Pagination offset"),
    max_results: int = typer.Option(200, "--max-results", help="Maximum results"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        items = _make_client(ctx.obj).search_testcases(
            query=query, fields=fields, start_at=start_at, max_results=max_results
        )
        typer.echo(_render_models(items, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("create")
def testcase_create(
    ctx: typer.Context,
    data_json: str = typer.Option(..., "--data-json", help="Test case payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testcase"
        if dry_run:
            typer.echo(format_dry_run("POST", url, body=data, fmt=fmt.value))
            return
        key = client.create_testcase(data)
        _write_result("created", key, fmt, summary=str(data.get("name", "")) or None)
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("update")
def testcase_update(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test case key"),
    data_json: str = typer.Option(..., "--data-json", help="Update payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testcase/{key}"
        if dry_run:
            typer.echo(format_dry_run("PUT", url, body=data, fmt=fmt.value))
            return
        client.update_testcase(key, data)
        _write_result("updated", key, fmt)
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("delete")
def testcase_delete(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test case key"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testcase/{key}"
        if dry_run:
            typer.echo(format_dry_run("DELETE", url, fmt=fmt.value))
            return
        client.delete_testcase(key)
        _write_result("deleted", key, fmt)
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("latest-result")
def testcase_latest_result(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test case key"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        result = _make_client(ctx.obj).get_testcase_latest_result(key)
        typer.echo(format_output(None if result is None else result.to_compact_dict(), fmt) if fmt == OutputFormat.COMPACT else format_output(None if result is None else result.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("steps")
def testcase_steps(
    ctx: typer.Context,
    issue_id: str = typer.Argument(..., help="Test case key"),
    project_id: str = typer.Argument("", help="Project ID kept for MCP parity"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        steps = _make_client(ctx.obj).get_test_steps(issue_id, project_id)
        typer.echo(format_output(steps.to_compact_dict() if fmt == OutputFormat.COMPACT else steps.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("add-step")
def testcase_add_step(
    ctx: typer.Context,
    issue_id: str = typer.Argument(..., help="Test case key"),
    project_id: str = typer.Argument("", help="Project ID kept for MCP parity"),
    step: str = typer.Option(..., "--step", help="Step description"),
    data: str | None = typer.Option(None, "--data", help="Test data"),
    result: str | None = typer.Option(None, "--result", help="Expected result"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        client = _make_client(ctx.obj)
        request = TestStepRequest(step=step, data=data, result=result)
        if dry_run:
            typer.echo(format_dry_run("PUT", f"{client.base_url}{client.API}/testcase/{issue_id}", body=request.model_dump(), fmt=fmt.value))
            return
        created = client.add_test_step(issue_id, project_id, request)
        typer.echo(format_output(created.to_compact_dict() if fmt == OutputFormat.COMPACT else created.model_dump(), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testcase_app.command("add-steps")
def testcase_add_steps(
    ctx: typer.Context,
    issue_id: str = typer.Argument(..., help="Test case key"),
    project_id: str = typer.Argument("", help="Project ID kept for MCP parity"),
    data_json: str = typer.Option(..., "--data-json", help="JSON array of step objects"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_array(data_json)
        requests = [TestStepRequest.model_validate(item) for item in data]
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("PUT", f"{client.base_url}{client.API}/testcase/{issue_id}", body=data, fmt=fmt.value))
            return
        created = client.add_multiple_test_steps(issue_id, project_id, requests)
        typer.echo(format_output([step.to_compact_dict() if fmt == OutputFormat.COMPACT else step.model_dump() for step in created], fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Test plans
# ---------------------------------------------------------------------------


@testplan_app.command("get")
def testplan_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test plan key"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(_render_model(_make_client(ctx.obj).get_testplan(key, fields=fields), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testplan_app.command("create")
def testplan_create(
    ctx: typer.Context,
    data_json: str = typer.Option(..., "--data-json", help="Test plan payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("POST", f"{client.base_url}{client.API}/testplan", body=data, fmt=fmt.value))
            return
        key = client.create_testplan(data)
        _write_result("created", key, fmt, summary=str(data.get("name", "")) or None)
    except AtlasError as e:
        _handle_error(e, fmt)


@testplan_app.command("search")
def testplan_search(
    ctx: typer.Context,
    query: str | None = typer.Option(None, "--query", help="TQL query"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    start_at: int = typer.Option(0, "--start-at", help="Pagination offset"),
    max_results: int = typer.Option(200, "--max-results", help="Maximum results"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        items = _make_client(ctx.obj).search_testplans(
            query=query, fields=fields, start_at=start_at, max_results=max_results
        )
        typer.echo(_render_models(items, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Test runs and results
# ---------------------------------------------------------------------------


@testrun_app.command("get")
def testrun_get(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test run key"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(_render_model(_make_client(ctx.obj).get_testrun(key, fields=fields), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("create")
def testrun_create(
    ctx: typer.Context,
    data_json: str = typer.Option(..., "--data-json", help="Test run payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("POST", f"{client.base_url}{client.API}/testrun", body=data, fmt=fmt.value))
            return
        key = client.create_testrun(data)
        _write_result("created", key, fmt, summary=str(data.get("name", "")) or None)
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("search")
def testrun_search(
    ctx: typer.Context,
    query: str | None = typer.Option(None, "--query", help="TQL query"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    start_at: int = typer.Option(0, "--start-at", help="Pagination offset"),
    max_results: int = typer.Option(200, "--max-results", help="Maximum results"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        items = _make_client(ctx.obj).search_testruns(
            query=query, fields=fields, start_at=start_at, max_results=max_results
        )
        typer.echo(_render_models(items, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("results")
def testrun_results(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="Test run key"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(_render_models(_make_client(ctx.obj).get_testrun_results(key), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("create-result")
def testrun_create_result(
    ctx: typer.Context,
    test_run_key: str = typer.Argument(..., help="Test run key"),
    test_case_key: str = typer.Argument(..., help="Test case key"),
    data_json: str = typer.Option(..., "--data-json", help="Result payload JSON object"),
    environment: str | None = typer.Option(None, "--environment", help="Environment filter"),
    user_key: str | None = typer.Option(None, "--user-key", help="User key filter"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testrun/{test_run_key}/testcase/{test_case_key}/testresult"
        if dry_run:
            typer.echo(format_dry_run("POST", url, body=data, fmt=fmt.value))
            return
        result_id = client.create_testrun_result(
            test_run_key, test_case_key, data, environment=environment, user_key=user_key
        )
        _write_result("created", test_case_key, fmt, id=str(result_id))
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("update-result")
def testrun_update_result(
    ctx: typer.Context,
    test_run_key: str = typer.Argument(..., help="Test run key"),
    test_case_key: str = typer.Argument(..., help="Test case key"),
    data_json: str = typer.Option(..., "--data-json", help="Result payload JSON object"),
    environment: str | None = typer.Option(None, "--environment", help="Environment filter"),
    user_key: str | None = typer.Option(None, "--user-key", help="User key filter"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testrun/{test_run_key}/testcase/{test_case_key}/testresult"
        if dry_run:
            typer.echo(format_dry_run("PUT", url, body=data, fmt=fmt.value))
            return
        result_id = client.update_testrun_result(
            test_run_key, test_case_key, data, environment=environment, user_key=user_key
        )
        _write_result("updated", test_case_key, fmt, id=str(result_id))
    except AtlasError as e:
        _handle_error(e, fmt)


@testrun_app.command("bulk-results")
def testrun_bulk_results(
    ctx: typer.Context,
    test_run_key: str = typer.Argument(..., help="Test run key"),
    data_json: str = typer.Option(..., "--data-json", help="JSON array of result payloads"),
    environment: str | None = typer.Option(None, "--environment", help="Environment filter"),
    user_key: str | None = typer.Option(None, "--user-key", help="User key filter"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_array(data_json)
        client = _make_client(ctx.obj)
        url = f"{client.base_url}{client.API}/testrun/{test_run_key}/testresults"
        if dry_run:
            typer.echo(format_dry_run("POST", url, body=data, fmt=fmt.value))
            return
        ids = client.create_bulk_testrun_results(test_run_key, data, environment=environment, user_key=user_key)
        typer.echo(format_output({"action": "created", "key": test_run_key, "ids": ids}, fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@testresult_app.command("create")
def testresult_create(
    ctx: typer.Context,
    data_json: str = typer.Option(..., "--data-json", help="Test result payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("POST", f"{client.base_url}{client.API}/testresult", body=data, fmt=fmt.value))
            return
        result_id = client.create_testresult(data)
        _write_result("created", str(data.get("testCaseKey", "")), fmt, id=str(result_id))
    except AtlasError as e:
        _handle_error(e, fmt)


# ---------------------------------------------------------------------------
# Environments and issue links
# ---------------------------------------------------------------------------


@environment_app.command("list")
def environment_list(
    ctx: typer.Context,
    project_key: str = typer.Argument(..., help="Project key"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(format_output(_make_client(ctx.obj).get_environments(project_key), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)


@environment_app.command("create")
def environment_create(
    ctx: typer.Context,
    data_json: str = typer.Option(..., "--data-json", help="Environment payload JSON object"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without executing"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        data = _parse_json_object(data_json)
        client = _make_client(ctx.obj)
        if dry_run:
            typer.echo(format_dry_run("POST", f"{client.base_url}{client.API}/environment", body=data, fmt=fmt.value))
            return
        env_id = client.create_environment(data)
        _write_result("created", str(data.get("projectKey", "")), fmt, id=str(env_id), summary=str(data.get("name", "")))
    except AtlasError as e:
        _handle_error(e, fmt)


@issuelink_app.command("testcases")
def issuelink_testcases(
    ctx: typer.Context,
    issue_key: str = typer.Argument(..., help="Jira issue key"),
    fields: str | None = typer.Option(None, "--fields", help="Comma-separated fields"),
    format: str | None = typer.Option(None, "--format", help="Override output format"),
) -> None:
    ctx.ensure_object(dict)
    fmt = _resolve_fmt(ctx.obj, format)
    try:
        typer.echo(_render_models(_make_client(ctx.obj).get_issue_testcases(issue_key, fields=fields), fmt))
    except AtlasError as e:
        _handle_error(e, fmt)
