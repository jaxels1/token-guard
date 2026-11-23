#!/usr/bin/env python3
"""
token-guard cmd_filter: Rewrites noisy single shell commands through a fast
output compressor (or `rtk` when available), preserving errors and exit codes
and spilling full unfiltered logs to a temp file whenever output is trimmed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NOISY_LINE_PATTERNS = (
    re.compile(r"^\s*\.\.\.\s*ok\s*$", re.I),
    re.compile(r"^test\s+.+\.\.\.\s+ok\s*$", re.I),
    re.compile(r"^\s*✓\s+"),
    re.compile(r"^\s*PASS\s+"),
    re.compile(r"^\s*(?:Compiling|Downloading|Fetching|Fresh|Resolving)\s+"),
    re.compile(r"^\s*(?:Collecting|Using cached|Requirement already satisfied)\s+"),
)
KEEP_LINE_RE = re.compile(
    r"(?:error|fail(?:ed|ure)?|exception|traceback|warn(?:ing)?|fatal|panic|assert|"
    r"passed|skipped|summary|total|:\d+:\d+|line\s+\d+|SyntaxError|TypeError|AssertionError)",
    re.I,
)

# Never rewrite commands containing chaining, pipes, redirects, or command substitution
UNSAFE_SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "$(", "`", "\n")

NOISY_CMD_REGEXES = (
    re.compile(r"^(?:py\.?test|pytest)\b", re.I),
    re.compile(r"^(?:python3?|py)\s+-m\s+(?:pytest|unittest)\b", re.I),
    re.compile(r"^(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|typecheck)|install|ci)\b", re.I),
    re.compile(r"^(?:npx\s+)?(?:vitest|jest|eslint|tsc|next\s+build|vite\s+build)\b", re.I),
    re.compile(r"^(?:ruff\s+check|mypy)\b", re.I),
    re.compile(r"^cargo\s+(?:test|build|check|clippy)\b", re.I),
    re.compile(r"^go\s+test\b", re.I),
    re.compile(r"^dotnet\s+test\b", re.I),
    re.compile(r"^pip3?\s+install\b", re.I),
    re.compile(r"^git\s+(?:log|diff)\b", re.I),
)

RTK_SUPPORTED_PREFIXES = (
    "pytest",
    "cargo",
    "git",
    "vitest",
    "jest",
    "eslint",
    "tsc",
    "npm",
    "go",
    "ruff",
)


def default_log_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "token-guard-logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_metrics_file() -> Path:
    base = os.environ.get("PLUGIN_DATA") or os.path.join(
        tempfile.gettempdir(), "token-guard-state"
    )
    d = Path(base)
    d.mkdir(parents=True, exist_ok=True)
    return d / "metrics.jsonl"


def _append_metric(record: dict) -> None:
    try:
        mf = default_metrics_file()
        with open(mf, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **record}) + "\n")
    except OSError:
        pass


def is_noisy_command(command_line: str) -> bool:
    cmd = (command_line or "").strip()
    if not cmd:
        return False
    lower = cmd.lower()
    if "cmd_filter.py" in lower or lower.startswith("rtk "):
        return False
    if any(tok in cmd for tok in UNSAFE_SHELL_TOKENS):
        return False
    if re.match(r"^git\s+(?:log|diff)\b", cmd, re.I) and (
        "--stat" in cmd or "--name-only" in cmd or "--oneline" in cmd
    ):
        return False
    return any(rx.match(cmd) for rx in NOISY_CMD_REGEXES)


def maybe_rewrite_command(
    command_line: str,
    *,
    script_path: str | None = None,
    prefer_rtk: bool = True,
) -> str | None:
    """Return filtered command string if `command_line` is a noisy single command, else None."""
    if not is_noisy_command(command_line):
        return None

    stripped = command_line.strip()
    first_tok = stripped.split()[0].lower()
    if prefer_rtk and first_tok in RTK_SUPPORTED_PREFIXES and shutil.which("rtk"):
        return f"rtk {stripped}"

    filter_path = os.path.abspath(script_path or __file__)
    b64 = base64.urlsafe_b64encode(stripped.encode("utf-8")).decode("ascii")
    return f'python3 "{filter_path}" --run-b64 {b64}'


def _resolve_carriage_returns(text: str) -> str:
    resolved_lines: list[str] = []
    for raw_line in text.splitlines():
        if "\r" in raw_line:
            parts = raw_line.split("\r")
            final = next((p for p in reversed(parts) if p != ""), "")
            resolved_lines.append(final)
        else:
            resolved_lines.append(raw_line)
    return "\n".join(resolved_lines)


def filter_output(
    raw_text: str,
    command_line: str = "",
    *,
    max_lines: int = 85,
    max_chars: int = 5500,
    log_dir: str | Path | None = None,
) -> tuple[str, dict]:
    """
    Strip ANSI noise & repetitive passing lines while preserving errors,
    tracebacks, summaries, and full raw log spill when truncated.
    """
    raw_bytes = len(raw_text.encode("utf-8", errors="replace"))
    cleaned = ANSI_ESCAPE_RE.sub("", raw_text)
    cleaned = _resolve_carriage_returns(cleaned)

    # Collapse 2+ blank lines into single blank line
    lines_in = cleaned.splitlines()
    collapsed: list[str] = []
    blank_run = 0
    for line in lines_in:
        if not line.strip():
            blank_run += 1
            if blank_run == 1:
                collapsed.append("")
        else:
            blank_run = 0
            collapsed.append(line.rstrip())

    # Drop low-signal passing/progress boilerplate on long outputs
    if len(collapsed) > 35:
        tail_keep_from = max(0, len(collapsed) - 18)
        filtered_lines: list[str] = []
        dropped_boilerplate = 0
        for idx, line in enumerate(collapsed):
            if idx >= tail_keep_from or KEEP_LINE_RE.search(line):
                filtered_lines.append(line)
            elif any(p.match(line) for p in NOISY_LINE_PATTERNS):
                dropped_boilerplate += 1
            else:
                filtered_lines.append(line)
    else:
        filtered_lines = collapsed
        dropped_boilerplate = 0

    truncated = False
    if len(filtered_lines) > max_lines:
        head = filtered_lines[:24]
        tail = filtered_lines[-42:]
        middle_errors = [
            l for l in filtered_lines[24:-42] if KEEP_LINE_RE.search(l)
        ][:20]
        omitted = len(filtered_lines) - len(head) - len(tail) - len(middle_errors)
        filtered_lines = [
            *head,
            *(["... [error lines preserved] ...", *middle_errors] if middle_errors else []),
            f"... [{max(0, omitted)} lines omitted by token-guard] ...",
            *tail,
        ]
        truncated = True

    result_text = "\n".join(filtered_lines).strip()
    if len(result_text) > max_chars:
        half_head = result_text[:2000]
        half_tail = result_text[-2800:]
        result_text = f"{half_head}\n... [truncated by token-guard] ...\n{half_tail}"
        truncated = True

    out_bytes = len(result_text.encode("utf-8", errors="replace"))
    saved_bytes = max(0, raw_bytes - out_bytes)

    log_path_str: str | None = None
    if truncated or (dropped_boilerplate > 0 and saved_bytes > 512) or saved_bytes > 2048:
        target_dir = Path(log_dir) if log_dir else default_log_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{time.time()}:{command_line}".encode("utf-8")).hexdigest()[:10]
        log_file = target_dir / f"{int(time.time())}-{digest}.log"
        try:
            log_file.write_text(raw_text, encoding="utf-8", errors="replace")
            log_path_str = str(log_file)
        except OSError:
            log_path_str = None

        pct = int(round((saved_bytes / raw_bytes) * 100)) if raw_bytes > 0 else 0
        footer = f"\n[token-guard: stripped {saved_bytes} bytes ({pct}%)"
        if log_path_str:
            footer += f", full log: {log_path_str}"
        footer += "]"
        result_text = (result_text + footer).strip()
        out_bytes = len(result_text.encode("utf-8", errors="replace"))
        saved_bytes = max(0, raw_bytes - out_bytes)

    meta = {
        "raw_bytes": raw_bytes,
        "filtered_bytes": out_bytes,
        "saved_bytes": saved_bytes,
        "truncated": truncated,
        "dropped_boilerplate": dropped_boilerplate,
        "log_path": log_path_str,
    }
    return result_text, meta


def run_filtered_command(command_line: str) -> int:
    proc = subprocess.run(
        command_line,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    compressed, meta = filter_output(proc.stdout or "", command_line)
    if compressed:
        print(compressed)
    _append_metric({
        "event": "cmd_filtered",
        "command": command_line[:160],
        "raw_bytes": meta["raw_bytes"],
        "filtered_bytes": meta["filtered_bytes"],
        "saved_bytes": meta["saved_bytes"],
        "returncode": proc.returncode,
    })
    return int(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) >= 2 and args[0] == "--run-b64":
        try:
            cmd = base64.urlsafe_b64decode(args[1].encode("ascii")).decode("utf-8")
        except Exception as exc:
            print(f"token-guard invalid --run-b64 payload: {exc}", file=sys.stderr)
            return 2
        return run_filtered_command(cmd)

    if "--run" in args:
        idx = args.index("--run")
        rest = args[idx + 1 :]
        if rest and rest[0] == "--":
            rest = rest[1:]
        if not rest:
            return 2
        return run_filtered_command(" ".join(rest))

    raw = sys.stdin.read()
    compressed, _ = filter_output(raw)
    if compressed:
        print(compressed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
