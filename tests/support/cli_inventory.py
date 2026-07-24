"""Canonical CLI surface extraction for public-compatibility gating.

Plan reference: `.omx/plans/atls-0.3.0-markdown-first-informed-migration-plan.md`
sections 4.4, 13.7 and Story 0.

The 0.3.0 redesign keeps atls 0.2.13 as the public compatibility baseline: every
command, option, alias, required flag and default stays unless an explicit
breaking-change entry says otherwise. Reviewing that by reading diffs does not
scale, so this module reduces the Typer tree to a stable JSON document that can
be diffed mechanically and classified as kept/changed/removed/added.

Extraction goes through ``typer.main.get_command`` rather than Typer's own
objects because Click is what actually parses argv: what we record here is the
surface a user hits, not the decorator source.
"""

from __future__ import annotations

import json
from typing import Any

import click
import typer
from typer.main import get_command

SCHEMA = "atls-cli-inventory-v1"

# Defaults are compared byte-for-byte across versions, so every value has to
# survive a JSON round trip identically. Anything outside this set is recorded
# through a marker form instead of its repr, because reprs of callables and
# sentinels embed memory addresses and would make the fixture unstable.
_JSON_SCALARS = (str, int, float, bool)


def _normalize_default(value: Any) -> Any:
    """Return a stable JSON representation of a Click default."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_default(item) for item in value]
    if callable(value):
        # Callable defaults are resolved at parse time; the identity of the
        # function is not part of the user-visible contract.
        return {"__dynamic__": True}
    return {"__opaque__": type(value).__name__}


def _param_kind(param: click.Parameter) -> str:
    if isinstance(param, click.Argument):
        return "argument"
    if isinstance(param, click.Option):
        return "option"
    return type(param).__name__.lower()


def _type_name(param: click.Parameter) -> str:
    param_type = param.type
    name = getattr(param_type, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(param_type).__name__.lower()


def _describe_param(param: click.Parameter) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": param.name,
        "kind": _param_kind(param),
        # ``opts`` is the user-facing spelling set: for options it is the flag
        # plus aliases, for arguments it is the metavar-ish positional name.
        "opts": sorted(param.opts),
        "secondary_opts": sorted(param.secondary_opts),
        "required": bool(param.required),
        "default": _normalize_default(param.default),
        "multiple": bool(getattr(param, "multiple", False)),
        "nargs": param.nargs,
        "type": _type_name(param),
    }
    if isinstance(param, click.Option):
        entry["is_flag"] = bool(param.is_flag)
        entry["hidden"] = bool(param.hidden)
    return entry


def _describe_params(command: click.Command) -> list[dict[str, Any]]:
    described = [_describe_param(param) for param in command.params]
    # Sort by the python-level name: it is stable across versions even when a
    # flag spelling changes, which is exactly the delta we want to surface.
    return sorted(described, key=lambda item: str(item["name"]))


def _walk(command: click.Command, path: tuple[str, ...]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(command, click.Group):
        if path:
            # Record the group itself so that adding or removing a whole
            # command family, or changing a group-level option, is visible.
            entries.append(
                {
                    "path": " ".join(path),
                    "kind": "group",
                    "hidden": bool(command.hidden),
                    "params": _describe_params(command),
                }
            )
        for name in sorted(command.commands):
            entries.extend(_walk(command.commands[name], (*path, name)))
        return entries
    entries.append(
        {
            "path": " ".join(path),
            "kind": "command",
            "hidden": bool(command.hidden),
            "params": _describe_params(command),
        }
    )
    return entries


def extract_inventory(app: typer.Typer) -> dict[str, Any]:
    """Reduce a Typer application to a stable, diffable surface document."""
    root = get_command(app)
    entries = _walk(root, ())
    return {
        "schema": SCHEMA,
        "root_params": _describe_params(root),
        "entries": sorted(entries, key=lambda item: str(item["path"])),
    }


def load_app_inventory() -> dict[str, Any]:
    """Extract the inventory of the installed atls CLI."""
    from atlassian_skills.cli.main import app

    return extract_inventory(app)


def dumps(inventory: dict[str, Any]) -> str:
    """Serialize an inventory so that byte comparison is meaningful."""
    return json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def index_by_path(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(entry["path"]): entry for entry in inventory["entries"]}


def index_params(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(param["name"]): param for param in entry["params"]}
