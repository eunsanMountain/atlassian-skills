from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
SKILL = ROOT / "src" / "atlassian_skills" / "_assets" / "skills" / "atls" / "SKILL.md"


def test_bundled_skill_has_only_state_free_markdown_workflow() -> None:
    text = SKILL.read_text(encoding="utf-8")
    stale = (
        "SQLite",
        "protected_region",
        "protected-region",
        "needs_migration",
        "atls state",
        "table-style",
        "allow-stale-managed",
        "migration accept",
        "migration revoke",
        "binding marker",
    )

    assert not {token for token in stale if token.casefold() in text.casefold()}

    # Inversions of the managed/stateless and md diff safety boundaries must
    # never appear (a doc edit that equates the two transports, or promotes
    # md diff to a storage proof, is exactly the regression these guard).
    forbidden = (
        "same raw JSON",
        "identical raw JSON",
        "md diff is a storage",
        "md diff` is a storage",
    )
    assert not {token for token in forbidden if token.casefold() in text.casefold()}
    for token in (
        "page inspect",
        "pulled_with_migrations",
        "exact_remote_prefix_append",
        "migration_consent_required",
        "next_actions[].argv",
        "--accept-migration",
        "--accept-conversion",
        "upload_unknown",
        "readback_pending",
        "requires_user_approval=true",
        "--intent=structure-edit",
        "without an unnecessary get or repull",
        # Scoped to the Confluence command once Jira grew an `md push` of its
        # own. The unqualified sentence now reads as a promise Jira does not
        # keep -- it uploads nothing.
        "Automatic asset synchronization exists only in Confluence `page md push`",
        "Jira `issue description md push` uploads nothing",
        "page update --body-format storage",
        "This in-file journal is Confluence `page md push` only",
        # The Jira description journal is a sidecar, because the file's whole
        # content IS the description and an in-band marker would publish.
        "`<file>.atls.op.json`",
        "exact cfxmark version is fingerprint input",
        # managed vs stateless transports differ; branch on outcome, not fields.
        "different raw JSON/`status`",
        "branch on outcome, not field equality",
        # md compare is a local review aid, never a storage-candidate proof.
        "`md compare` is a three-way Markdown comparison, not a storage-candidate proof",
        # And the verb rule that keeps a reader off `diff` for a local file at all.
        "`compare` is a local file against the live remote",
        # The managed description contract, pinned where it is user-facing.
        "Attachments are carried, never uploaded",
        "whitespace_only_change",
        "identity_not_carried",
    ):
        assert token in text


def _cfxmark_requirement() -> str:
    """The cfxmark requirement exactly as pyproject declares it.

    Read rather than duplicated: the point of this assertion is that the docs and
    the package agree, and a second copy of the range can only ever drift from the
    first. It also stops a deliberate floor bump from failing here for no reason.
    """

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # Anchored to a dependency entry, not merely the first quoted `cfxmark` in the file.
    # The unanchored form matched a *comment* -- the capability build string
    # `("cfxmark 0.6.1", "editable")` -- so this helper returned a converter label while
    # its docstring promised the requirement. The two happened to agree until the pin
    # became a range, and then the README had to repeat a version-with-space that
    # pyproject never declared.
    match = re.search(r'^\s*"(cfxmark[<>=!~][^"]*)",?\s*$', text, re.MULTILINE)
    assert match, "pyproject.toml declares no cfxmark requirement"
    return match.group(1)


def test_readme_and_migration_guide_match_public_cli_surface() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "confluence-markdown-0.3-migration.md").read_text(encoding="utf-8")
    current = readme + guide

    for removed in (
        "atls confluence page migration list",
        "atls confluence page migration accept",
        "atls confluence page migration revoke",
        "atls state recover",
        "atls state relocate",
        "--allow-stale-managed",
    ):
        assert removed not in current
    for required in (
        "page get PAGE_ID --body-repr=view --format=raw",
        "page inspect PAGE_ID --format=json",
        "page md validate page.md --format=json",
        "page md push PAGE_ID",
        "--accept-migration",
        "--accept-conversion",
        _cfxmark_requirement(),
        "outside Markdown-conversion",
    ):
        assert required in current


