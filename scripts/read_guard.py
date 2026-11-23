#!/usr/bin/env python3
"""
token-guard read_guard: Blocks duplicate unchanged file reads and unbounded
reads of lockfiles, minified bundles, build directories, or oversized files.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.lock",
    "poetry.lock",
    "uv.lock",
    "go.sum",
    "gemfile.lock",
    "composer.lock",
    "bun.lock",
    "bun.lockb",
}

MINIFIED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".bundle.js",
    ".chunk.js",
    ".js.map",
    ".css.map",
)

VENDOR_DIR_SEGMENTS = {
    "node_modules",
    ".next",
    "dist",
    "build",
    "out",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "coverage",
}

MAX_RANGE_SPAN = 10**12


def default_state_dir() -> Path:
    base = os.environ.get("PLUGIN_DATA") or os.path.join(
        tempfile.gettempdir(), "token-guard-state"
    )
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_path(raw_path: str) -> str:
    try:
        resolved = os.path.realpath(raw_path)
    except OSError:
        resolved = os.path.abspath(raw_path)
    return os.path.normcase(resolved)


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    if not ranges:
        return []
    ordered = sorted((min(a, b), max(a, b)) for a, b in ranges)
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for s, e in ordered[1:]:
        if s <= merged[-1][1] + 1:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return merged


def _count_lines_capped(file_path: str, limit: int) -> tuple[int, bool]:
    """Return (count, exceeded_limit) without loading huge files into RAM."""
    lines = 0
    try:
        with open(file_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                lines += chunk.count(b"\n")
                if lines > limit:
                    return lines, True
        return lines + (1 if os.path.getsize(file_path) > 0 else 0), False
    except OSError:
        return 0, False


def _estimate_range_bytes(file_path: str, req_start: int, req_end: int, file_size: int) -> int:
    if req_start <= 1 and req_end >= MAX_RANGE_SPAN // 2:
        return max(file_size, 1024)
    span_lines = max(1, req_end - req_start + 1)
    # Approximate ~45 bytes/line capped at actual file_size
    return min(max(file_size, 512), span_lines * 45)


def append_metric(state_dir: Path, record: dict[str, Any]) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        record_with_ts = {"ts": time.time(), **record}
        metrics_file = state_dir / "metrics.jsonl"
        with open(metrics_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record_with_ts) + "\n")
    except OSError:
        pass


def _state_file(state_dir: Path, conversation_id: str) -> Path:
    digest = hashlib.sha256((conversation_id or "default").encode("utf-8")).hexdigest()[:24]
    return state_dir / f"reads-{digest}.json"


def _load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"files": {}}
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"files": {}}


def _save_state(state_path: Path, data: dict[str, Any]) -> None:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_path, state_path)
    except OSError:
        pass


def _detect_compaction_marker(transcript_path: str | None) -> str | None:
    """Inspect the tail of transcript.jsonl for the latest compaction / summary marker."""
    if not transcript_path:
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > 65536:
                fh.seek(size - 65536)
            tail = fh.read().decode("utf-8", errors="replace")
        for line in reversed(tail.splitlines()):
            if '"compacted"' in line or "<CONTEXT_SUMMARY>" in line or '"context_compaction"' in line:
                return hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()[:16]
    except OSError:
        pass
    return None


def evaluate_read(
    conversation_id: str,
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    *,
    state_dir: str | Path | None = None,
    max_unbounded_lines: int = 500,
    max_unbounded_bytes: int = 40960,
    step_idx: int | None = None,
    transcript_path: str | None = None,
    max_step_age: int = 40,
    max_age_seconds: float = 900.0,
) -> dict[str, Any]:
    """
    Validate a file read request.
    Returns: {"allow": bool, "code": str, "reason": str, "estimated_saved_bytes": int}
    """
    if not file_path or not isinstance(file_path, str):
        return {"allow": True, "code": "no-path", "reason": "", "estimated_saved_bytes": 0}

    s_dir = Path(state_dir) if state_dir else default_state_dir()
    norm = normalize_path(file_path)
    lower_name = os.path.basename(norm).lower()
    parts = {seg.lower() for seg in Path(norm).parts}

    unbounded = start_line is None and end_line is None

    try:
        st = os.stat(file_path)
        exists = os.path.isfile(file_path)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        file_size = int(st.st_size)
    except OSError:
        exists = False
        mtime_ns = 0
        file_size = 0

    # 1. Block unbounded token-bomb reads (lockfiles, minified bundles, vendor dirs, huge files)
    if unbounded:
        if lower_name in LOCKFILE_NAMES:
            saved = max(file_size, 24000)
            append_metric(s_dir, {
                "event": "read_blocked",
                "code": "lockfile-unbounded",
                "path": norm,
                "saved_bytes": saved,
            })
            return {
                "allow": False,
                "code": "lockfile-unbounded",
                "reason": (
                    f"token-guard blocked unbounded read of lockfile '{os.path.basename(file_path)}'. "
                    "Use `rg` or pass narrow StartLine/EndLine bounds if needed."
                ),
                "estimated_saved_bytes": saved,
            }

        if any(lower_name.endswith(sfx) for sfx in MINIFIED_SUFFIXES):
            saved = max(file_size, 16000)
            append_metric(s_dir, {
                "event": "read_blocked",
                "code": "minified-unbounded",
                "path": norm,
                "saved_bytes": saved,
            })
            return {
                "allow": False,
                "code": "minified-unbounded",
                "reason": (
                    f"token-guard blocked unbounded read of minified/bundle asset '{os.path.basename(file_path)}'. "
                    "Pass StartLine/EndLine or search targeted symbols instead."
                ),
                "estimated_saved_bytes": saved,
            }

        hit_vendor = parts & VENDOR_DIR_SEGMENTS
        if hit_vendor:
            saved = max(file_size, 12000)
            append_metric(s_dir, {
                "event": "read_blocked",
                "code": "vendor-dir-unbounded",
                "path": norm,
                "saved_bytes": saved,
            })
            return {
                "allow": False,
                "code": "vendor-dir-unbounded",
                "reason": (
                    f"token-guard blocked unbounded read inside build/vendor directory ({', '.join(sorted(hit_vendor))}). "
                    "Pass explicit StartLine/EndLine bounds."
                ),
                "estimated_saved_bytes": saved,
            }

        if exists:
            if file_size > max_unbounded_bytes:
                append_metric(s_dir, {
                    "event": "read_blocked",
                    "code": "oversized-file-bytes",
                    "path": norm,
                    "saved_bytes": file_size,
                })
                return {
                    "allow": False,
                    "code": "oversized-file-bytes",
                    "reason": (
                        f"token-guard blocked unbounded read of {os.path.basename(file_path)} ({file_size} bytes > {max_unbounded_bytes}). "
                        "Use `rg` / `code-index` to locate symbols and read with StartLine/EndLine."
                    ),
                    "estimated_saved_bytes": file_size,
                }

            line_count, exceeded = _count_lines_capped(file_path, max_unbounded_lines)
            if exceeded:
                saved = max(file_size, max_unbounded_lines * 40)
                append_metric(s_dir, {
                    "event": "read_blocked",
                    "code": "oversized-file-lines",
                    "path": norm,
                    "saved_bytes": saved,
                })
                return {
                    "allow": False,
                    "code": "oversized-file-lines",
                    "reason": (
                        f"token-guard blocked unbounded read of {os.path.basename(file_path)} (>{max_unbounded_lines} lines). "
                        "Locate target symbols first and read a bounded <150-line slice."
                    ),
                    "estimated_saved_bytes": saved,
                }

    # 2. Deduplicate unchanged re-reads per (conversation_id, path, range, mtime_ns, size)
    req_start = int(start_line) if start_line is not None else 1
    req_end = int(end_line) if end_line is not None else MAX_RANGE_SPAN

    now_ts = time.time()
    st_path = _state_file(s_dir, conversation_id)
    state = _load_state(st_path)

    # Reset read cache automatically when a new context compaction marker appears in the transcript
    compaction_marker = _detect_compaction_marker(transcript_path)
    if compaction_marker and compaction_marker != state.get("compaction_marker"):
        state["files"] = {}
        state["compaction_marker"] = compaction_marker

    cur_step = int(step_idx) if step_idx is not None else int(state.get("auto_step", 0)) + 1
    state["auto_step"] = cur_step

    entry = state["files"].get(norm)

    if exists and isinstance(entry, dict):
        same_version = entry.get("mtime_ns") == mtime_ns and entry.get("size") == file_size
        age_steps = cur_step - int(entry.get("last_step", cur_step))
        age_seconds = now_ts - float(entry.get("last_ts", now_ts))
        expired_by_ttl = (max_step_age > 0 and age_steps > max_step_age) or (
            max_age_seconds > 0 and age_seconds > max_age_seconds
        )
        if expired_by_ttl:
            entry = None
            state["files"].pop(norm, None)

        if entry and same_version:
            # Escape hatch: if the agent immediately retries the exact same range after a block
            # (e.g. because context compaction dropped the raw lines), allow it on the 2nd attempt.
            if entry.get("last_blocked_range") == [req_start, req_end]:
                entry["last_blocked_range"] = None
                entry["last_step"] = cur_step
                entry["last_ts"] = now_ts
                _save_state(st_path, state)
                return {
                    "allow": True,
                    "code": "compaction-retry-allowed",
                    "reason": "Allowed explicit retry after duplicate block.",
                    "estimated_saved_bytes": 0,
                }

            for prev_start, prev_end in entry.get("ranges", []):
                if prev_start <= req_start and req_end <= prev_end:
                    saved = _estimate_range_bytes(file_path, req_start, req_end, file_size)
                    entry["last_blocked_range"] = [req_start, req_end]
                    _save_state(st_path, state)
                    append_metric(s_dir, {
                        "event": "read_blocked",
                        "code": "duplicate-read",
                        "path": norm,
                        "range": [req_start, req_end],
                        "saved_bytes": saved,
                    })
                    span_label = (
                        f"lines {req_start}-{req_end}"
                        if not unbounded
                        else "full file"
                    )
                    return {
                        "allow": False,
                        "code": "duplicate-read",
                        "reason": (
                            f"token-guard blocked duplicate read of unchanged {os.path.basename(file_path)} ({span_label} already read). "
                            "Reuse earlier context, target an unread range, or retry once if prior context was compacted."
                        ),
                        "estimated_saved_bytes": saved,
                    }

            # Partial overlap trimming for bounded reads (e.g., 1-100 already read, now requesting 50-150 -> trim to 101-150)
            if not unbounded and start_line is not None and end_line is not None:
                trimmed_start = req_start
                trimmed_end = req_end
                for prev_start, prev_end in entry.get("ranges", []):
                    if prev_start <= trimmed_start <= prev_end < trimmed_end:
                        trimmed_start = prev_end + 1
                    elif trimmed_start < prev_start <= trimmed_end <= prev_end:
                        trimmed_end = prev_start - 1

                if (trimmed_start, trimmed_end) != (req_start, req_end) and trimmed_start <= trimmed_end:
                    overlap_lines = (req_end - req_start + 1) - (trimmed_end - trimmed_start + 1)
                    saved = max(256, overlap_lines * 45)
                    append_metric(s_dir, {
                        "event": "read_trimmed",
                        "code": "partial-overlap-trimmed",
                        "path": norm,
                        "original_range": [req_start, req_end],
                        "trimmed_range": [trimmed_start, trimmed_end],
                        "saved_bytes": saved,
                    })
                    prev_ranges = entry.get("ranges", [])
                    state["files"][norm] = {
                        "mtime_ns": mtime_ns,
                        "size": file_size,
                        "ranges": _merge_ranges([*prev_ranges, [trimmed_start, trimmed_end]]),
                        "last_step": cur_step,
                        "last_ts": now_ts,
                        "last_blocked_range": None,
                    }
                    _save_state(st_path, state)
                    return {
                        "allow": True,
                        "code": "partial-overlap-trimmed",
                        "reason": f"Trimmed already-read lines to {trimmed_start}-{trimmed_end}.",
                        "overwrite_range": [trimmed_start, trimmed_end],
                        "estimated_saved_bytes": saved,
                    }

    # Record allowed read
    if exists:
        prev_ranges = (
            entry.get("ranges", [])
            if isinstance(entry, dict)
            and entry.get("mtime_ns") == mtime_ns
            and entry.get("size") == file_size
            else []
        )
        new_ranges = _merge_ranges([*prev_ranges, [req_start, req_end]])
        state["files"][norm] = {
            "mtime_ns": mtime_ns,
            "size": file_size,
            "ranges": new_ranges,
            "last_step": cur_step,
            "last_ts": now_ts,
            "last_blocked_range": None,
        }
        _save_state(st_path, state)

    return {"allow": True, "code": "ok", "reason": "", "estimated_saved_bytes": 0}
