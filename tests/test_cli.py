from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quidz.cli import main


def run_demo(tmp_path: Path, *extra: str) -> Path:
    assert main(["demo", "--db", str(tmp_path / "quidz.db"), *extra]) == 0
    return tmp_path / "quidz.db"


def counter(output: str, name: str) -> int:
    match = re.search(rf"^\s+{name}\s+(\d+)$", output, re.MULTILINE)
    assert match is not None, f"counter {name} missing from:\n{output}"
    return int(match.group(1))


def test_demo_prints_the_ledger_the_report_and_the_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_demo(tmp_path)
    output = capsys.readouterr().out
    assert all(marker in output for marker in ("LEDGER", "EXCEPTION REPORT", "seed          0"))


def test_demo_with_a_tampered_delivery_rejects_it_and_still_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_demo(tmp_path, "--break", "tamper")
    assert counter(capsys.readouterr().out, "signature_rejected") > 0


def test_reconcile_fails_on_a_critical_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    report_path = tmp_path / "report.json"
    code = main(
        ["reconcile", "--db", str(db_path), "--json", str(report_path), "--fail-on", "critical"]
    )
    severities = {
        finding["severity"] for finding in json.loads(report_path.read_text())["findings"]
    }
    capsys.readouterr()
    assert (code, "critical" in severities) == (1, True)


def test_an_unknown_flag_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        main(["demo", "--not-a-flag"])
    assert raised.value.code == 2


def test_replay_reproduces_the_terminal_state_of_the_run_that_logged_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    capsys.readouterr()
    code = main(
        ["replay", f"{db_path}.jsonl", "--db", str(tmp_path / "replayed.db"), "--assert-terminal"]
    )
    assert (code, "terminal state matches the log" in capsys.readouterr().out) == (0, True)
