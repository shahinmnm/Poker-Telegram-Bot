"""Audit deprecated GameEngine callsites.

This script scans the codebase for legacy usages of
``GameEngine.progress_stage`` and ``GameEngine.finalize_game`` that still
provide the deprecated ``context`` or ``game`` keyword arguments. A JSON
report is printed to stdout and the exit status is non-zero when any legacy
calls are detected.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


TARGET_DIRS: Tuple[str, ...] = ("pokerapp", "tests")
SKIP_DIR_PARTS: Tuple[str, ...] = ("__pycache__",)
METHODS: Tuple[str, ...] = ("progress_stage", "finalize_game")
DEPRECATED_KWARGS: Tuple[str, ...] = ("context", "game")


def iter_python_files() -> Iterable[Path]:
    for base in TARGET_DIRS:
        root = Path(base)
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if any(part in SKIP_DIR_PARTS for part in path.parts):
                continue
            yield path


def find_call_snippets(source: str, method: str) -> Iterable[Tuple[int, str]]:
    token = f".{method}("
    start = 0
    while True:
        index = source.find(token, start)
        if index == -1:
            break
        call_start = index
        cursor = index + len(token)
        depth = 1
        while cursor < len(source) and depth > 0:
            char = source[cursor]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        snippet = source[call_start:cursor]
        line_number = source.count("\n", 0, call_start) + 1
        yield line_number, snippet.strip()
        start = cursor


def audit_file(path: Path) -> Dict[str, List[Tuple[int, str]]]:
    text = path.read_text(encoding="utf-8")
    findings: Dict[str, List[Tuple[int, str]]] = {}
    for method in METHODS:
        for line_number, snippet in find_call_snippets(text, method):
            if any(re.search(rf"\b{kwarg}\s*=", snippet) for kwarg in DEPRECATED_KWARGS):
                findings.setdefault(method, []).append((line_number, snippet))
    return findings


def main() -> int:
    report: Dict[str, Dict[str, List[Tuple[int, str]]]] = {}
    for file_path in iter_python_files():
        findings = audit_file(file_path)
        if findings:
            report[str(file_path)] = findings

    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report else 0


if __name__ == "__main__":
    sys.exit(main())