def test_design_records_portable_authority_and_recovery_boundaries() -> None:
    design = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")

    for required in (
        "S0/M0/C0/M1/C1",
        "exact_remote_prefix_append",
        "requires_user_approval=true",
        "remote_freshness=not_checked",
        "upload_unknown",
        "body_put_not_observed",
        "readback_pending",
        "There is no checkout registry",
        "no runtime SQLite state",
        "Live Atlassian writes",
        "journal is scoped to managed `md push`",
        "page update --body-format=storage",
    ):
        assert required in design


#: Excluded from the identifier guard, each for a reason that is not "it fails".
#: Anything not listed here is guarded: a document added later is covered by
#: default, and taking it out is a decision somebody has to write down.
_NOT_ABOUT_THIS_PRODUCT = {
    # An inventory of the MCP server this project replaces. The names in it are
    # that server's tools, so they are supposed to be absent from this source.
    "docs/mcp-analysis.md",
}
_RECORDS_RATHER_THAN_PROMISES = "docs/release-evidence"


def _documents_describing_this_product() -> list[Path]:
    """Every document that tells a reader what this product does.

    Evidence files are left out because they describe defects, and describing a
    wrong name requires writing it.
    """

    docs = [ROOT / "README.md", ROOT / "CHANGELOG.md", SKILL]
    docs += sorted(
        path
        for path in (ROOT / "docs").rglob("*.md")
        if _RECORDS_RATHER_THAN_PROMISES not in path.as_posix()
        and str(path.relative_to(ROOT)) not in _NOT_ABOUT_THIS_PRODUCT
    )
    return docs


def test_the_docs_name_only_states_the_code_actually_has() -> None:
    """A state a document names and the code never produces is a false promise.

    The tests above pin documents against each other: DESIGN.md must contain a
    phrase, SKILL.md must contain a phrase. Nothing compared either of them to
    the product, so `body_put_failed` lived in five documents and no source file
    for as long as those documents existed -- pinned, and therefore looking
    verified. The first sweep found three of the five, which is why this guard
    takes every document by default instead of a list somebody remembers to
    extend. The real status is `body_put_not_observed`, and the difference is
    not spelling: the whole recovery design rests on a lost response being
    indistinguishable from an unsent request, so a state called `failed` asserts
    the one thing this code refuses to assert. DESIGN.md said it two sentences
    before "It never guesses that an upload or PUT succeeded."

    Checked against source, not against another document. This file is excluded
    from the haystack for the same reason: it holds the doc strings it pins, so
    including it would let any name here vouch for itself.
    """

    self_path = Path(__file__).resolve()
    haystack = "\n".join(
        path.read_text(encoding="utf-8")
        for root in ("src", "tests")
        for path in (ROOT / root).rglob("*.py")
        if path.resolve() != self_path
    )
    # Snake_case in backticks: how these documents write an identifier. Prose
    # words and flags do not match, so a false positive would have to be a
    # multi-word lowercase phrase joined by underscores, which is a name.
    named = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")

    missing = {}
    for doc in _documents_describing_this_product():
        absent = sorted({n for n in named.findall(doc.read_text(encoding="utf-8")) if n not in haystack})
        if absent:
            missing[str(doc.relative_to(ROOT))] = absent

    assert not missing, f"documents name identifiers no source file has: {missing}"


def test_the_skill_names_the_manifest_version_the_code_writes() -> None:
    """`atls:managed v=2` sat in the skill after the code moved to v3.

    Nothing caught it, because the skill's manifest version was prose. It is the
    first line an agent reads before hand-inspecting a managed file, so a stale
    number there sends the agent looking for fields that moved -- the same failure
    as a document naming a state the code does not produce, which is what U0 spent
    a pass removing.
    """

    from atlassian_skills.core.managed_manifest import CURRENT_MANAGED_MANIFEST_VERSION

    skill = (
        Path(__file__).resolve().parents[2] / "src" / "atlassian_skills" / "_assets" / "skills" / "atls" / "SKILL.md"
    ).read_text(encoding="utf-8")

    versions = set(re.findall(r"atls:managed v=(\d+)", skill))
    assert versions, "the skill no longer names the manifest at all"
    assert versions == {str(CURRENT_MANAGED_MANIFEST_VERSION)}, (
        f"the skill names manifest {sorted(versions)} and the code writes {CURRENT_MANAGED_MANIFEST_VERSION}"
    )


