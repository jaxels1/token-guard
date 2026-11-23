#!/usr/bin/env python3
"""
token-guard audit_transcript: Parses Antigravity, Claude Code, and ChatGPT /
OpenAI Codex Desktop app (rollout-*.jsonl) transcripts + optional metrics.jsonl
to report turns, duplicate reads, unbounded reads, command output savings,
KV cache hit rates, and net estimated tokens saved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from cmd_filter import filter_output, is_noisy_command  # noqa: E402
from read_guard import LOCKFILE_NAMES, MINIFIED_SUFFIXES, normalize_path  # noqa: E402

READ_TOOLS = {"read_file", "view_file", "Read"}
EDIT_TOOLS = {
    "edit_file",
    "write_file",
    "Edit",
    "Write",
    "replace_file_content",
    "write_to_file",
    "apply_patch",
}
CMD_TOOLS = {"run_command", "Bash", "Shell", "run_shell_command", "exec_command"}

# Detect shell-based file reads inside ChatGPT/Codex `exec_command` calls
SHELL_READ_RE = re.compile(
    r"""(?:Get-Content(?:\s+-LiteralPath|\s+-Path)?|(?:\bcat|\bhead|\btail)\b)\s+['"]?([^'"\n|;]+?\.[A-Za-z0-9]{1,8})['"]?""",
    re.I,
)
EXEC_CMD_EXTRACT_RE = re.compile(r'"cmd"\s*:\s*"((?:\\.|[^"\\])*)"|cmd\s*:\s*"((?:\\.|[^"\\])*)"')


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


def _extract_codex_commands(js_code: str) -> list[str]:
    cmds: list[str] = []
    for m in EXEC_CMD_EXTRACT_RE.finditer(js_code):
        raw_cmd = m.group(1) or m.group(2) or ""
        try:
            unescaped = bytes(raw_cmd, "utf-8").decode("unicode_escape")
        except Exception:
            unescaped = raw_cmd.replace('\\"', '"').replace("\\n", "\n")
        for sub in unescaped.splitlines():
            sub_s = sub.strip()
            if sub_s:
                cmds.append(sub_s)
    return cmds


def _extract_calls_from_step(obj: dict[str, Any]) -> list[tuple[str, dict[str, Any], int]]:
    """Return list of (tool_name, args, result_bytes_estimate) from a JSONL line."""
    calls: list[tuple[str, dict[str, Any], int]] = []

    # 1. Antigravity compact/full transcript format
    if isinstance(obj.get("tool_calls"), list):
        for tc in obj["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "")
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            out = tc.get("result") or tc.get("output") or ""
            out_bytes = len(str(out).encode("utf-8", errors="replace")) if out else 0
            calls.append((name, args, out_bytes))

    if obj.get("type") in ("TOOL_RESPONSE", "tool_result") and isinstance(obj.get("content"), str):
        calls.append(("__tool_output__", {"text": obj["content"]}, len(obj["content"].encode("utf-8"))))

    # 2. Claude Code message.content array
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

    # 3. ChatGPT / OpenAI Codex Desktop app rollout-*.jsonl (`type: "response_item"`)
    if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
        p = obj["payload"]
        ptype = p.get("type")
        if ptype == "custom_tool_call" and p.get("name") == "exec":
            js_input = str(p.get("input") or "")
            sub_cmds = _extract_codex_commands(js_input)
            if not sub_cmds and js_input.strip():
                calls.append(("exec_command", {"CommandLine": js_input[:300]}, 0))
            for sc in sub_cmds:
                for rm in SHELL_READ_RE.finditer(sc):
                    target_file = rm.group(1).strip()
                    bounded = "-TotalCount" in sc or "-First" in sc or "Select-Object" in sc or "head " in sc
                    calls.append((
                        "read_file",
                        {"Path": target_file, **({"StartLine": 1, "EndLine": 120} if bounded else {})},
                        0,
                    ))
                calls.append(("run_command", {"CommandLine": sc}, 0))
        elif ptype == "custom_tool_call_output":
            out_items = p.get("output")
            text_parts = []
            if isinstance(out_items, list):
                for it in out_items:
                    if isinstance(it, dict) and isinstance(it.get("text"), str):
                        text_parts.append(it["text"])
            elif isinstance(out_items, str):
                text_parts.append(out_items)
            joined = "\n".join(text_parts)
            calls.append(("__tool_output__", {"text": joined}, len(joined.encode("utf-8", errors="replace"))))
        elif ptype == "function_call":
            fname = str(p.get("name") or "")
            raw_args = p.get("arguments")
            try:
                fargs = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                fargs = {}
            if not isinstance(fargs, dict):
                fargs = {}
            if fname == "exec_command":
                cmd_str = str(fargs.get("cmd") or fargs.get("command") or "")
                for rm in SHELL_READ_RE.finditer(cmd_str):
                    target_file = rm.group(1).strip()
                    bounded = "-TotalCount" in cmd_str or "-First" in cmd_str or "Select-Object" in cmd_str
                    calls.append((
                        "read_file",
                        {"Path": target_file, **({"StartLine": 1, "EndLine": 120} if bounded else {})},
                        0,
                    ))
                calls.append(("run_command", {"CommandLine": cmd_str}, 0))
            else:
                calls.append((fname, fargs, 0))
        elif ptype == "function_call_output":
            fout = str(p.get("output") or "")
            calls.append(("__tool_output__", {"text": fout}, len(fout.encode("utf-8", errors="replace"))))

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
    recorded_input_tokens = 0
    recorded_cached_tokens = 0

    read_history: dict[str, list[tuple[int, int]]] = {}
    pending_noisy_cmd = False

    for obj in _iter_jsonl(t_path):
        step_type = str(obj.get("type") or "")
        if step_type in ("USER_INPUT", "PLANNER_RESPONSE", "user", "assistant", "turn_context"):
            turns += 1
        elif step_type == "token_usage_record":
            usage = (obj.get("payload") or {}).get("usage") or {}
            recorded_input_tokens += int(usage.get("input_tokens") or 0)
            recorded_cached_tokens += int(usage.get("cached_input_tokens") or 0)

        for name, args, out_bytes in _extract_calls_from_step(obj):
            if name == "__tool_output__":
                text = str(args.get("text") or "")
                if pending_noisy_cmd or out_bytes > 3500:
                    raw_cmd_bytes += out_bytes
                    compressed, _ = filter_output(text, "tool_output")
                    c_bytes = len(compressed.encode("utf-8", errors="replace"))
                    estimated_filtered_cmd_bytes += min(out_bytes, c_bytes)
                    pending_noisy_cmd = False
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

            elif name in EDIT_TOOLS:
                raw_p = str(args.get("Path") or args.get("TargetFile") or args.get("file_path") or "")
                if raw_p:
                    read_history.pop(normalize_path(raw_p), None)

            elif name in CMD_TOOLS:
                cmd_line = str(args.get("CommandLine") or args.get("command") or "")
                if is_noisy_command(cmd_line):
                    noisy_cmd_count += 1
                    pending_noisy_cmd = True

    hook_blocked_reads = 0
    hook_cmd_filtered = 0
    hook_saved_bytes = 0
    if metrics_path and Path(metrics_path).is_file():
        for rec in _iter_jsonl(Path(metrics_path)):
            ev = rec.get("event")
            if ev in ("read_blocked", "read_trimmed"):
                hook_blocked_reads += 1
                hook_saved_bytes += int(rec.get("saved_bytes") or 0)
            elif ev == "cmd_filtered":
                hook_cmd_filtered += 1
                hook_saved_bytes += int(rec.get("saved_bytes") or 0)

    cmd_saved_bytes = max(0, raw_cmd_bytes - estimated_filtered_cmd_bytes)
    transcript_recoverable_bytes = duplicate_saved_bytes + unbounded_saved_bytes + cmd_saved_bytes
    total_saved_bytes = max(transcript_recoverable_bytes, hook_saved_bytes)
    cache_hit_pct = (
        round((recorded_cached_tokens / recorded_input_tokens) * 100, 1)
        if recorded_input_tokens > 0
        else None
    )

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
        "recorded_input_tokens": recorded_input_tokens,
        "recorded_cached_input_tokens": recorded_cached_tokens,
        "kv_cache_hit_rate_pct": cache_hit_pct,
        "hook_blocked_reads": hook_blocked_reads,
        "hook_commands_filtered": hook_cmd_filtered,
        "hook_recorded_saved_bytes": hook_saved_bytes,
        "net_estimated_bytes_saved": total_saved_bytes,
        "net_estimated_tokens_saved": total_saved_bytes // 4,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Antigravity, Claude Code, or ChatGPT/Codex JSONL transcripts for token savings."
    )
    parser.add_argument("transcript", help="Path to transcript.jsonl or rollout-*.jsonl")
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
        if report["recorded_input_tokens"] > 0:
            print(
                f"Recorded Input Tokens: {report['recorded_input_tokens']:,} | "
                f"Cached: {report['recorded_cached_input_tokens']:,} ({report['kv_cache_hit_rate_pct']}% KV cache hit)"
            )
        print(
            f"Duplicate reads flagged: {report['duplicate_reads_detected']} | "
            f"Unbounded token-bomb reads: {report['unbounded_token_bomb_reads']}"
        )
        print(
            f"Noisy commands: {report['noisy_commands_detected']} | "
            f"Command bytes stripped: {report['command_output_bytes_saved']:,}"
        )
        if ns.metrics:
            print(
                f"Active hook telemetry: {report['hook_blocked_reads']} blocked/trimmed reads, "
                f"{report['hook_commands_filtered']} filtered cmds ({report['hook_recorded_saved_bytes']:,} bytes saved)"
            )
        print(
            f"Net estimated savings: {report['net_estimated_bytes_saved']:,} bytes "
            f"(~{report['net_estimated_tokens_saved']:,} tokens)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
