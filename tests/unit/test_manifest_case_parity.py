"""One case set, two products, each running its own resolver against it.

D2's requirement is **fixture parity, not shared implementation**. The two resolvers are
deliberately independent -- the downstream adapter must answer for documents atls has never seen,
and a hard dependency on the library would make "needs migration" unanswerable for exactly
the documents that need it. What must not be independent is the examples: testing one
judgement with two different sets eventually tests two different things, and the half that
drifts is the half nobody reads.

The corpus lives here, in a committed file, and the adapter asserts its own copy hashes to the same
value. That is the link, and it holds without either repository importing the other or
knowing where the other is checked out -- which a path-crossing test would need and CI
would not have.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "fixtures" / "manifest_cases.json"

#: The digest the adapter's `test_the_case_corpus_matches_the_one_atls_ships` also asserts. Changing
#: the corpus therefore breaks the other product's suite until it is mirrored, which is the
#: point: a set that can be edited on one side alone is not shared.
CORPUS_SHA256 = "db8a4fac216212f994f3d716f6f9960e58ae7aeeab962887016f7ca5363cf9a0"


def _cases() -> list[dict]:
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert payload["schema"] == "atls-manifest-cases-v1"
    return payload["cases"]


def test_the_corpus_is_the_one_both_products_agreed_on() -> None:
    digest = hashlib.sha256(json.dumps(_cases(), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    assert digest == CORPUS_SHA256, (
        "the shared manifest case set changed; mirror it into the downstream adapter's "
        "test_managed_manifest_is_the_only_binding.py and update both digests"
    )


def test_the_corpus_covers_both_answers() -> None:
    """A set of twelve that all said "no" would pass any resolver that always refuses."""

    cases = _cases()
    assert len(cases) == 12
    assert sum(1 for case in cases if case["publishable"]) >= 1
    assert sum(1 for case in cases if not case["publishable"]) >= 8
    assert all(case["why"].strip() for case in cases), "every case says what it is for"


@pytest.mark.parametrize("case", _cases(), ids=[case["name"] for case in _cases()])
def test_atls_agrees_with_the_shared_corpus(case: dict) -> None:
    """atls's own parser, on the same examples the downstream adapter is judged against.

    Only the publishable/not answer is compared. The refusal *vocabularies* are each
    product's own -- the adapter names `manifest_duplicated` where atls raises
    `ManagedManifestError` -- and demanding one vocabulary would be shared implementation,
    which D2 explicitly does not ask for.
    """

    from atlassian_skills.core.managed_manifest import ManagedManifestError, parse_managed_manifest

    try:
        manifest = parse_managed_manifest(case["text"])
    except (ManagedManifestError, ValueError):
        accepted = False
    else:
        accepted = manifest is not None and bool(getattr(manifest, "page", None))

    assert accepted is case["publishable"], (
        f"{case['name']}: atls says publishable={accepted}, the shared corpus says "
        f"{case['publishable']} -- {case['why']}"
    )