def test_every_command_the_skill_names_exists_and_is_not_hidden() -> None:
    """The skill's commands, checked against the CLI rather than against a word list.

    P8 found the skill telling agents to run `page md prepare-merge` and
    `page md finalize-merge` for the whole stale flow. This release demoted both to hidden
    aliases and made `compare`, `prepare-reconcile`, `record-reconciled-against` and
    `rebaseline` the public path — so an agent following the skill was reaching for commands
    `--help` no longer lists, and the four it should have used were not mentioned once.

    Nothing caught that, because the existing contract test compares hand-listed strings: it
    can only notice a command somebody thought to add to the list. This derives one side from
    the CLI, so the next demoted command fails here without anyone maintaining anything.

    Hidden counts as absent on purpose. A hidden command still runs, and that is exactly the
    trap — the skill is what an agent reads instead of `--help`, so a command it names must be
    one the agent could have found on its own.
    """

    from tests.support.cli_inventory import load_app_inventory

    skill = _packaged_skill()
    inventory = load_app_inventory()
    visible = {str(entry["path"]) for entry in inventory["entries"] if not entry.get("hidden")}
    known = {str(entry["path"]) for entry in inventory["entries"]}

    # Command paths as the skill writes them: a product word, then subcommands, stopping at
    # the first token that is not a bare lowercase word (an argument, a flag, a backtick).
    products = ("confluence", "jira", "bitbucket", "auth", "state", "setup", "doctor")
    groups = {
        "page": "confluence",
        "issue": "jira",
        "space": "confluence",
        "search": "confluence",
    }
    named: set[str] = set()
    for match in re.finditer(r"`([a-z][a-z0-9 -]{2,80})`", skill):
        tokens = match.group(1).split()
        if not tokens:
            continue
        if tokens[0] in groups:
            tokens = [groups[tokens[0]], *tokens]
        elif tokens[0] not in products:
            continue
        # Trim to the longest prefix that is a real path, so `page md push PAGE_ID` reduces
        # to `confluence page md push` rather than being discarded for its argument.
        for size in range(len(tokens), 1, -1):
            candidate = " ".join(tokens[:size])
            if candidate in known:
                named.add(candidate)
                break

    assert named, "no command paths were recognised in the skill -- the parser has drifted"
    hidden_or_missing = sorted(path for path in named if path not in visible)
    assert not hidden_or_missing, "the skill names commands an agent cannot find in --help: " + ", ".join(
        hidden_or_missing
    )


def test_the_skill_documents_the_reconciliation_commands_it_needs() -> None:
    """The other half: a command can exist, be visible, and still go unmentioned.

    The test above catches a command the skill names and the CLI has moved on from. It cannot
    catch the reverse -- §7.1's four commands being absent entirely, which is how the stale
    flow ended up documented in terms of two hidden aliases. These four are the flow, so they
    are named here rather than inferred.
    """

    skill = _packaged_skill()

    for command in ("md compare", "md prepare-reconcile", "md record-reconciled-against", "md rebaseline"):
        assert command in skill, f"the skill does not mention `{command}`"


# --------------------------------------------------------------------------
# The Skill may not promise a write the policy refuses
# --------------------------------------------------------------------------


def _packaged_skill() -> str:
    """The Skill as the wheel ships it, not as the working tree holds it.

    R4-pre round 1: every check here read the repository path, so they passed against a
    build whose packaged Skill did not contain the change under test -- `verify.sh` only
    rebuilt on `*.py`, and `SKILL.md` is not one. A documentation contract that reads the
    source it was written from is checking the author's intention; what a user gets is the
    installed artifact.
    """

    import atlassian_skills

    installed = Path(atlassian_skills.__file__).parent / "_assets" / "skills" / "atls" / "SKILL.md"
    assert installed.is_file(), f"no packaged Skill at {installed}"
    return installed.read_text(encoding="utf-8")


