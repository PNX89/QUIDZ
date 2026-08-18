from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from quidz.cli import build_parser, main

README = Path(__file__).resolve().parent.parent / "README.md"

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


def test_the_readme_cites_every_source_the_design_depends_on() -> None:
    urls = set(re.findall(r"https://[^\s<>()\[\],]+", README.read_text(encoding="utf-8")))
    assert sorted(REQUIRED_CITATIONS - urls) == []
