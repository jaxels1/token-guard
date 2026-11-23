#!/usr/bin/env python3
"""
token-guard audit_transcript: Parses Antigravity and Claude Code JSONL transcripts
and optional metrics.jsonl to report turns, duplicate reads, unbounded reads,
command output savings, and net estimated tokens saved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from cmd_filter import filter_output, is_noisy_command  # noqa: E402
from read_guard import LOCKFILE_NAMES, MINIFIED_SUFFIXES, normalize_path  # noqa: E402

READ_TOOLS = {"read_file", "view_file", "Read"}
EDIT_TOOLS = {"edit_file", "write_file", "Edit", "Write", "replace_file_content", "write_to_file"}
CMD_TOOLS = {"run_command", "Bash", "Shell", "run_shell_command"}


def _iter_jsonl(path: Path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    yield obj
            except json.JSONDecodeError:
                continue


def _extract_calls_from_step(obj: dict[str, Any]) -> list[tuple[str, dict[str, Any], int]]:
    """Return list of (tool_name, args, result_bytes_estimate) from a JSONL line."""
    calls: list[tuple[str, dict[str, Any], int]] = []

    # Antigravity compact/full transcript format
    if isinstance(obj.get("tool_calls"), list):
        for tc in obj["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "")
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            out = tc.get("result") or tc.get("output") or ""
            out_bytes = len(str(out).encode("utf-8", errors="replace")) if out else 0
            calls.append((name, args, out_bytes))

    # Antigravity tool response step (`content` on tool_response step)
    if obj.get("type") in ("TOOL_RESPONSE", "tool_result") and isinstance(obj.get("content"), str):
        calls.append(("__tool_output__", {"text": obj["content"]}, len(obj["content"].encode("utf-8"))))

    # Claude Code message.content array
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
    if msg and isinstance(msg.get("content"), list):
        for item in msg["content"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use":
                name = str(item.get("name") or "")
                args = item.get("input") if isinstance(item.get("input"), dict) else {}
                calls.append((name, args, 0))
            elif item.get("type") == "tool_result":
                content = item.get("content") or ""
                text = content if isinstance(content, str) else json.dumps(content)
                calls.append(("__tool_output__", {"text": text}, len(text.encode("utf-8"))))

    return calls


def audit_transcript(
    transcript_path: str | Path,
    metrics_path: str | Path | None = None,
) -> dict[str, Any]:
    t_path = Path(transcript_path)
    if not t_path.is_file():
        raise FileNotFoundError(f"Transcript not found: {transcript_path}")

    turns = 0
    tool_calls = 0
    duplicate_reads = 0
    unbounded_bomb_reads = 0
    duplicate_saved_bytes = 0
    unbounded_saved_bytes = 0
    noisy_cmd_count = 0
    raw_cmd_bytes = 0
    estimated_filtered_cmd_bytes = 0

    read_history: dict[str, list[tuple[int, int]]] = {}
    last_tool_cmd: str | None = None

    for obj in _iter_jsonl(t_path):
        step_type = str(obj.get("type") or "")
        if step_type in ("USER_INPUT", "PLANNER_RESPONSE", "user", "assistant"):
            turns += 1

        for name, args, out_bytes in _extract_calls_from_step(obj):
            if name == "__tool_output__":
                text = str(args.get("text") or "")
                if last_tool_cmd and is_noisy_command(last_tool_cmd):
                    raw_cmd_bytes += out_bytes
                    compressed, _ = filter_output(text, last_tool_cmd)
                    estimated_filtered_cmd_bytes += len(compressed.encode("utf-8", errors="replace"))
                    last_tool_cmd = None
                continue

            tool_calls += 1
            if name in READ_TOOLS:
                raw_p = str(
                    args.get("Path")
                    or args.get("AbsolutePath")
                    or args.get("file_path")
                    or args.get("path")
                    or ""
                )
                if not raw_p:
                    continue
                norm = normalize_path(raw_p)
                lower_base = os.path.basename(norm).lower()
                s = args.get("StartLine") if args.get("StartLine") is not None else args.get("offset")
                e = args.get("EndLine")
                start_l = int(s) if s is not None else 1
                end_l = int(e) if e is not None else 10**12
                unbounded = s is None and e is None

                if unbounded and (
                    lower_base in LOCKFILE_NAMES
                    or any(lower_base.endswith(sfx) for sfx in MINIFIED_SUFFIXES)
                ):
                    unbounded_bomb_reads += 1
                    unbounded_saved_bytes += max(out_bytes, 24000)

                prior = read_history.get(norm, [])
                if any(ps <= start_l and end_l <= pe for ps, pe in prior):
                    duplicate_reads += 1
                    duplicate_saved_bytes += max(out_bytes, min(12000, max(512, (end_l - start_l + 1) * 45)))
                else:
                    prior.append((start_l, end_l))
                    read_history[norm] = prior
                last_tool_cmd = None

            elif name in EDIT_TOOLS:
                raw_p = str(args.get("Path") or args.get("TargetFile") or args.get("file_path") or "")
                if raw_p:
                    read_history.pop(normalize_path(raw_p), None)
                last_tool_cmd = None

            elif name in CMD_TOOLS:
                cmd_line = str(args.get("CommandLine") or args.get("command") or "")
                if is_noisy_command(cmd_line):
                    noisy_cmd_count += 1
                    last_tool_cmd = cmd_line
                else:
                    last_tool_cmd = None
            else:
                last_tool_cmd = None

    hook_blocked_reads = 0
    hook_cmd_filtered = 0
    hook_saved_bytes = 0
    if metrics_path and Path(metrics_path).is_file():
        for rec in _iter_jsonl(Path(metrics_path)):
            ev = rec.get("event")
            if ev == "read_blocked":
                hook_blocked_reads += 1
                hook_saved_bytes += int(rec.get("saved_bytes") or 0)
            elif ev == "cmd_filtered":
                hook_cmd_filtered += 1
                hook_saved_bytes += int(rec.get("saved_bytes") or 0)

    cmd_saved_bytes = max(0, raw_cmd_bytes - estimated_filtered_cmd_bytes)
    transcript_recoverable_bytes = duplicate_saved_bytes + unbounded_saved_bytes + cmd_saved_bytes
    total_saved_bytes = max(transcript_recoverable_bytes, hook_saved_bytes)

    return {
        "transcript": str(t_path),
        "turns": turns,
        "tool_calls": tool_calls,
        "duplicate_reads_detected": duplicate_reads,
        "unbounded_token_bomb_reads": unbounded_bomb_reads,
        "noisy_commands_detected": noisy_cmd_count,
        "raw_command_output_bytes": raw_cmd_bytes,
        "estimated_filtered_command_bytes": estimated_filtered_cmd_bytes,
        "command_output_bytes_saved": cmd_saved_bytes,
        "hook_blocked_reads": hook_blocked_reads,
        "hook_commands_filtered": hook_cmd_filtered,
        "hook_recorded_saved_bytes": hook_saved_bytes,
        "net_estimated_bytes_saved": total_saved_bytes,
        "net_estimated_tokens_saved": total_saved_bytes // 4,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Antigravity or Claude Code JSONL transcripts for token savings."
    )
    parser.add_argument("transcript", help="Path to transcript.jsonl")
    parser.add_argument("--metrics", help="Optional path to token-guard metrics.jsonl")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    ns = parser.parse_args(argv)

    try:
        report = audit_transcript(ns.transcript, ns.metrics)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if ns.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Transcript: {report['transcript']}")
        print(f"Turns: {report['turns']} | Tool calls: {report['tool_calls']}")
        print(
            f"Duplicate reads flagged: {report['duplicate_reads_detected']} | "
            f"Unbounded token-bomb reads: {report['unbounded_token_bomb_reads']}"
        )
        print(
            f"Noisy commands: {report['noisy_commands_detected']} | "
            f"Command bytes stripped: {report['command_output_bytes_saved']}"
        )
        if ns.metrics:
            print(
                f"Active hook telemetry: {report['hook_blocked_reads']} blocked reads, "
                f"{report['hook_commands_filtered']} filtered cmds ({report['hook_recorded_saved_bytes']} bytes saved)"
            )
        print(
            f"Net estimated savings: {report['net_estimated_bytes_saved']} bytes "
            f"(~{report['net_estimated_tokens_saved']} tokens)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