def _effectively_permitted(status: str) -> bool:
    """Whether this build actually lets a pull write a canonical file for this grade.

    Both of §8.2.1's axes, read from the shipped registries rather than the policy row: a
    registered capability lifts the row and the row does not know it. Reading the row meant
    the guard below could not see a grade already unlocked, so it would pass a Skill
    telling agents a write is refused when the build permits it.
    """

    import cfxmark

    from atlassian_skills.confluence.compatibility import PROFILE, STATUS_BY_CLASSIFICATION
    from atlassian_skills.confluence.preservation import CAPABILITIES, identity_preservation_for

    row = next(item for item in STATUS_BY_CLASSIFICATION.values() if item.status == status)
    if row.canonical_write_permitted:
        return True
    if status == "markdown_identity_bound":
        return identity_preservation_for(f"cfxmark {cfxmark.__version__}", PROFILE) is not None
    if status == "xhtml_required":
        return bool(CAPABILITIES)
    return False


def test_the_skill_does_not_call_a_forbidden_grade_editable() -> None:
    """Derived from the policy table, not from a list somebody keeps in step.

    The Skill said `markdown_identity_bound` was editable and that a pull writes a
    file for it. Both were false: the grade's `canonical_write_permitted` is `False`
    until a preservation capability is registered, so the pull writes nothing and the
    push refuses. An agent following the Skill would have gone looking for a file that
    was never created — and the Skill is what it reads *instead of* checking.

    The claim under test is narrow on purpose. This does not ask the Skill to explain
    the policy; it asks that no grade the policy forbids is named next to a word
    promising a write.

    **Scoped to the Confluence section, because the grade name is shared and the
    policies are not.** A Jira description graded `markdown_identity_bound` really is
    editable: `description_push.assess_candidate` compares identity values and their
    multiplicity against a freshly read issue, so the grade's promise is backed. The
    Confluence grade of the same name has no registered preservation capability, so its
    promise is not. One name, two policies, and the first version of this guard failed
    on the Jira line -- which is worth keeping in view, because a reader can make the
    same mistake the guard did.
    """

    from atlassian_skills.confluence.compatibility import STATUS_BY_CLASSIFICATION

    text = _packaged_skill()
    forbidden = {
        status.status for status in STATUS_BY_CLASSIFICATION.values() if not _effectively_permitted(status.status)
    }
    assert forbidden, "every grade permits a canonical write, so this guard proves nothing"

    promises = ("editable", "write a file", "writes a file")
    confluence = text.split("## Jira", 1)[0]
    assert "canonical_write_permitted" in confluence, "the Confluence section was not located"
    for line in confluence.splitlines():
        named = sorted(status for status in forbidden if status in line)
        if not named:
            continue
        said = [word for word in promises if word in line]
        assert not said, f"{named} promised {said}: {line.strip()}"


def test_the_skill_points_at_the_field_that_actually_decides() -> None:
    """A guard against the previous one being satisfied by deleting the subject.

    Removing every mention of the grades would pass the check above and leave an agent
    with no way to tell whether a pull will produce a file. So the field that decides
    has to be named.
    """

    text = _packaged_skill()
    assert "canonical_write_permitted" in text


def test_the_lifecycle_table_names_every_generated_file_suffix() -> None:
    """Derived from the code's own suffixes, not from a list kept in step by hand.

    The table's value is entirely in being complete: a generated file it omits is one
    nobody was warned about, and the two mistakes it exists to prevent -- committing an
    intermediate, deleting a publish in flight -- are both made by someone who did not
    know the file was there.

    Suffixes rather than paths, because the paths are the caller's and the suffixes are
    ours.
    """

    text = _packaged_skill()
    table = text.split("## Files this writes to your disk", 1)
    assert len(table) == 2, "the lifecycle table is gone"
    section = table[1].split("\n## ", 1)[0]

    from atlassian_skills.confluence.compatibility import storage_path_for

    generated = {
        ".md.atls.json",
        # Whatever the storage sidecar is actually called, asked of the code that names it.
        Path(storage_path_for("page.md") or "page.xhtml").suffix,
        ".reconciled.md",
    }
    # Rows, not the section. Prose beneath the table mentions `atls:operation` too, and
    # the first version of this check passed with that row deleted -- satisfied by the
    # sentence explaining the row that was no longer there.
    rows = [line for line in section.splitlines() if line.startswith("|")]
    joined = "\n".join(rows)
    for suffix in sorted(generated):
        assert suffix in joined, f"no lifecycle row for {suffix}"
    for marker in ("atls:managed", "atls:operation"):
        assert marker in joined, f"no lifecycle row for {marker}"
    # Four columns, so a row cannot answer "commit it?" by omission.
    for row in rows[2:]:
        assert row.count("|") >= 5, f"lifecycle row is missing a column: {row}"


