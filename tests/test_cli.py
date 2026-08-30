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


def test_the_unmodelled_break_mode_retries_then_dead_letters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The mechanism the whole README is built around, exercised end to end: an event type this
    # ledger does not model is stored rather than refused at the door, retried under the cap
    # of three, and dead lettered where a human sees it rather than dropped.
    run_demo(tmp_path, "--break", "unmodelled")
    output = capsys.readouterr().out
    assert "dead_lettered 1   retried 2" in output
    assert counter(output, "dead_lettered") == 1


def test_reconcile_exits_one_because_the_gate_closed_with_no_fail_on_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 has two independent causes, and one run with --fail-on triggers both at once.

    Tested jointly, either branch can be deleted and the other keeps the suite green. This is
    the gate on its own, which is the branch the README's CI wiring leans on: a bare
    `quidz reconcile --db PATH` has to refuse while outbound money movement is blocked.
    """
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    report_path = tmp_path / "report.json"
    code = main(["reconcile", "--db", str(db_path), "--json", str(report_path)])
    severities = {
        finding["severity"] for finding in json.loads(report_path.read_text())["findings"]
    }
    assert (code, "critical" in severities) == (1, True)
    assert "outbound      BLOCKED" in capsys.readouterr().out


def test_reconcile_exits_one_on_a_finding_at_the_fail_on_threshold_while_the_gate_is_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other cause, isolated: the threshold the operator chose, not the gate.

    One skewed capture is a single BREAK below both materiality thresholds, so the gate stays
    open and nothing but --fail-on can produce a non zero code here.
    """
    db_path = run_demo(tmp_path, "--break", "amount-mismatch")
    code = main(["reconcile", "--db", str(db_path), "--fail-on", "break"])
    assert (code, "outbound      open" in capsys.readouterr().out) == (1, True)


def test_reconcile_exits_zero_when_the_gate_is_open_and_no_finding_reaches_the_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same report as above, which carries a BREAK: neither a bare run nor one asking about
    # criticals may refuse on it, or the two branches above would pass by always returning 1.
    db_path = run_demo(tmp_path, "--break", "amount-mismatch")
    codes = [
        main(["reconcile", "--db", str(db_path)]),
        main(["reconcile", "--db", str(db_path), "--fail-on", "critical"]),
    ]
    capsys.readouterr()
    assert codes == [0, 0]


def test_the_http_transport_drives_the_same_pipeline_as_the_in_process_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The README calls `quidz demo --http` what CI runs, so CI has to actually run it.

    Nothing in the suite passed the flag, which left the ASGI client, both webhook routes and
    the status code mapping in _receive_over_http unexercised: the sentence carrying this
    repository's argument against bound servers in tests was the one sentence a reader could
    falsify with grep. Comparing the two transports rather than reading the label back is what
    makes a delivery posted to the wrong route, or a status code counted in the wrong tally,
    fail here rather than pass quietly, because every line of a run this adversarial has to
    come out the same whichever way the bytes arrived.
    """
    pytest.importorskip("fastapi", reason="the server extra is not installed")
    pytest.importorskip("httpx", reason="--http drives the app through httpx's ASGI transport")

    direct, over_http = tmp_path / "direct" / "quidz.db", tmp_path / "http" / "quidz.db"
    assert main(["demo", "--scenario", "adversarial", "--db", str(direct)]) == 0
    direct_out = capsys.readouterr().out.replace(str(direct), "DB")
    assert main(["demo", "--scenario", "adversarial", "--http", "--db", str(over_http)]) == 0
    http_out = capsys.readouterr().out.replace(str(over_http), "DB")

    assert "  transport     in-process ASGI\n" in http_out
    assert http_out.replace("in-process ASGI", "in-process, no socket") == direct_out


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


def test_replay_refuses_a_log_that_carries_no_delivery_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A terminal line with nothing to replay against it: valid JSONL, valid schema, and the
    # kind of file a log rotation gone wrong could produce.
    log = tmp_path / "empty.jsonl"
    log.write_text(json.dumps({"kind": "terminal", "payments": {}}) + "\n", encoding="utf-8")
    code = main(["replay", str(log), "--db", str(tmp_path / "replayed.db")])
    assert (code, "carries no delivery records" in capsys.readouterr().err) == (2, True)


def test_replay_with_assert_terminal_refuses_a_log_that_never_recorded_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --assert-terminal has nothing to assert against here, which is a different defect from
    # the mismatch the sibling test below covers, and from the log-with-no-deliveries case
    # above: this log has deliveries, just no recorded terminal state at all.
    db_path = run_demo(tmp_path)
    capsys.readouterr()
    records = [
        line
        for line in Path(f"{db_path}.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] != "terminal"
    ]
    assert records, "the demo run logged no deliveries to replay"
    stripped = tmp_path / "no_terminal.jsonl"
    stripped.write_text("\n".join(records) + "\n", encoding="utf-8")
    code = main(
        ["replay", str(stripped), "--db", str(tmp_path / "replayed.db"), "--assert-terminal"]
    )
    assert (code, "records no terminal state to assert against" in capsys.readouterr().err) == (
        2,
        True,
    )


def test_reconcile_without_a_provider_snapshot_says_to_run_demo_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Absent is not the same defect as unreadable: the sibling test above corrupts the bytes
    # of a snapshot that exists, this one deletes the snapshot demo just wrote, and the two
    # used to produce the same message because nothing distinguished them.
    db_path = run_demo(tmp_path)
    capsys.readouterr()
    Path(f"{db_path}.remote.json").unlink()
    code = main(["reconcile", "--db", str(db_path)])
    err = capsys.readouterr().err
    assert (code, "no provider snapshot beside" in err, "is not readable" in err) == (
        2,
        True,
        False,
    )


def test_replay_reproduces_the_terminal_state_of_the_run_that_logged_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    capsys.readouterr()
    code = main(
        ["replay", f"{db_path}.jsonl", "--db", str(tmp_path / "replayed.db"), "--assert-terminal"]
    )
    assert (code, "terminal state matches the log" in capsys.readouterr().out) == (0, True)


def test_replay_exits_one_when_the_replayed_terminal_state_differs_from_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The half of --assert-terminal that the claim actually rests on.

    The matching case above passes whether the mismatch branch returns 1 or 0, so on its own it
    proves only that a good log is accepted. A tampered terminal record is what a ledger that
    no longer reproduces itself looks like from here, and the README names exit 1 for it.
    """
    db_path = run_demo(tmp_path, "--scenario", "adversarial")
    capsys.readouterr()
    log = Path(f"{db_path}.jsonl")
    lines = log.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])
    assert record["kind"] == "terminal" and record["payments"]
    payment_id, recorded = sorted(record["payments"].items())[0]
    # A terminal state this delivery stream cannot reach, so the divergence is a real one
    # rather than an ordering the ledger is meant to tolerate.
    tampered = "voided" if recorded == "refunded" else "refunded"
    record["payments"][payment_id] = tampered
    lines[-1] = json.dumps(record, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code = main(["replay", str(log), "--db", str(tmp_path / "replayed.db"), "--assert-terminal"])
    output = capsys.readouterr().out
    assert (code, "TERMINAL STATE MISMATCH" in output) == (1, True)
    # The line for this payment, not the whole report: the LEDGER block above prints a line
    # starting with the same id, and every status in the report appears somewhere in it.
    block = output.split("TERMINAL STATE MISMATCH", 1)[1]
    named = next(line for line in block.splitlines() if line.startswith(f"  {payment_id}"))
    assert f"log {tampered}" in named
    assert f"replay {recorded}" in named
