#!/usr/bin/env python3
"""
token-guard outline_gen: Generates a compact structural skeleton (imports,
classes, functions/methods, and exact line numbers) for oversized (>500-line)
files using Python's stdlib `ast` module for .py files and fast regex heuristics
for JS/TS/TSX/Go/Rust/Java/C/CSS/Markdown files.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

REGEX_SYMBOL_PATTERNS = (
    re.compile(
        r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?(?:function|class|interface|type|enum|const|let)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
    ),
    re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:func|type)\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)*\b(?:class|interface|enum|void|[A-Za-z0-9_<>]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("),
    re.compile(r"^(#{1,3}\s+.+)$"),
)


def _python_ast_outline(source: str, max_items: int = 45) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    items: list[str] = []
    imports: list[int] = []

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_l = getattr(node, "end_lineno", node.lineno)
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            args = [a.arg for a in node.args.args[:4]]
            if len(node.args.args) > 4:
                args.append("...")
            items.append(f"  L{node.lineno}-{end_l}: {prefix} {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            end_l = getattr(node, "end_lineno", node.lineno)
            items.append(f"  L{node.lineno}-{end_l}: class {node.name}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    sub_end = getattr(sub, "end_lineno", sub.lineno)
                    items.append(f"    L{sub.lineno}-{sub_end}: .{sub.name}()")

    header: list[str] = []
    if imports:
        header.append(f"  L{min(imports)}-{max(imports)}: [imports ({len(imports)} statements)]")
    combined = header + items
    if len(combined) > max_items:
        omitted = len(combined) - max_items
        combined = combined[:max_items] + [f"  ... (+{omitted} more symbols)"]
    return combined


def _regex_outline(source: str, max_items: int = 45) -> list[str]:
    lines = source.splitlines()
    items: list[str] = []
    for idx, line in enumerate(lines, start=1):
        if len(line) > 240:
            continue
        for pat in REGEX_SYMBOL_PATTERNS:
            m = pat.match(line)
            if m:
                snippet = line.strip().rstrip("{;").strip()
                if len(snippet) > 78:
                    snippet = snippet[:75] + "..."
                items.append(f"  L{idx}: {snippet}")
                break

    if len(items) > max_items:
        omitted = len(items) - max_items
        items = items[:max_items] + [f"  ... (+{omitted} more symbols)"]
    return items


def generate_file_outline(file_path: str | Path, max_items: int = 40) -> str:
    """Return a compact structural outline with line numbers for an oversized file."""
    p = Path(file_path)
    if not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    total_lines = raw.count("\n") + 1
    ext = p.suffix.lower()
    entries: list[str] = []
    if ext in (".py", ".pyw"):
        entries = _python_ast_outline(raw, max_items=max_items)
    if not entries:
        entries = _regex_outline(raw, max_items=max_items)
    if not entries:
        # Fallback: section checkpoints every ~100 lines
        lines = raw.splitlines()
        for ln in range(1, len(lines) + 1, max(50, len(lines) // 10)):
            preview = lines[ln - 1].strip()[:65]
            if preview:
                entries.append(f"  L{ln}: {preview}")

    body = "\n".join(entries[:max_items])
    return f"Structural outline of {os.path.basename(str(file_path))} ({total_lines} lines):\n{body}"