def test_the_skill_does_not_deny_a_write_this_build_permits() -> None:
    """The other direction, which nothing checked.

    A6 registered the identity carry, so `markdown_identity_bound` publishes here. A Skill
    still saying only one grade may write is not dangerous the way a false promise is --
    nobody loses data -- but it sends an agent onto the storage workflow for a page that
    never needed it, and the agent cannot discover the sentence is stale.
    """

    from atlassian_skills.confluence.compatibility import STATUS_BY_CLASSIFICATION

    text = _packaged_skill()
    permitted = sorted(
        status.status for status in STATUS_BY_CLASSIFICATION.values() if _effectively_permitted(status.status)
    )
    assert permitted, "nothing is permitted, so this guard proves nothing"
    for line in text.splitlines():
        if "only" not in line.lower():
            continue
        for grade in permitted:
            assert grade not in line, f"{grade} is permitted here and this line says otherwise: {line.strip()}"


def test_the_docs_warn_about_uv_run_exactly_while_it_is_broken() -> None:
    """Derived from the lock and `pyproject.toml`, so the note cannot outlive its reason.

    Both `CLAUDE.md` and `README.md` tell a reader to run `uv run pytest`. When the
    declared cfxmark lower bound exceeds the registry lock, that command cannot work.
    An instruction that fails is worse than a missing one — the reader assumes their
    environment is broken.

    Asserted in both directions. While the lock is behind, the warning must be present;
    once the lock catches up, the warning must go, or the next reader is told a working
    command is broken.
    """

    import re

    import tomli

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'\[\[package\]\]\nname = "cfxmark"\nversion = "([^"]+)"', lock)
    assert match, "no cfxmark in uv.lock"
    locked = tuple(int(part) for part in match.group(1).split(".")[:3])

    project = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = next(item for item in project["project"]["dependencies"] if item.startswith("cfxmark"))
    floor_text = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", requirement)
    assert floor_text, f"no lower bound in {requirement!r}"
    floor = tuple(int(part) for part in floor_text.group(1).split(".")[:3])

    behind = locked < floor
    for name in ("CLAUDE.md", "README.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        warned = "does not work in this tree yet" in text
        if behind:
            assert warned, f"{name} tells a reader to run `uv run pytest` and the lock cannot support it"
        else:
            assert not warned, (
                f"{name} still warns that `uv run pytest` is broken, but the lock now satisfies "
                f"cfxmark>={'.'.join(map(str, floor))} -- remove the note"
            )


def test_the_internal_release_evidence_is_not_tracked_in_this_public_repository() -> None:
    """The gate that replaced the readiness gate, because the thing it guarded moved.

    `RELEASE-BLOCKERS.md` used to live here and this test used to assert that no evidence
    file claimed the release was ready while a blocker row was open. That ledger and the
    77 files around it are now kept with the release artifacts instead: this repository is
    public, and they carry the organisation's Jira keys, Confluence page ids and absolute
    local paths. Most of them are verbatim pytest transcripts, so scrubbing the
    identifiers would have edited the record rather than redacted it.

    The old assertion cannot follow them out -- CI cannot read a ledger that is not in the
    checkout, and a gate that silently passes because its subject is missing is worse than
    no gate. So it is replaced by the invariant that now matters: the evidence must not
    come back. `git rm --cached` alone would not hold, because the files stay on disk and
    every check would keep passing locally while the next `git add -A` re-committed them.

    Asserted against the index rather than the filesystem, deliberately -- the files are
    expected to exist locally for whoever is running the release.
    """

    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "docs/release-evidence", "docs/release-0.3.4-runbook.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert tracked == [], (
        f"{len(tracked)} internal release-evidence file(s) are tracked in a public repository, "
        f"starting with {tracked[0]}"
    )


