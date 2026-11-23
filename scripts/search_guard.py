#!/usr/bin/env python3
"""
token-guard search_guard: Guards Grep / Search / Find tool calls against
monorepo search explosions across node_modules, build artifacts, lockfiles,
and minified bundles, injecting safe exclusion globs and head_limit caps.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from read_guard import LOCKFILE_NAMES, MINIFIED_SUFFIXES, VENDOR_DIR_SEGMENTS, append_metric, default_state_dir

DEFAULT_EXCLUDE_GLOBS = [
    "!**/node_modules/**",
    "!**/.next/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/target/**",
    "!**/.venv/**",
    "!**/coverage/**",
    "!**/package-lock.json",
    "!**/yarn.lock",
    "!**/pnpm-lock.yaml",
    "!**/Cargo.lock",
    "!**/*.min.js",
    "!**/*.min.css",
    "!**/*.bundle.js",
    "!**/*.map",
]


def evaluate_search(
    tool_name: str,
    args: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
    default_head_limit: int = 80,
) -> dict[str, Any]:
    """
    Validate or tighten a Grep / grep_search / find_by_name / Glob tool call.
    Returns: {"allow": bool, "reason": str, "overwrite": dict | None}
    """
    s_dir = Path(state_dir) if state_dir else default_state_dir()
    search_path = str(
        args.get("SearchPath")
        or args.get("SearchDirectory")
        or args.get("path")
        or args.get("dir")
        or ""
    )

    if search_path:
        base = os.path.basename(search_path).lower()
        parts = {seg.lower() for seg in Path(search_path).parts}
        if base in LOCKFILE_NAMES or any(base.endswith(sfx) for sfx in MINIFIED_SUFFIXES):
            append_metric(s_dir, {
                "event": "search_blocked",
                "code": "search-target-bomb",
                "path": search_path,
                "saved_bytes": 16000,
            })
            return {
                "allow": False,
                "reason": (
                    f"token-guard blocked unrestrained search directly on lockfile/minified asset '{os.path.basename(search_path)}'."
                ),
                "overwrite": None,
            }
        hit_vendor = parts & VENDOR_DIR_SEGMENTS
        if hit_vendor:
            append_metric(s_dir, {
                "event": "search_blocked",
                "code": "search-vendor-dir",
                "path": search_path,
                "saved_bytes": 20000,
            })
            return {
                "allow": False,
                "reason": (
                    f"token-guard blocked search inside vendor/build directory ({', '.join(sorted(hit_vendor))}). "
                    "Target source directories instead."
                ),
                "overwrite": None,
            }

    overwrite: dict[str, Any] = {}

    # Claude Code / Cursor `Grep` tool supports `head_limit` and `glob`
    if tool_name == "Grep":
        if not args.get("head_limit"):
            overwrite["head_limit"] = default_head_limit

    # Antigravity `find_by_name` supports `Excludes` array and `MaxDepth`
    if tool_name == "find_by_name":
        existing_excludes = args.get("Excludes")
        if not existing_excludes:
            overwrite["Excludes"] = [
                "node_modules",
                ".next",
                "dist",
                "build",
                "target",
                ".venv",
                "coverage",
            ]

    return {
        "allow": True,
        "reason": "",
        "overwrite": overwrite if overwrite else None,
    }


def filter_search_stdout(raw_text: str, max_matches: int = 80) -> tuple[str, dict[str, Any]]:
    """
    Filter raw `rg` / `grep` / `find` stdout to strip vendor/lockfile/minified
    noise and cap match lines at `max_matches` with a file/match summary.
    """
    raw_bytes = len(raw_text.encode("utf-8", errors="replace"))
    kept: list[str] = []
    omitted_matches = 0
    dropped_vendor = 0
    omitted_files: set[str] = set()

    for line in raw_text.splitlines():
        lower = line.lower()
        if (
            "node_modules" in lower
            or "/dist/" in lower
            or "\\dist\\" in lower
            or "/.next/" in lower
            or "\\.next\\" in lower
            or "package-lock.json" in lower
            or "pnpm-lock.yaml" in lower
            or "yarn.lock" in lower
            or ".min.js" in lower
            or ".bundle.js" in lower
        ):
            dropped_vendor += 1
            continue

        if len(kept) < max_matches:
            kept.append(line[:220] + ("..." if len(line) > 220 else ""))
        else:
            omitted_matches += 1
            file_prefix = line.split(":", 1)[0]
            if file_prefix:
                omitted_files.add(file_prefix)

    if omitted_matches > 0 or dropped_vendor > 0:
        summary_parts = []
        if omitted_matches > 0:
            f_count = max(1, len(omitted_files))
            summary_parts.append(f"...and {omitted_matches} more matches across {f_count} files")
        if dropped_vendor > 0:
            summary_parts.append(f"{dropped_vendor} vendor/lockfile/minified lines excluded")
        kept.append(f"[token-guard search cap: {'; '.join(summary_parts)}]")

    out_text = "\n".join(kept)
    out_bytes = len(out_text.encode("utf-8", errors="replace"))
    return out_text, {
        "raw_bytes": raw_bytes,
        "filtered_bytes": out_bytes,
        "saved_bytes": max(0, raw_bytes - out_bytes),
        "omitted_matches": omitted_matches,
        "dropped_vendor": dropped_vendor,
    }
