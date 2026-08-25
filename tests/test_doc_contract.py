"""The doc-drift contract, generated. The contract itself is in DOCDRIFT.md.

Every repository in this toolset makes checkable claims in its README on purpose, and a claim
that was true when written and is false now is worse than one never made: it reads as evidence
right up until somebody checks it, and those somebodies are interviewers.

This file carries the half of the contract that is identical everywhere, and asserts that the
other half is still implemented here. It is generated, so editing it is pointless: change the
manifest instead.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent
README = REPO / "README.md"

# Paths the README names that are deliberately NOT in this repository, declared rather than
# inferred, so a real rename can never hide behind an exception.
FILE_EXCEPTIONS: set[str] = set()  # None declared for this repository.

# The named implementation of each other contract kind. These live in the shared manifest and are
# copied in here, so this repository fails on its own if one is deleted or renamed.
IMPLEMENTATIONS: dict[str, str] = {
    "NUMBER": "test_the_readme_states_the_number_of_tests_this_suite_actually_has",
    "COMMAND": "test_every_command_the_readme_shows_is_one_the_cli_accepts",
    "OUTPUT": "test_the_readme_output_block_is_what_the_demo_actually_prints",
    "REFERENCE": "test_every_test_the_readme_names_still_exists",
}

PATH_CLAIM = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|toml|yml|yaml|json|md|txt|png|svg|lock))`")


def _resolves(claim: str) -> bool:
    """The resolution rule, in order: root, then src/, then a unique basename in the tree.

    The src/ step is the module-path convention rather than laziness. Prose says
    `quidz/app.py` because that is the import path a reader types, and the file is at
    `src/quidz/app.py`. Both are correct and a check that refused the first would be wrong.
    """
    if (REPO / claim).exists():
        return True
    if (REPO / "src" / claim).exists():
        return True
    # The basename fallback applies ONLY to a claim with no directory in it. A claim that names a
    # directory is making a claim about that directory, and letting it resolve because some file
    # with the same basename exists elsewhere would accept exactly the rename this is looking for.
    # Found by mutation: creating .cursor/mcp.json made .vscode/mcp.json resolve too.
    if "/" in claim:
        return False
    hits = [
        p
        for p in REPO.rglob(claim)
        if ".venv" not in p.parts and "node_modules" not in p.parts and ".git" not in p.parts
    ]
    return len(hits) == 1


def test_every_file_the_readme_names_resolves_in_this_tree() -> None:
    """A path in the README that points at nothing is a rename nobody finished.

    This is the cheapest defect in the set to introduce and the least likely to be noticed: the
    prose stays plausible, the file moves, and only a reader who tries to open it finds out.
    """
    claims = sorted(set(PATH_CLAIM.findall(README.read_text(encoding="utf-8"))))
    assert claims, "the README names no files at all, which means this test is checking nothing"
    broken = [c for c in claims if c not in FILE_EXCEPTIONS and not _resolves(c)]
    assert broken == [], (
        f"the README names files that resolve nowhere: {broken}. Either fix the path, or if the "
        "file genuinely lives on the reader's machine rather than here, declare it in the "
        "manifest's fileExceptions."
    )


def test_every_declared_exception_is_still_needed() -> None:
    """An exception that has stopped being necessary is a hole nobody is watching.

    If a declared exception now resolves, the reason for it is gone and it should be removed
    rather than left as a standing permission for that path to break.
    """
    stale = [e for e in FILE_EXCEPTIONS if _resolves(e)]
    assert stale == [], f"these exceptions resolve now and should be removed: {stale}"


def test_this_repository_still_implements_every_contract_kind() -> None:
    """The contract is only a contract because something checks it is kept.

    Each kind above names the test in this repository that implements it. Delete or rename one
    and this fails, saying which kind lost its implementation. Without this, DOCDRIFT.md would be
    a description of what used to be true.
    """
    suite = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((REPO / "tests").glob("test_*.py"))
    )
    # Written as a loop rather than a comprehension on purpose. The one line version lands at
    # exactly the hundred character limit these repositories use, so a formatter would keep
    # rewrapping it and this generated file would never be format stable.
    missing: dict[str, str] = {}
    for kind, name in IMPLEMENTATIONS.items():
        if f"def {name}(" not in suite:
            missing[kind] = name
    assert missing == {}, (
        f"these contract kinds have no implementation in this repository any more: {missing}. "
        "Either restore the test or update the manifest to name its replacement."
    )