def test_the_two_products_reconcile_under_one_vocabulary() -> None:
    """D3, derived from both command groups rather than from a list kept in step.

    §7.1 made `compare` / `prepare-reconcile` / `record-reconciled-against` / `rebaseline`
    the public reconciliation names for Confluence and demoted `prepare-merge` /
    `finalize-merge` to hidden aliases. Jira kept the old names — so the same operation on
    the same kind of document had two names depending on which product you had reached, and
    an agent reads one skill for both.

    The old names must stay reachable. They are in scripts, and breaking a caller to tidy a
    help listing is the worse trade.
    """

    from typer.main import get_command

    from atlassian_skills.cli.main import app

    root = get_command(app)

    def visible_and_hidden(*path: str) -> tuple[set[str], set[str]]:
        group = root
        for part in path:
            group = group.commands[part]  # type: ignore[attr-defined]
        commands = group.commands  # type: ignore[attr-defined]
        visible = {name for name, cmd in commands.items() if not cmd.hidden}
        hidden = {name for name, cmd in commands.items() if cmd.hidden}
        return visible, hidden

    jira_visible, jira_hidden = visible_and_hidden("jira", "issue", "description")
    confluence_visible, _ = visible_and_hidden("confluence", "page", "md")

    shared = {"prepare-reconcile", "record-reconciled-against"}
    for name in sorted(shared):
        assert name in confluence_visible, f"Confluence no longer publishes {name}"
        assert name in jira_visible, f"Jira does not publish {name}, so the two vocabularies differ"

    # The demoted names still resolve, and are still out of the listing.
    for old in ("prepare-merge", "finalize-merge"):
        assert old in jira_hidden, f"{old} must remain a hidden alias, not vanish or reappear"


def test_the_readme_agrees_with_the_code_about_the_manifest_version() -> None:
    """R4-pre P2. The README said v2 while the code writes v3.

    Derived from the constant, not from a second copy of the number. A reader following a
    README that names the wrong manifest version does not get a warning; they get a document
    the push refuses, and no way to tell which of the two is wrong.
    """

    from atlassian_skills.core.managed_manifest import CURRENT_MANAGED_MANIFEST_VERSION

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    current = f"v{CURRENT_MANAGED_MANIFEST_VERSION} manifest"
    assert current in readme, f"the README does not mention the {current} the code writes"
    stale = [
        f"v{other} manifest" for other in range(1, CURRENT_MANAGED_MANIFEST_VERSION) if f"v{other} manifest" in readme
    ]
    assert not stale, f"the README still describes {stale}"


def test_the_readme_does_not_promise_a_pull_that_always_writes() -> None:
    """R4-pre P2, and the more dangerous half of it.

    The README said "pull always publishes the managed file, even when conversion reports
    losses" and called loss reporting "guidance, not a hidden local approval state". Both
    were true once and are the opposite of §8.2 now: the pull writes nothing where the write
    is not permitted. A reader following it goes looking for a file that was never created,
    concludes the tool is broken, and has no reason to suspect the README.

    Checked against the same field the Skill and the payload use, so the three cannot drift
    apart independently.
    """

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "canonical_write_permitted" in readme, "the README does not name the field that decides"
    for claim in ("pull always publishes", "always publishes the managed file"):
        assert claim not in readme.casefold(), f"the README still claims: {claim}"


def test_the_docs_name_the_version_the_package_actually_is() -> None:
    """Three files claim a current version and only one of them is the truth.

    `pyproject.toml` is what ships. The README roadmap said `0.3.0 (current)` and
    `CLAUDE.md` said `Current version: 0.3.0` while the package had been 0.3.4 for four
    releases -- a reader deciding whether a feature is in their install was being told
    to look at the wrong one.

    Checked rather than corrected once, because this is drift, not a typo: the release
    procedure bumps `pyproject.toml` and nothing walks the prose.
    """

    import tomli

    version = tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = re.findall(r"^- \*\*([0-9]+\.[0-9]+\.[0-9]+) \(current\)\*\*", readme, re.MULTILINE)
    assert claimed == [version], f"README roadmap says {claimed}, package is {version}"

    guide = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    stated = re.findall(r"\*\*Current version\*\*: ([0-9]+\.[0-9]+\.[0-9]+)", guide)
    assert stated == [version], f"CLAUDE.md says {stated}, package is {version}"
