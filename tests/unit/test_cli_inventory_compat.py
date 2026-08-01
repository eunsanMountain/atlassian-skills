"""Public CLI compatibility gate against the atls v0.2.13 baseline.

Plan reference: `.omx/plans/atls-0.3.0-markdown-first-informed-migration-plan.md`
sections 4.4, 13.7 and Story 0.

atls 0.2.13 is the published surface. The 0.3.0 redesign keeps every command,
option, alias, required flag and default from it unless the classification
fixture says otherwise, so the failure this guards against is a *silent* delta:
a flag quietly dropped while the SQLite layer is being removed, or a default
flipped during a rewrite. Reviewers cannot reliably catch that in a diff of
tens of thousands of lines, so the rule is mechanical instead — every
difference must be named in `inventory_delta_classification.json`, and an
unnamed one fails here.

The withdrawn 0.3.0 candidate (2e765e2) is still checked out, so its
candidate-only surface is tracked separately: it may be present now and absent
after Story 11, but it must never appear in the v0.2.13 baseline.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.cli_inventory import (
    SCHEMA,
    dumps,
    index_by_path,
    index_params,
    load_app_inventory,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "cli"
BASELINE_PATH = FIXTURE_DIR / "inventory_v0.2.13.json"
CLASSIFICATION_PATH = FIXTURE_DIR / "inventory_delta_classification.json"

VALID_CLASSIFICATIONS = {"kept", "changed", "removed", "added"}


def _load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    return _load(BASELINE_PATH)


@pytest.fixture(scope="module")
def classification() -> dict[str, Any]:
    return _load(CLASSIFICATION_PATH)


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return load_app_inventory()


def test_extraction_is_deterministic() -> None:
    """A fixture that shifts between runs cannot gate anything."""
    assert dumps(load_app_inventory()) == dumps(load_app_inventory())


def test_baseline_fixture_is_well_formed(baseline: dict[str, Any]) -> None:
    assert baseline["schema"] == SCHEMA
    assert baseline["entries"], "baseline inventory is empty"
    paths = [entry["path"] for entry in baseline["entries"]]
    assert len(paths) == len(set(paths)), "duplicate command paths in baseline"


def test_classification_entries_use_valid_values(classification: dict[str, Any]) -> None:
    for path, entry in classification["entries"].items():
        assert entry["classification"] in VALID_CLASSIFICATIONS, path
        for param_name, param in entry.get("params", {}).items():
            assert param["classification"] in VALID_CLASSIFICATIONS, f"{path}:{param_name}"


def test_candidate_only_surface_is_absent_from_the_baseline(
    baseline: dict[str, Any], classification: dict[str, Any]
) -> None:
    """Candidate-only paths carry no public compatibility debt, by definition.

    If one of them turns out to exist in v0.2.13, deleting it in Story 11 would
    be a real breaking change and must move into the classified delta instead.
    """
    baseline_paths = set(index_by_path(baseline))
    leaked = sorted(baseline_paths & set(classification["candidate_only"]["paths"]))
    assert not leaked, f"candidate-only paths present in the published v0.2.13 surface: {leaked}"


def test_every_command_path_delta_is_classified(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    """No command may appear or disappear without a named decision."""
    old = index_by_path(baseline)
    new = index_by_path(current)
    entries = classification["entries"]
    candidate_only = set(classification["candidate_only"]["paths"])

    unexplained_removals = [
        path for path in sorted(set(old) - set(new)) if entries.get(path, {}).get("classification") != "removed"
    ]
    assert not unexplained_removals, (
        f"v0.2.13 commands disappeared without a 'removed' classification: {unexplained_removals}"
    )

    unexplained_additions = [
        path
        for path in sorted(set(new) - set(old))
        if path not in candidate_only and entries.get(path, {}).get("classification") != "added"
    ]
    assert not unexplained_additions, (
        f"new commands appeared without an 'added' classification: {unexplained_additions}"
    )


def test_every_visibility_change_on_shared_commands_is_classified(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    """A command can vanish from help without being removed.

    The gate watched paths and parameters, so hiding one passed silently -- and
    a hidden command is, to anyone reading `--help` or using completion, gone.
    That is a compatibility decision with the same weight as deleting it, so it
    is named the same way.

    `visibility` rather than reusing `classification`, because the command itself
    is unchanged: it still exists, still takes the same options, still works.
    """

    old = index_by_path(baseline)
    new = index_by_path(current)
    entries = classification["entries"]

    unexplained = [
        f"{path}: hidden {old[path].get('hidden')} -> {new[path].get('hidden')}"
        for path in sorted(set(old) & set(new))
        if bool(old[path].get("hidden")) != bool(new[path].get("hidden"))
        and entries.get(path, {}).get("visibility") != "hidden"
    ]
    assert not unexplained, f"visibility changed without a named decision: {unexplained}"


def test_every_parameter_delta_on_shared_commands_is_classified(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    """The subtle regression: a command survives but its options drift."""
    old = index_by_path(baseline)
    new = index_by_path(current)
    entries = classification["entries"]

    unexplained: list[str] = []
    for path in sorted(set(old) & set(new)):
        declared = entries.get(path, {}).get("params", {})
        old_params = index_params(old[path])
        new_params = index_params(new[path])

        for name in sorted(set(old_params) - set(new_params)):
            if declared.get(name, {}).get("classification") != "removed":
                unexplained.append(f"{path}: parameter {name!r} removed")

        for name in sorted(set(new_params) - set(old_params)):
            if declared.get(name, {}).get("classification") != "added":
                unexplained.append(f"{path}: parameter {name!r} added")

        for name in sorted(set(old_params) & set(new_params)):
            if old_params[name] == new_params[name]:
                continue
            if declared.get(name, {}).get("classification") != "changed":
                unexplained.append(f"{path}: parameter {name!r} changed")

    assert not unexplained, "unclassified public CLI parameter deltas:\n  " + "\n  ".join(unexplained)


def test_classification_semantics_match_reality(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    """Each label must mean what it says, not merely name an existing path.

    Without this the classification degrades into a plain allowlist: any entry
    would excuse any delta on that path, and stale entries would keep widening
    the exemption as the code moves under them.
    """
    old = index_by_path(baseline)
    new = index_by_path(current)
    stale: list[str] = []

    for path, entry in classification["entries"].items():
        kind = entry["classification"]

        if kind == "added":
            if path in old:
                stale.append(f"{path}: declared 'added' but already exists in the v0.2.13 baseline")
            if path not in new:
                stale.append(f"{path}: declared 'added' but absent from the current CLI")
        elif kind == "removed":
            if path not in old:
                stale.append(f"{path}: declared 'removed' but never existed in the v0.2.13 baseline")
            if path in new:
                stale.append(f"{path}: declared 'removed' but still present")
        elif kind == "changed":
            if path not in old or path not in new:
                stale.append(f"{path}: declared 'changed' but not present on both sides")
            elif old[path] == new[path]:
                stale.append(f"{path}: declared 'changed' but the surface is identical")
        elif kind == "kept":
            if path in old and path in new and old[path] != new[path]:
                stale.append(f"{path}: declared 'kept' but the surface differs")

        if path not in old or path not in new:
            continue

        old_params = index_params(old[path])
        new_params = index_params(new[path])
        for name, param in entry.get("params", {}).items():
            param_kind = param["classification"]
            in_old, in_new = name in old_params, name in new_params
            label = f"{path}: parameter {name!r}"
            if param_kind == "added":
                if in_old:
                    stale.append(f"{label} declared 'added' but exists in the baseline")
                if not in_new:
                    stale.append(f"{label} declared 'added' but absent from the current CLI")
            elif param_kind == "removed":
                if not in_old:
                    stale.append(f"{label} declared 'removed' but never existed in the baseline")
                if in_new:
                    stale.append(f"{label} declared 'removed' but still present")
            elif param_kind == "changed":
                if not (in_old and in_new):
                    stale.append(f"{label} declared 'changed' but not present on both sides")
                elif old_params[name] == new_params[name]:
                    stale.append(f"{label} declared 'changed' but is identical")

    assert not stale, "classification entries no longer match reality:\n  " + "\n  ".join(stale)


def test_classification_pins_the_current_plan(classification: dict[str, Any]) -> None:
    """A plan edit must force the delta classification to be re-examined.

    The plan is a gitignored local artifact, so a clean clone legitimately does
    not have it; that case skips rather than fails. When it is present the hash
    must match, otherwise the classification could be silently describing an
    older contract.
    """
    repo_root = Path(__file__).resolve().parents[2]
    plan_path = repo_root / str(classification["plan"])
    if not plan_path.is_file():
        pytest.skip(f"plan document is not present in this checkout: {plan_path}")

    actual = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    recorded = str(classification["plan_sha256"])
    assert actual == recorded, (
        "the plan changed but the CLI delta classification was not re-pinned.\n"
        f"  plan      : {plan_path}\n"
        f"  recorded  : {recorded}\n"
        f"  actual    : {actual}\n"
        "Re-review the delta classification, then update 'plan_sha256'."
    )


def test_global_options_are_unchanged(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    """Root callback options apply to every command, so drift here is widest-blast-radius."""
    old = {str(param["name"]): param for param in baseline["root_params"]}
    new = {str(param["name"]): param for param in current["root_params"]}

    drift = [f"global option {name!r} removed" for name in sorted(set(old) - set(new))]
    drift += [f"global option {name!r} added" for name in sorted(set(new) - set(old))]
    drift += [
        f"global option {name!r} changed: {old[name]} -> {new[name]}"
        for name in sorted(set(old) & set(new))
        if old[name] != new[name]
    ]

    assert not drift, "global CLI options drifted from the v0.2.13 baseline:\n  " + "\n  ".join(drift)


# --------------------------------------------------------------------------
# Negative regressions.
#
# A gate that only ever passes proves nothing: it may be inspecting the wrong
# objects, or comparing something to itself. Each case below injects exactly
# the drift the corresponding gate exists to stop and asserts it is caught.
# --------------------------------------------------------------------------


def _entry(inventory: dict[str, Any], path: str) -> dict[str, Any]:
    for item in inventory["entries"]:
        if item["path"] == path:
            entry: dict[str, Any] = item
            return entry
    raise AssertionError(f"fixture no longer contains {path!r}; update this regression")


def test_gate_catches_unclassified_new_command(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(current)
    mutated["entries"].append({"path": "confluence page sneaky", "kind": "command", "hidden": False, "params": []})
    with pytest.raises(AssertionError, match="without an 'added' classification"):
        test_every_command_path_delta_is_classified(baseline, mutated, classification)


def test_gate_catches_silent_command_removal(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(current)
    mutated["entries"] = [item for item in mutated["entries"] if item["path"] != "confluence page search"]
    with pytest.raises(AssertionError, match="without a 'removed' classification"):
        test_every_command_path_delta_is_classified(baseline, mutated, classification)


def test_gate_catches_silent_option_drop(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(current)
    entry = _entry(mutated, "confluence page search")
    entry["params"] = [param for param in entry["params"] if param["name"] != "limit"]
    with pytest.raises(AssertionError, match="parameter 'limit' removed"):
        test_every_parameter_delta_on_shared_commands_is_classified(baseline, mutated, classification)


def test_gate_catches_silent_default_change(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(current)
    for param in _entry(mutated, "jira issue search")["params"]:
        if param["name"] == "limit":
            param["default"] = 999
    with pytest.raises(AssertionError, match="parameter 'limit' changed"):
        test_every_parameter_delta_on_shared_commands_is_classified(baseline, mutated, classification)


def test_gate_catches_global_option_drift(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    mutated = copy.deepcopy(current)
    mutated["root_params"] = [param for param in mutated["root_params"] if param["name"] != "quiet"]
    with pytest.raises(AssertionError, match="global option 'quiet' removed"):
        test_global_options_are_unchanged(baseline, mutated)


def test_gate_catches_global_alias_drop(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    """`-f` disappearing while `--format` stays would break every scripted call."""
    mutated = copy.deepcopy(current)
    for param in mutated["root_params"]:
        if param["name"] == "format":
            param["opts"] = ["--format"]
    with pytest.raises(AssertionError, match="global option 'format' changed"):
        test_global_options_are_unchanged(baseline, mutated)


def test_gate_catches_candidate_only_path_claimed_as_baseline(
    baseline: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(classification)
    mutated["candidate_only"]["paths"].append("confluence page search")
    with pytest.raises(AssertionError, match="candidate-only paths present"):
        test_candidate_only_surface_is_absent_from_the_baseline(baseline, mutated)


def test_gate_catches_added_that_already_existed_in_the_baseline(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    """The failure mode that turns the classification into a bare allowlist."""
    mutated = copy.deepcopy(classification)
    mutated["entries"]["confluence page search"] = {
        "classification": "added",
        "plan_ref": "none",
        "reason": "incorrectly classified",
    }
    with pytest.raises(AssertionError, match="already exists in the v0.2.13 baseline"):
        test_classification_semantics_match_reality(baseline, current, mutated)


def test_gate_catches_changed_without_an_actual_delta(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(classification)
    mutated["entries"]["confluence page search"] = {
        "classification": "changed",
        "plan_ref": "none",
        "reason": "stale entry",
    }
    with pytest.raises(AssertionError, match="the surface is identical"):
        test_classification_semantics_match_reality(baseline, current, mutated)


def test_gate_catches_removed_that_never_existed(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(classification)
    mutated["entries"]["confluence page ghost"] = {
        "classification": "removed",
        "plan_ref": "none",
        "reason": "stale entry",
    }
    with pytest.raises(AssertionError, match="never existed in the v0.2.13 baseline"):
        test_classification_semantics_match_reality(baseline, current, mutated)


def test_gate_catches_stale_parameter_classification(
    baseline: dict[str, Any], current: dict[str, Any], classification: dict[str, Any]
) -> None:
    mutated = copy.deepcopy(classification)
    mutated["entries"]["confluence page pull-md"]["params"]["format"] = {
        "classification": "removed",
        "plan_ref": "none",
        "reason": "stale entry",
    }
    with pytest.raises(AssertionError, match="declared 'removed' but still present"):
        test_classification_semantics_match_reality(baseline, current, mutated)


def test_gate_catches_plan_sha_mismatch(classification: dict[str, Any]) -> None:
    mutated = copy.deepcopy(classification)
    mutated["plan_sha256"] = "0" * 64
    plan_path = Path(__file__).resolve().parents[2] / str(mutated["plan"])
    if not plan_path.is_file():
        pytest.skip(f"plan document is not present in this checkout: {plan_path}")
    with pytest.raises(AssertionError, match="was not re-pinned"):
        test_classification_pins_the_current_plan(mutated)
