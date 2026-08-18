from __future__ import annotations

import ast
import dataclasses
import importlib
import pkgutil
import subprocess
from pathlib import Path
from types import ModuleType

from quidz.reconcile import GatePolicy, gate, to_json
from test_reconcile import local, remote, run

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "quidz"
SERVER_ONLY = {"fastapi", "starlette", "pydantic", "uvicorn"}
SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}


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


def package_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []
    for found in pkgutil.iter_modules([str(PACKAGE)]):
        try:
            modules.append(importlib.import_module(f"quidz.{found.name}"))
        except ImportError:
            # quidz.app is the one module that needs the optional server extra. Everything
            # else is standard library only, so nothing else can be skipped by this.
            continue
    return modules


def dataclass_defaults() -> list[tuple[str, str, object]]:
    """(class, field, default) for every plain default on every dataclass in the package."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, object]] = []
    for module in package_modules():
        for value in vars(module).values():
            if not isinstance(value, type) or not dataclasses.is_dataclass(value):
                continue
            if not value.__module__.startswith("quidz."):
                continue
            for entry in dataclasses.fields(value):
                key = (value.__qualname__, entry.name)
                if entry.default is dataclasses.MISSING or key in seen:
                    continue
                seen.add(key)
                out.append((value.__qualname__, entry.name, entry.default))
    return out


def test_only_the_adapter_reaches_for_a_web_framework() -> None:
    offenders = {
        module.name: sorted(SERVER_ONLY & top_level_imports(module))
        for module in sorted(PACKAGE.glob("*.py"))
        if module.name != "app.py" and SERVER_ONLY & top_level_imports(module)
    }
    model_imports = top_level_imports(PACKAGE / "model.py")
    assert (offenders, "sqlite3" in model_imports) == ({}, False)


def test_every_tracked_file_is_pure_ascii() -> None:
    """A non-ASCII codepoint in source is where a homoglyph or a bidi override hides.

    Trojan Source, CVE-2021-42574: a right-to-left override reorders how a reviewer reads a
    line without changing how the interpreter runs it, and a Cyrillic letter that draws like a
    Latin one makes two distinct identifiers look like one. Both survive code review by
    construction and neither survives a byte scan.

    It buys a second thing cheaply. The report renderers lay out fixed-width columns that an
    on-call reads at 3am, and a codepoint whose display width the terminal disagrees about
    takes the alignment of every column after it.
    """
    checked: list[Path] = []
    offenders: list[str] = []
    for path in tracked_files():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        checked.append(path)
        offending = next((index for index, byte in enumerate(data) if byte > 0x7F), None)
        if offending is not None:
            offenders.append(f"{path.relative_to(ROOT)} byte {offending}")
    # Proves the scan reached the package rather than an empty file list, which is what a
    # git ls-files that returns nothing would silently look like.
    assert PACKAGE / "money.py" in checked
    assert offenders == []


def test_no_dataclass_default_is_one_python_3_11_refuses_to_import() -> None:
    """3.11 rejects any dataclass default whose class is unhashable; 3.12 relaxed the rule.

    A MappingProxyType default therefore imports on 3.12 and up and raises ValueError at class
    creation on 3.11, which is a supported version here and a required CI leg, so the package
    is uninstallable there while every local run stays green. This reproduces 3.11's own rule
    on whatever interpreter runs the suite, which is the only way one leg's failure is visible
    from the others.
    """
    defaults = dataclass_defaults()
    # Proves the scan reaches the class the bug was in, so an empty result cannot pass quietly.
    assert ("GatePolicy", "fee_tolerance_bps") in {(owner, name) for owner, name, _ in defaults}
    offenders = [
        f"{owner}.{name}" for owner, name, default in defaults if default.__class__.__hash__ is None
    ]
    assert offenders == []


def test_the_json_report_is_byte_stable_across_runs() -> None:
    policy = GatePolicy()
    outputs = set()
    for _ in range(2):
        report = run(ledger=[local(captured=800)], provider=[remote(captured=1000)])
        outputs.add(to_json(report, gate(report, policy=policy, now=report.generated_at)))
    assert len(outputs) == 1
