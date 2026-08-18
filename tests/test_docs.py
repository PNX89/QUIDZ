from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from quidz.cli import build_parser, main

README = Path(__file__).resolve().parent.parent / "README.md"

# Only the database and delivery log lines carry the temporary path, so they are the only two
# lines a run into tmp_path may legitimately differ on.
VOLATILE_PREFIXES = ("  database", "  delivery log")

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
    out: list[list[str]] = []
    for info, body in fenced_blocks(README.read_text(encoding="utf-8")):
        if info != "bash":
            continue
        for line in body:
            tokens = shlex.split(line.split("#", 1)[0])
            if "quidz" not in tokens:
                continue
            index = tokens.index("quidz")
            if index == 0 or tokens[index - 1] == "run":
                out.append(tokens[index + 1 :])
    return out


def stable_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith(VOLATILE_PREFIXES)]


def test_the_readme_output_block_is_what_the_demo_actually_prints(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The README's example output is a captured run, and stays one as the code changes."""
    argv = ["demo", "--scenario", "adversarial", "--seed", "0", "--db", str(tmp_path / "q.db")]
    assert main(argv) == 0
    printed = stable_lines(capsys.readouterr().out)
    assert printed == stable_lines("\n".join(demo_block()))


def test_every_command_the_readme_shows_is_one_the_cli_accepts() -> None:
    parser = build_parser()
    commands = documented_commands()
    parsed = [parser.parse_args(argv).command for argv in commands]
    assert (len(commands), set(parsed)) == (5, {"demo"})


def test_the_readme_cites_every_source_the_design_depends_on() -> None:
    urls = set(re.findall(r"https://[^\s<>()\[\],]+", README.read_text(encoding="utf-8")))
    assert sorted(REQUIRED_CITATIONS - urls) == []
