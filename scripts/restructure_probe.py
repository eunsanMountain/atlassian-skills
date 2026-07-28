#!/usr/bin/env python3
"""Measure how often a block-restructure edit is provable, across a pulled corpus.

Why this exists
---------------
The live gate measures three edit shapes: no-edit round trip, EOF append, and a
one-word text edit. Real document work does none of those -- it rewrites, moves,
and drops whole blocks. That shape drops to ``full_migration``, where the edit
alignment can tie and the publish fails closed, and we have no idea how often
that happens because it has never been in a sample.

This probe supplies the missing number. For each managed page it deletes one
block and re-adds it in three positions, and records the verdict:

    in the deleted block's position | at the end | at the start

The first is expected to be provable and the other two are the ones at risk.
The spread between them is the signal: if all three pass on most real pages,
the failure is a corner case; if the last two fail broadly, in-place editing is
not usable for document work and the tie rule needs design attention.

Read-only
---------
Every call is ``pull-md`` (read) or ``push-md --dry-run`` (no PUT). The probe
never mutates a page. It still talks to the server, because the proof needs
fresh remote storage and that is not recoverable from a pulled file alone.

Usage
-----
    python scripts/restructure_probe.py --run-dir RUN --out report.json
    python scripts/restructure_probe.py --self-test        # offline, no network
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# The managed preamble -- manifest, notice, migration/asset comments -- is bound to
# the page and must survive verbatim; only the body below it is a probe target.
_PREAMBLE_RE = re.compile(r"\A(?:<!--.*?-->\s*)+", re.S)
_PAGE_ID_RE = re.compile(r"<!--\s*atls:managed\b[^>]*?\bpage=(\d+)")

# Value-free verdict tokens. Anything else collapses to "other" rather than being
# echoed, so a page title or body slice can never reach the report.
_READY = "ready"
_CONSENT = "consent"
_FATAL = "fatal"


def split_preamble(text: str) -> tuple[str, str]:
    """Split a managed file into (preamble, body)."""

    match = _PREAMBLE_RE.match(text)
    if match is None:
        return "", text
    return match.group(0), text[match.end() :]


def page_id_of(text: str) -> str | None:
    match = _PAGE_ID_RE.search(text)
    return match.group(1) if match else None


def body_blocks(body: str) -> list[str]:
    """Top-level blocks, blank-line separated, with empties dropped."""

    return [block for block in re.split(r"\n\s*\n", body.strip("\n")) if block.strip()]


def _movable_index(blocks: list[str]) -> int | None:
    """Pick a block that is safe to move: a plain paragraph, not first or last.

    Headings anchor sections and fenced code / tables / list runs can change
    meaning when relocated, so restricting to a paragraph keeps the probe
    measuring the alignment rather than a content-shape artifact.
    """

    for index in range(1, max(len(blocks) - 1, 1)):
        block = blocks[index]
        if block.lstrip().startswith(("#", "|", "```", "> ", "- ", "* ", "<!--")):
            continue
        if re.match(r"\s*\d+\.\s", block):
            continue
        return index
    return None


PROBE_MARKER = " atls-restructure-probe"


def restructure_variants(text: str) -> dict[str, str]:
    """Rewrite one paragraph and place it in three positions.

    All three variants carry the *same* edited block, so they are the same
    ``full_migration`` edit and differ only in where that block lands. Without the
    edit the in-place variant would be byte-identical to the remote and short out
    to ``no_change``, which measures nothing.

    Returns an empty dict when the document has no block that can be moved
    without also changing what the document means.
    """

    preamble, body = split_preamble(text)
    blocks = body_blocks(body)
    index = _movable_index(blocks)
    if index is None:
        return {}
    edited = blocks[index].rstrip() + PROBE_MARKER
    rest = blocks[:index] + blocks[index + 1 :]

    def build(ordered: list[str]) -> str:
        return preamble + "\n\n".join(ordered) + "\n"

    return {
        # Control: same edit, block stays where it was. Expected to be provable.
        "same_position": build([*rest[:index], edited, *rest[index:]]),
        "at_end": build([*rest, edited]),
        "at_start": build([edited, *rest]),
    }


def classify(stdout: str, returncode: int) -> dict[str, Any]:
    """Reduce an atls JSON envelope to a value-free verdict."""

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {"verdict": "other", "exit": returncode}
    if not isinstance(payload, dict):
        return {"verdict": "other", "exit": returncode}

    error = payload.get("error")
    if isinstance(error, dict):
        context = error.get("context") or {}
        reason = context.get("reason")
        verdict = _CONSENT if reason == "migration_consent_required" else _FATAL
        return {
            "verdict": verdict,
            "exit": returncode,
            "reason": reason if isinstance(reason, str) else None,
            "fatal_class": context.get("fatal_class") if isinstance(context.get("fatal_class"), str) else None,
            "proof_mode": None,
        }
    return {
        "verdict": _CONSENT if payload.get("consent_required") else _READY,
        "exit": returncode,
        "reason": None,
        "fatal_class": None,
        "proof_mode": payload.get("proof_mode"),
    }


def _run(args: list[str], timeout: int = 600) -> tuple[str, int]:
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    return done.stdout, done.returncode


def probe_page(managed_path: Path, *, atls: str = "atls") -> dict[str, Any]:
    text = managed_path.read_text(encoding="utf-8")
    page_id = page_id_of(text)
    if page_id is None:
        return {"skipped": "not_a_managed_file"}
    variants = restructure_variants(text)
    if not variants:
        return {"skipped": "no_movable_block", "page_id": page_id}

    version = _remote_version(atls, page_id)
    if version is None:
        return {"skipped": "version_unavailable", "page_id": page_id}

    results: dict[str, Any] = {"page_id": page_id, "blocks": len(body_blocks(split_preamble(text)[1]))}
    with tempfile.TemporaryDirectory() as tmp:
        for name, content in variants.items():
            candidate = Path(tmp) / f"{name}.md"
            candidate.write_text(content, encoding="utf-8")
            stdout, code = _run(
                [
                    atls,
                    "confluence",
                    "page",
                    "push-md",
                    page_id,
                    "--md-file",
                    str(candidate),
                    "--if-version",
                    str(version),
                    "--dry-run",
                    "--format=json",
                ]
            )
            results[name] = classify(stdout, code)
    return results


def _remote_version(atls: str, page_id: str) -> int | None:
    stdout, code = _run([atls, "confluence", "page", "get", page_id, "--format=json"], timeout=120)
    if code != 0:
        return None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    version = (payload.get("version") or {}).get("number")
    return version if isinstance(version, int) else None


def summarize(pages: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    classes: dict[str, dict[str, int]] = {}
    for page in pages:
        for name in ("same_position", "at_end", "at_start"):
            result = page.get(name)
            if not isinstance(result, dict):
                continue
            counts.setdefault(name, {}).setdefault(result["verdict"], 0)
            counts[name][result["verdict"]] += 1
            fatal_class = result.get("fatal_class")
            if fatal_class:
                classes.setdefault(name, {}).setdefault(fatal_class, 0)
                classes[name][fatal_class] += 1
    return {
        "pages": len(pages),
        "skipped": sum(1 for page in pages if page.get("skipped")),
        "verdicts": counts,
        "fatal_classes": classes,
    }


def _self_test() -> int:
    """Exercise the offline halves: variant construction and envelope classification."""

    managed = (
        "<!-- atls:managed v=2 page=456 site=sha256:x remote_version=3 -->\n"
        "<!-- cfxmark:notice keep me -->\n"
        "\n# Title\n\nfirst paragraph\n\nmiddle paragraph\n\nlast paragraph\n"
    )
    assert page_id_of(managed) == "456"
    preamble, body = split_preamble(managed)
    assert "atls:managed" in preamble and "cfxmark:notice" in preamble
    assert "atls:managed" not in body

    variants = restructure_variants(managed)
    assert set(variants) == {"same_position", "at_end", "at_start"}
    edited_blocks = {name: body_blocks(split_preamble(content)[1]) for name, content in variants.items()}
    original = body_blocks(body)
    moved = next(block for block in edited_blocks["at_end"] if block.endswith(PROBE_MARKER))

    for name, content in variants.items():
        # The manifest binds the page; a probe that rewrites it is measuring itself.
        assert content.startswith(preamble), name
        # Same blocks, same edit, in all three — only the position differs.
        assert sorted(edited_blocks[name]) == sorted(edited_blocks["at_end"]), name
        assert len(edited_blocks[name]) == len(original), name
    # The control must be a real edit, not the untouched document: an identical body
    # would short out to no_change and measure nothing.
    assert edited_blocks["same_position"] != original
    assert edited_blocks["same_position"].index(moved) == original.index(moved[: -len(PROBE_MARKER)])
    assert edited_blocks["at_end"][-1] == moved
    assert edited_blocks["at_start"][0] == moved

    # A heading-only document has nothing safe to move.
    assert restructure_variants("<!-- atls:managed page=1 -->\n\n# only\n\n## headings\n") == {}

    assert classify('{"proof_mode": "full_migration", "consent_required": false}', 0)["verdict"] == _READY
    assert classify('{"consent_required": true}', 0)["verdict"] == _CONSENT
    fatal = classify(
        '{"error": {"context": {"reason": "ownership_proof_invalid", "fatal_class": "semantic-mapping-ambiguous"}}}',
        7,
    )
    assert fatal["verdict"] == _FATAL and fatal["fatal_class"] == "semantic-mapping-ambiguous"
    consent = classify('{"error": {"context": {"reason": "migration_consent_required"}}}', 7)
    assert consent["verdict"] == _CONSENT
    # Non-JSON output must not be echoed anywhere.
    assert classify("Error: something with a PAGE TITLE in it", 7) == {"verdict": "other", "exit": 7}

    print("self-test OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", type=Path, help="run directory containing children/i*/page.md")
    parser.add_argument("--out", type=Path, help="write the JSON report here")
    parser.add_argument("--atls", default="atls", help="atls executable to use")
    parser.add_argument("--self-test", action="store_true", help="offline checks only, no network")
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.run_dir is None:
        parser.error("--run-dir is required unless --self-test is given")

    managed_files = sorted(args.run_dir.glob("children/*/page.md"))
    if not managed_files:
        parser.error(f"no children/*/page.md under {args.run_dir}")

    pages: list[dict[str, Any]] = []
    for path in managed_files:
        result = probe_page(path, atls=args.atls)
        result["source"] = path.parent.name
        pages.append(result)
        verdicts = " ".join(
            f"{name}={result[name]['verdict']}" for name in ("same_position", "at_end", "at_start") if name in result
        )
        print(f"{path.parent.name}: {verdicts or result.get('skipped')}", flush=True)

    report = {"schema": "atls-restructure-probe-v1", "summary": summarize(pages), "pages": pages}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
