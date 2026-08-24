"""Capture the demo's real output and the numbers the Pages card states.

WHY THIS EXISTS. The card at pnx89.github.io/QUIDZ shows the output of a real run and four
numbers about this repository. Both are committed, which means both can go stale.
`tests/test_docs.py` fails when what is committed stops matching a live run.

THE DATABASE IS DELETED FIRST, AND THAT IS NOT A CONVENIENCE. This demo applies deliveries to
a ledger, and the ledger is idempotent on purpose, so a second run against the same file is a
run in which nothing happens. Capturing that would publish a page saying the adversarial
scenario found nothing, which is the opposite of what it does find. The README's own path is
used so the two blocks describe the same run.

    uv run python scripts/capture_evidence.py
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "docs" / "evidence"
DB = pathlib.Path("/tmp/quidz-demo/quidz.db")
# Invoked through the module's own entry point rather than "-m quidz": the package ships a
# console script and has no __main__, so "-m" is the spelling that does not work.
DEMO = [
    sys.executable,
    "-c",
    "import sys;from quidz.cli import main;sys.exit(main())",
    "demo",
    "--scenario",
    "adversarial",
    "--db",
    str(DB),
]


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, cwd=ROOT, timeout=600)
    if result.returncode:
        raise SystemExit(f"{args[0]} failed: {result.stderr.strip()[:400]}")
    return result.stdout


def test_total() -> int:
    out = run(sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q")
    match = re.search(r"^(\d+) tests? collected", out, re.MULTILINE)
    if not match:
        raise SystemExit(f"could not read a collection total from:\n{out[-400:]}")
    return int(match.group(1))


def python_range() -> str:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = sorted({v for v in re.findall(r'"(3\.\d+)"', workflow)}, key=lambda v: int(v[2:]))
    if not versions:
        raise SystemExit("no Python versions found in the CI matrix")
    return f"{versions[0]} to {versions[-1]}"


def release() -> str:
    from quidz import __version__

    tag = f"v{__version__}"
    described = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"], capture_output=True, text=True, cwd=ROOT
    )
    if described.returncode == 0 and described.stdout.strip() != tag:
        raise SystemExit(
            f"the newest tag is {described.stdout.strip()} but the version is {__version__}"
        )
    return tag


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)

    output = run(*DEMO)
    if not output.strip():
        raise SystemExit("the demo produced no output, refusing to write empty evidence")
    if "/Users/" in output or "/var/folders/" in output:
        raise SystemExit("the demo output carries a machine specific path, refusing")
    (EVIDENCE / "demo.txt").write_text(output, encoding="utf-8")

    run_id = os.environ.get("GITHUB_RUN_ID")
    slug = os.environ.get("GITHUB_REPOSITORY", "PNX89/QUIDZ")
    facts = {
        "tests": test_total(),
        "python": python_range(),
        "release": release(),
        "captured": datetime.date.today().isoformat(),
        "runUrl": f"https://github.com/{slug}/actions/runs/{run_id}" if run_id else None,
    }
    (EVIDENCE / "facts.json").write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {EVIDENCE / 'demo.txt'} ({len(output.splitlines())} lines)")
    print(f"wrote {EVIDENCE / 'facts.json'} {facts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
