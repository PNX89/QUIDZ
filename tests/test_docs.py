from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from quidz import __version__
from quidz.cli import INSTALL_COMMAND, build_parser, main

TESTS = Path(__file__).resolve().parent
README = TESTS.parent / "README.md"

# The --db value the README's Quickstart passes, and therefore the path its output block
# prints. This run uses tmp_path instead and substitutes, rather than skipping those two lines:
# the block is the one thing a reader is invited to check against a real run, so a line the
# comparison excludes is the line that drifts.
README_DB = "/tmp/quidz-demo/quidz.db"

# The facts this design rests on, each one cited where it is used. Reachability is deliberately
# not checked: no test in this repository touches the network.
REQUIRED_CITATIONS = frozenset(
    {
        "https://docs.adyen.com/development-resources/api-idempotency",
        "https://docs.adyen.com/development-resources/currency-codes",
        "https://docs.adyen.com/development-resources/webhooks/handle-webhook-events",
        "https://docs.adyen.com/development-resources/webhooks/secure-webhooks/verify-hmac-signatures",
        "https://docs.adyen.com/development-resources/webhooks/webhook-types",
        "https://docs.adyen.com/reporting/settlement-reconciliation/transaction-level/settlement-details-report",
        "https://docs.stripe.com/api/idempotent_requests",
        "https://docs.stripe.com/currencies",
        "https://docs.stripe.com/payments/paymentintents/lifecycle",
        "https://docs.stripe.com/payments/place-a-hold-on-a-payment-method",
        "https://docs.stripe.com/refunds",
        "https://docs.stripe.com/webhooks",
        "https://docs.stripe.com/webhooks/signature",
        "https://github.com/Adyen/adyen-python-api-library/issues/117",
        "https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md",
        "https://www.sqlite.org/wal.html",
    }
)


