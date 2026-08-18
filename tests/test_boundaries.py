from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from quidz.reconcile import GatePolicy, gate, to_json
from test_reconcile import local, remote, run

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "quidz"
SERVER_ONLY = {"fastapi", "starlette", "pydantic", "uvicorn"}
SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
# The escape sequences, so this file stays free of the codepoints it is looking for.
EM_DASH = "\u2014"
EN_DASH = "\u2013"


def top_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode == 0 and result.stdout:
        return [ROOT / name for name in result.stdout.split("\0") if name]
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not SKIP_DIRECTORIES.intersection(path.parts)
    ]


def test_only_the_adapter_reaches_for_a_web_framework() -> None:
    offenders = {
        module.name: sorted(SERVER_ONLY & top_level_imports(module))
        for module in sorted(PACKAGE.glob("*.py"))
        if module.name != "app.py" and SERVER_ONLY & top_level_imports(module)
    }
    model_imports = top_level_imports(PACKAGE / "model.py")
    assert (offenders, "sqlite3" in model_imports) == ({}, False)


def test_no_tracked_file_carries_an_em_dash_or_an_en_dash() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if EM_DASH in text or EN_DASH in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_the_json_report_is_byte_stable_across_runs() -> None:
    policy = GatePolicy()
    outputs = set()
    for _ in range(2):
        report = run(ledger=[local(captured=800)], provider=[remote(captured=1000)])
        outputs.add(to_json(report, gate(report, policy=policy, now=report.generated_at)))
    assert len(outputs) == 1
