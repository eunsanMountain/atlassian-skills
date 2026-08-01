"""Concurrent `record` safety on Windows, with real processes.

AC7 is platform-neutral: two concurrent records end with exactly one winner and a named
refusal. POSIX gets that from `flock` on the canonical file, and review R2 verified it
with two real processes. Windows cannot use the same mechanism -- `msvcrt` locks byte
ranges mandatorily and a mandatorily-locked file cannot be renamed over -- so it locks a
sentinel in the temporary directory instead.

That branch has never executed. This file is what executes it, and it is skipped
everywhere else, so a green run on Linux says nothing about Windows and does not pretend
to. Three things are checked here that reading the code cannot establish:

* the lock actually excludes a second process, rather than both writing;
* the refusal is the same `record_in_progress` POSIX gives, so the two legs are one
  contract and not two;
* a killed holder does not poison the document. The operating system releases the lock
  when the handle closes, including on process death, which is why this design replaced
  one that removed a lock file in a `finally` -- that one left a file which refused every
  later record until somebody deleted it by hand.

Subprocesses rather than threads, deliberately: the earlier version of the POSIX test
called `record` twice in sequence and was named as though it raced. A lock that works
between threads and not between processes would pass that and fail in the field.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="exercises the Windows record lock")

#: The page before and after the third party's edit. Every process that builds the fake
#: needs both: the manifest binds the *first* version and §5.4 reads it back out of
#: history, so a fake constructed with only the current body has no entry for the version
#: the document is bound to and every record is refused
#: `historical_storage_hash_mismatch`.
#:
#: Found by running this file against the POSIX lock with the skip removed. That is worth
#: recording as a method: a Windows-only test cannot be debugged on a Windows-only run,
#: and everything here except the lock itself is platform-neutral, so the plumbing was
#: checked where it could be checked. Two real defects turned up that way -- a wrong
#: `record_reconciled_against` signature and this one -- both of which would otherwise
#: have surfaced as a red CI job nobody could reproduce locally.
ORIGINAL = "<p>alpha paragraph text here</p>"
MOVED = "<p>alpha paragraph text here</p><p>and more</p>"

#: Injected into each subprocess so it replays the same history as its parent.
_BUILD_CLIENT = f"""
from tests.unit.conftest import HistoryClient

def build_client():
    client = HistoryClient(storage={ORIGINAL!r})
    client.move_to({MOVED!r})
    return client
"""


def _client() -> Any:
    """The fake, with the same history every other process in this file will build."""

    from tests.unit.conftest import HistoryClient

    client = HistoryClient(storage=ORIGINAL)
    client.move_to(MOVED)
    return client


def _managed(tmp_path: Path) -> tuple[Path, Path, str]:
    """A managed document, a reconciled body, and the fingerprint, via the public path."""

    from atlassian_skills.confluence.pull_md import pull_md
    from atlassian_skills.confluence.reconcile import compare
    from tests.unit.conftest import HistoryClient

    client = HistoryClient(storage=ORIGINAL)
    managed = tmp_path / "page.md"
    pull_md(client, "123", output_path=managed, portable=True, no_assets=True)
    managed.write_text(
        managed.read_text(encoding="utf-8").replace("alpha paragraph", "alpha edited"),
        encoding="utf-8",
    )
    client.move_to(MOVED)
    comparison = compare(client, "123", managed)
    reconciled = tmp_path / "reconciled.md"
    reconciled.write_text("alpha edited\n\nand more\n", encoding="utf-8")
    return managed, reconciled, comparison.fingerprint()


_HOLDER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from atlassian_skills.confluence.reconcile import _held

path = Path(sys.argv[1])
with _held(path):
    Path(sys.argv[2]).write_text("held", encoding="utf-8")
    time.sleep(float(sys.argv[3]))
print(json.dumps({{"released": True}}))
"""


#: A column-0 template, like `_HOLDER` above. `textwrap.dedent` cannot be used here:
#: interpolating a multi-line block whose own lines start at column 0 makes the common
#: prefix empty, so dedent leaves the surrounding lines indented and the subprocess dies
#: on an IndentationError before it can report anything. That presented as a barrier that
#: never opened, which is a much worse thing to debug than a syntax error.
_RACER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {repo!r})
from atlassian_skills.confluence.reconcile import record_reconciled_against
from atlassian_skills.core.errors import ValidationError
{build}

ready = Path(sys.argv[2]); barrier = Path(sys.argv[3])
ready.write_text("ready", encoding="utf-8")
while not barrier.exists():
    time.sleep(0.005)
client = build_client()
try:
    receipt = record_reconciled_against(
        client, "123", Path(sys.argv[1]), Path(sys.argv[5]), compare_fingerprint=sys.argv[4]
    )
    print(json.dumps(["ok", receipt["status"]]))
except ValidationError as error:
    print(json.dumps(["refused", error.context.get("reason")]))