def fenced_blocks(text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    info: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            if info is None:
                info, body = line[3:].strip(), []
            else:
                blocks.append((info, body))
                info = None
            continue
        if info is not None:
            body.append(line)
    return blocks


def demo_block() -> list[str]:
    for _, body in fenced_blocks(README.read_text(encoding="utf-8")):
        if body and body[0] == "QUIDZ demo":
            return body
    raise AssertionError("the README carries no demo output block starting with 'QUIDZ demo'")


def documented_commands() -> list[list[str]]:
    """Every `quidz ...` the README shows, in a bash fence or in prose backticks alike.

    The exit codes of `quidz reconcile --fail-on` and `quidz replay --assert-terminal` are the
    CI contract this repo argues for, and both are documented in prose, so a scan that reads
    fenced blocks only covers everything except the two commands that matter most.
    """
    text = README.read_text(encoding="utf-8")
    lines = [line for info, body in fenced_blocks(text) if info == "bash" for line in body]
    lines += [
        span
        for span in re.findall(r"`([^`\n]+)`", text)
        if span == "quidz" or span.startswith("quidz ")
    ]
    out: list[list[str]] = []
    for line in lines:
        tokens = shlex.split(line.split("#", 1)[0])
        if "quidz" not in tokens:
            continue
        index = tokens.index("quidz")
        if index == 0 or tokens[index - 1] == "run":
            out.append(tokens[index + 1 :])
    return out


def test_the_readme_output_block_is_what_the_demo_actually_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The README's example output is a captured run, and stays one as the code changes.

    Every line is compared, the database and delivery log paths included. Only the temporary
    directory this run was given is normalised to the one the README's own command passes.
    """
    db_path = tmp_path / "quidz.db"
    argv = ["demo", "--scenario", "adversarial", "--seed", "0", "--db", str(db_path)]
    assert main(argv) == 0
    printed = capsys.readouterr().out.replace(str(db_path), README_DB)
    assert printed.splitlines() == demo_block()


def test_every_command_the_readme_shows_is_one_the_cli_accepts() -> None:
    parser = build_parser()
    parsed = [parser.parse_args(argv).command for argv in documented_commands()]
    assert set(parsed) == {"demo", "reconcile", "replay"}


def test_the_quickstart_runs_the_command_that_produced_the_output_block() -> None:
    """A block introduced as a real run has to be reproducible from a command the README gives.

    Without the explicit --db the demo writes to a fresh mkdtemp path, so the two path lines
    shown could not come from the command shown.
    """
    quickstart = next(argv for argv in documented_commands() if argv[:1] == ["demo"])
    assert quickstart == ["demo", "--scenario", "adversarial", "--db", README_DB]


def test_every_test_the_readme_names_still_exists() -> None:
    """The invariant table's whole value is that each row names the test that pins it.

    Rename a test and the row points at nothing, which is worse than no citation: it reads as
    evidence right up until somebody goes looking for it.
    """
    text = README.read_text(encoding="utf-8")
    cited = set(re.findall(r"`(test_\w+\.py)::(\w+)`", text))
    missing = sorted(
        f"{module}::{name}"
        for module, name in cited
        if f"def {name}(" not in (TESTS / module).read_text(encoding="utf-8")
    )
    assert (len(cited), missing) == (12, [])


def test_the_install_command_the_cli_prints_is_the_one_the_readme_gives() -> None:
    # Two places tell a reader how to install, and the CLI's copy is the one nobody re-reads.
    assert INSTALL_COMMAND in README.read_text(encoding="utf-8")


def test_the_readme_cites_every_source_the_design_depends_on() -> None:
    urls = set(re.findall(r"https://[^\s<>()\[\],]+", README.read_text(encoding="utf-8")))
    assert sorted(REQUIRED_CITATIONS - urls) == []


def test_the_readme_states_the_number_of_tests_this_suite_actually_has() -> None:
    """Collected in a subprocess, so the number comes from pytest rather than from a memory.

    A count typed into a README is true on the day it is typed. `--collect-only` runs nothing,
    so this does not recurse, and `-o addopts=` neutralises this repository's own addopts so
    the output shape is the plain one this parses rather than whatever the config makes it.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
        cwd=TESTS.parent,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    assert match is not None, f"could not read a collection total from:\n{result.stdout[-500:]}"
    collected = int(match.group(1))
    assert collected > 0
    assert f"{collected} tests" in README.read_text(encoding="utf-8")


def _escaped(text: str) -> str:
    """The card is HTML, so the captured output appears in it escaped, not raw."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def test_the_committed_demo_output_still_matches_a_live_run(tmp_path: Path) -> None:
    """The Pages card publishes this output, so a stale copy is a lie on a public page.

    Run against a fresh database in tmp_path, with the path substituted back, exactly as
    `test_the_readme_output_block_is_what_the_demo_actually_prints` does above. The ledger is
    idempotent by design, so a second run against the same file is a run in which nothing
    happens, and comparing against that would prove only that the demo can do nothing twice.
    """
    db = tmp_path / "quidz.db"
    live = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys;from quidz.cli import main;sys.exit(main())",
            "demo",
            "--scenario",
            "adversarial",
            "--db",
            str(db),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=TESTS.parent,
    ).stdout.replace(str(db), "/tmp/quidz-demo/quidz.db")
    committed = (TESTS.parent / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    assert committed == live, (
        "docs/evidence/demo.txt no longer matches a live run. "
        "Run: uv run python scripts/capture_evidence.py, then regenerate the card."
    )


def test_the_published_card_carries_the_output_it_claims_to() -> None:
    card = (TESTS.parent / "site" / "index.html").read_text(encoding="utf-8")
    demo = (TESTS.parent / "docs" / "evidence" / "demo.txt").read_text(encoding="utf-8")
    assert _escaped(demo.rstrip()) in card, "the card's terminal block is not the captured output"
    assert "a test fails when it" in card
    assert "/Users/" not in card and "/var/folders/" not in card


def test_the_card_states_numbers_that_are_true_today() -> None:
    facts = json.loads(
        (TESTS.parent / "docs" / "evidence" / "facts.json").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
        cwd=TESTS.parent,
    )
    match = re.search(r"^(\d+) tests? collected", result.stdout, re.MULTILINE)
    assert match is not None, f"no collection total in:\n{result.stdout[-400:]}"
    assert facts["tests"] == int(match.group(1)), "facts.json's test total is stale"
    # Against the package version, never `git describe`: actions/checkout clones without tags.
    assert facts["release"] == f"v{__version__}"
    card = (TESTS.parent / "site" / "index.html").read_text(encoding="utf-8")
    assert f"<dd>{facts['tests']}</dd>" in card
    assert f"<dd>{facts['release']}</dd>" in card
