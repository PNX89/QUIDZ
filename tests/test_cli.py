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


def test_a_delivery_log_that_is_not_utf_8_is_a_usage_error_and_not_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 out of replay means the ledger did not reproduce its recorded terminal state.

    A log this process cannot decode has to be exit 2, the unreadable-input code, because
    UnicodeDecodeError is a ValueError and the OSError handler beside it never sees one. Left
    alone it exits on a traceback, and an operator reads that as replay drift.
    """
    log = tmp_path / "broken.jsonl"
    log.write_bytes(b'{"kind":"delivery","provider":"stripe"\xff}\n')
    code = main(["replay", str(log), "--db", str(tmp_path / "replayed.db"), "--assert-terminal"])
    assert (code, "not valid UTF-8" in capsys.readouterr().err) == (2, True)


def test_a_provider_snapshot_that_is_not_utf_8_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = run_demo(tmp_path)
    capsys.readouterr()
    Path(f"{db_path}.remote.json").write_bytes(b'{"as_of": 1.0, "payments": [\xff]}')
    code = main(["reconcile", "--db", str(db_path)])
    assert (code, "is not readable" in capsys.readouterr().err) == (2, True)


def test_replay_reproduces_the_terminal_state_of_the_run_that_logged_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    capsys.readouterr()
    code = main(
        ["replay", f"{db_path}.jsonl", "--db", str(tmp_path / "replayed.db"), "--assert-terminal"]
    )
    assert (code, "terminal state matches the log" in capsys.readouterr().out) == (0, True)