"""


def _spawn_holder(managed: Path, flag: Path, seconds: float) -> subprocess.Popen[str]:
    script = _HOLDER.format(repo=str(Path(__file__).resolve().parents[2]))
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), str(managed), str(flag), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(flag: Path, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if flag.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"the holder never reported taking the lock: {flag}")


def test_a_second_process_is_refused_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    from atlassian_skills.confluence.reconcile import record_reconciled_against
    from atlassian_skills.core.errors import ValidationError

    managed, reconciled, fingerprint = _managed(tmp_path)
    flag = tmp_path / "held.flag"
    holder = _spawn_holder(managed, flag, 5.0)
    try:
        _wait_for(flag)
        with pytest.raises(ValidationError) as refused:
            record_reconciled_against(
                _client(),
                "123",
                managed,
                reconciled,
                compare_fingerprint=fingerprint,
            )
        # The same reason POSIX raises. Two legs, one contract.
        assert refused.value.context["reason"] == "record_in_progress"
    finally:
        holder.wait(timeout=30)

    # And nothing was left in the directory people keep in Git.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["held.flag", "page.md", "reconciled.md"]


def test_the_record_succeeds_once_the_holder_has_finished(tmp_path: Path) -> None:
    """The refusal has to be temporary, or it is an outage rather than arbitration."""

    from atlassian_skills.confluence.reconcile import record_reconciled_against

    managed, reconciled, fingerprint = _managed(tmp_path)
    flag = tmp_path / "held.flag"
    holder = _spawn_holder(managed, flag, 0.5)
    _wait_for(flag)
    holder.wait(timeout=30)

    receipt = record_reconciled_against(
        _client(),
        "123",
        managed,
        reconciled,
        compare_fingerprint=fingerprint,
    )

    assert receipt["status"] == "reconciled"


def test_a_killed_holder_does_not_leave_the_document_permanently_refused(tmp_path: Path) -> None:
    """The defect this design exists to avoid.

    The first Windows implementation created a lock file with `O_EXCL` and removed it in
    a `finally`. A process killed in between left the file, and every later record on that
    document was refused until somebody deleted it by hand. The sentinel here is locked by
    the operating system, which releases it when the handle closes -- and a killed process
    closes its handles.
    """

    from atlassian_skills.confluence.reconcile import record_reconciled_against

    managed, reconciled, fingerprint = _managed(tmp_path)
    flag = tmp_path / "held.flag"
    holder = _spawn_holder(managed, flag, 60.0)
    _wait_for(flag)
    holder.kill()
    holder.wait(timeout=30)

    receipt = record_reconciled_against(
        _client(),
        "123",
        managed,
        reconciled,
        compare_fingerprint=fingerprint,
    )

    assert receipt["status"] == "reconciled", "a killed holder left the document unrecordable"


def test_exactly_one_of_two_racing_processes_wins(tmp_path: Path) -> None:
    """Both start before either finishes, and the loser gets the named refusal.

    A barrier rather than a sleep: with a sleep the two calls can simply not overlap, and
    a test that never creates the window it is named for passes for the wrong reason.
    """

    managed, reconciled, fingerprint = _managed(tmp_path)
    barrier = tmp_path / "go.flag"
    script = _RACER.format(repo=str(Path(__file__).resolve().parents[2]), build=_BUILD_CLIENT)
    procs = []
    for index in (0, 1):
        ready = tmp_path / f"ready{index}.flag"
        procs.append(
            (
                ready,
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(managed),
                        str(ready),
                        str(barrier),
                        fingerprint,
                        str(reconciled),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
        )
    for ready, _proc in procs:
        _wait_for(ready)
    barrier.write_text("go", encoding="utf-8")

    outcomes = []
    for _ready, proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        outcomes.append(json.loads(out.strip().splitlines()[-1]))

    kinds = sorted(kind for kind, _detail in outcomes)
    assert kinds == ["ok", "refused"], outcomes
    refusal = next(detail for kind, detail in outcomes if kind == "refused")
    # Either arbitration: the lock refused it, or it took the lock after the winner had
    # already replaced the file and the re-derived comparison caught it. Both are correct
    # and both are write-0 for the loser; what must not happen is two winners.
    assert refusal in {"record_in_progress", "local_changed_since_compare"}, outcomes


def test_two_processes_with_different_temp_directories_still_arbitrate(tmp_path: Path) -> None:
    """R2-AC7-1's counterexample, as a test.

    The first Windows implementation locked a sentinel under `tempfile.gettempdir()`, which
    reads `TMP`/`TEMP` from the process environment. Two processes with different `TMP`
    values locked different sentinels and neither saw the other, so setting an environment
    variable defeated the arbitration §7.4 requires on the document.

    The lock is a named mutex now, whose name comes from the resolved document path alone,
    so this should hold by construction. It is asserted anyway, with the environments
    deliberately different, because "by construction" is what the previous version also
    looked like.
    """

    import os as _os

    managed, reconciled, fingerprint = _managed(tmp_path)
    flag = tmp_path / "held.flag"

    holder_temp = tmp_path / "tmp-holder"
    holder_temp.mkdir()
    env = dict(_os.environ)
    env["TMP"] = env["TEMP"] = str(holder_temp)

    script = _HOLDER.format(repo=str(Path(__file__).resolve().parents[2]))
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(managed), str(flag), "5.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(flag)
        # This process keeps the inherited TMP, which is a different directory.
        from atlassian_skills.confluence.reconcile import record_reconciled_against
        from atlassian_skills.core.errors import ValidationError

        with pytest.raises(ValidationError) as refused:
            record_reconciled_against(_client(), "123", managed, reconciled, compare_fingerprint=fingerprint)
        assert refused.value.context["reason"] == "record_in_progress"
    finally:
        holder.wait(timeout=30)
