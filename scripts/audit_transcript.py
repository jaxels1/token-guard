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


def discover_transcripts(home_dir: str | Path | None = None) -> dict[str, list[Path]]:
    """Auto-discover local agent session transcripts across Claude Code, Codex, and Antigravity."""
    home = Path(home_dir) if home_dir is not None else Path.home()
    found: dict[str, list[Path]] = {
        "Claude Code": [],
        "ChatGPT / Codex": [],
        "Antigravity": [],
    }

    claude_root = home / ".claude" / "projects"
    if claude_root.is_dir():
        found["Claude Code"] = sorted(
            p for p in claude_root.rglob("*.jsonl") if p.is_file() and p.stat().st_size > 0
        )

    codex_root = home / ".codex" / "sessions"
    if codex_root.is_dir():
        found["ChatGPT / Codex"] = sorted(
            p for p in codex_root.rglob("*.jsonl") if p.is_file() and p.stat().st_size > 0
        )

    agy_root = home / ".gemini" / "antigravity" / "brain"
    if agy_root.is_dir():
        agy_files: list[Path] = []
        for conv_dir in sorted(agy_root.iterdir()):
            if not conv_dir.is_dir():
                continue
            logs_dir = conv_dir / ".system_generated" / "logs"
            full_p = logs_dir / "transcript_full.jsonl"
            compact_p = logs_dir / "transcript.jsonl"
            if full_p.is_file() and full_p.stat().st_size > 0:
                agy_files.append(full_p)
            elif compact_p.is_file() and compact_p.stat().st_size > 0:
                agy_files.append(compact_p)
        found["Antigravity"] = agy_files

    return found


def scan_all_transcripts(home_dir: str | Path | None = None) -> dict[str, Any]:
    """Scan all discovered local transcripts and aggregate token-guard savings."""
    discovered = discover_transcripts(home_dir=home_dir)
    by_agent: dict[str, dict[str, Any]] = {}
    totals = {
        "sessions_scanned": 0,
        "turns": 0,
        "tool_calls": 0,
        "duplicate_reads_detected": 0,
        "unbounded_token_bomb_reads": 0,
        "noisy_commands_detected": 0,
        "raw_command_output_bytes": 0,
        "estimated_filtered_command_bytes": 0,
        "command_output_bytes_saved": 0,
        "recorded_input_tokens": 0,
        "recorded_cached_input_tokens": 0,
        "net_estimated_bytes_saved": 0,
        "net_estimated_tokens_saved": 0,
    }

    for agent_name, paths in discovered.items():
        agent_bucket = {k: 0 for k in totals}
        for p in paths:
            try:
                rep = audit_transcript(p)
            except Exception:
                continue
            if rep["turns"] == 0 and rep["tool_calls"] == 0:
                continue
            agent_bucket["sessions_scanned"] += 1
            totals["sessions_scanned"] += 1
            for k in (
                "turns",
                "tool_calls",
                "duplicate_reads_detected",
                "unbounded_token_bomb_reads",
                "noisy_commands_detected",
                "raw_command_output_bytes",
                "estimated_filtered_command_bytes",
                "command_output_bytes_saved",
                "recorded_input_tokens",
                "recorded_cached_input_tokens",
                "net_estimated_bytes_saved",
                "net_estimated_tokens_saved",
            ):
                agent_bucket[k] += int(rep.get(k) or 0)
                totals[k] += int(rep.get(k) or 0)
        by_agent[agent_name] = agent_bucket

    rec_in = totals["recorded_input_tokens"]
    rec_cached = totals["recorded_cached_input_tokens"]
    fresh_uncached = max(0, rec_in - rec_cached)
    kv_hit_pct = round((rec_cached / rec_in) * 100, 1) if rec_in > 0 else None
    fresh_savings_pct = (
        round(min(99.9, (totals["net_estimated_tokens_saved"] / fresh_uncached) * 100), 1)
        if fresh_uncached > 0
        else None
    )

    return {
        "sessions_scanned": totals["sessions_scanned"],
        "turns": totals["turns"],
        "tool_calls": totals["tool_calls"],
        "duplicate_reads_detected": totals["duplicate_reads_detected"],
        "unbounded_token_bomb_reads": totals["unbounded_token_bomb_reads"],
        "noisy_commands_detected": totals["noisy_commands_detected"],
        "raw_command_output_bytes": totals["raw_command_output_bytes"],
        "estimated_filtered_command_bytes": totals["estimated_filtered_command_bytes"],
        "command_output_bytes_saved": totals["command_output_bytes_saved"],
        "recorded_input_tokens": rec_in,
        "recorded_cached_input_tokens": rec_cached,
        "fresh_uncached_input_tokens": fresh_uncached,
        "kv_cache_hit_rate_pct": kv_hit_pct,
        "net_estimated_bytes_saved": totals["net_estimated_bytes_saved"],
        "net_estimated_tokens_saved": totals["net_estimated_tokens_saved"],
        "fresh_token_burn_reduction_pct": fresh_savings_pct,
        "by_agent": by_agent,
    }


def _fmt_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def _fmt_tok(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n:,}"


def format_scan_all_scorecard(summary: dict[str, Any]) -> str:
    w = 72
    top = "┌" + "─" * w + "┐"
    sep = "├" + "─" * w + "┤"
    bot = "└" + "─" * w + "┘"

    def row(left: str, right: str = "") -> str:
        if not right:
            txt = left[: w - 2]
            return f"│ {txt:<{w - 2}} │"
        avail = w - 3 - len(right)
        l_trim = left[: max(1, avail)]
        return f"│ {l_trim:<{avail}} {right} │"

    lines = [
        top,
        row("TOKEN-GUARD · LOCAL AGENT SESSION SCORECARD", "v1.1.0"),
        sep,
        row(
            f"Sessions scanned: {summary['sessions_scanned']:,}",
            f"Turns: {summary['turns']:,} · Tool calls: {summary['tool_calls']:,}",
        ),
    ]

    for agent, b in summary.get("by_agent", {}).items():
        if b.get("sessions_scanned", 0) > 0:
            lines.append(
                row(
                    f"  • {agent}",
                    f"{b['sessions_scanned']:,} sessions · {b['tool_calls']:,} calls · ~{_fmt_tok(b['net_estimated_tokens_saved'])} tok",
                )
            )

    raw_b = summary["raw_command_output_bytes"]
    filt_b = summary["estimated_filtered_command_bytes"]
    cmd_pct = round(((raw_b - filt_b) / raw_b) * 100, 1) if raw_b > 0 else 0.0

    lines.extend(
        [
            sep,
            row(
                "Duplicate unchanged file reads:",
                f"{summary['duplicate_reads_detected']:,} blocked",
            ),
            row(
                "Unbounded lockfile / bundle reads:",
                f"{summary['unbounded_token_bomb_reads']:,} blocked",
            ),
            row(
                "Noisy CLI / test outputs compressed:",
                f"{_fmt_mb(raw_b)} -> {_fmt_mb(filt_b)} (-{cmd_pct}%)",
            ),
        ]
    )

    if summary.get("recorded_input_tokens", 0) > 0:
        lines.append(
            row(
                "Recorded KV prefix cache hit rate:",
                f"{summary['kv_cache_hit_rate_pct']}% ({_fmt_tok(summary['recorded_cached_input_tokens'])} / {_fmt_tok(summary['recorded_input_tokens'])} tok)",
            )
        )

    saved_tok = _fmt_tok(summary["net_estimated_tokens_saved"])
    saved_mb = _fmt_mb(summary["net_estimated_bytes_saved"])
    pct_str = (
        f" (-{summary['fresh_token_burn_reduction_pct']}% fresh burn)"
        if summary.get("fresh_token_burn_reduction_pct")
        else ""
    )
    lines.extend(
        [
            sep,
            row("RECOVERABLE CONTEXT BLOAT:", f"{saved_mb} (~{saved_tok} tok){pct_str}"),
            row("Install token-guard:", "npx skills add jaxels1/token-guard"),
            bot,
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Antigravity, Claude Code, or ChatGPT/Codex JSONL transcripts for token savings."
    )
    parser.add_argument("transcript", nargs="?", help="Path to transcript.jsonl or rollout-*.jsonl")
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Scan all local Claude Code, ChatGPT/Codex, and Antigravity sessions on this machine",
    )
    parser.add_argument("--metrics", help="Optional path to token-guard metrics.jsonl")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    ns = parser.parse_args(argv)

    if ns.scan_all:
        summary = scan_all_transcripts()
        if ns.json:
            print(json.dumps(summary, indent=2))
        else:
            print(format_scan_all_scorecard(summary))
        return 0

    if not ns.transcript:
        parser.error("Provide a transcript path or pass --scan-all")

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
