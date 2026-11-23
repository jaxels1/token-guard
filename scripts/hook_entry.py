#!/usr/bin/env python3
"""
token-guard hook_entry: Auto-detects host PreToolUse schema (Antigravity,
Claude Code, Cursor, Gemini CLI), dispatches to read_guard / cmd_filter, and
emits the host's native response JSON. Strictly fail-open on errors.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def detect_host(payload: dict[str, Any]) -> str:
    if "toolCall" in payload or "hookEventName" in payload:
        return "antigravity"
    hook_evt = str(payload.get("hook_event_name") or "")
    if "cursor_version" in payload or hook_evt.startswith("before") or "workspace_roots" in payload:
        return "cursor"
    if "tool_input" in payload and "tool_name" in payload:
        tool_name = str(payload.get("tool_name") or "")
        if payload.get("host") == "gemini" or tool_name in ("run_shell_command", "read_file"):
            return "gemini"
        return "claude"
    return "antigravity"


def extract_tool(payload: dict[str, Any], host: str) -> tuple[str, dict[str, Any], str]:
    if host == "antigravity":
        tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        tool_name = str(tc.get("name") or payload.get("toolName") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        conv_id = str(payload.get("conversationId") or "antigravity-default")
        return tool_name, args, conv_id

    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    conv_id = str(payload.get("session_id") or payload.get("conversation_id") or f"{host}-default")
    return tool_name, args, conv_id


def format_allow(
    host: str,
    overwrite: dict[str, Any] | None = None,
    orig_args: dict[str, Any] | None = None,
    permission_overrides: list[str] | None = None,
) -> dict[str, Any]:
    if host == "antigravity":
        out: dict[str, Any] = {"decision": "allow"}
        if overwrite:
            out["overwrite"] = overwrite
        if permission_overrides:
            out["permissionOverrides"] = permission_overrides
        return out

    if host == "claude":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }
        if overwrite and orig_args is not None:
            out["hookSpecificOutput"]["updatedInput"] = {**orig_args, **overwrite}
        return out

    if host == "cursor":
        out = {"permission": "allow"}
        if overwrite and orig_args is not None:
            out["updated_input"] = {**orig_args, **overwrite}
        return out

    # Gemini CLI
    out = {"decision": "allow"}
    if overwrite and orig_args is not None:
        out["tool_input"] = {**orig_args, **overwrite}
    return out


def format_deny(host: str, reason: str) -> dict[str, Any]:
    if host == "antigravity":
        return {"decision": "deny", "reason": reason}
    if host == "claude":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if host == "cursor":
        return {"permission": "deny", "agent_message": reason, "user_message": reason}
    return {"decision": "deny", "reason": reason}


def handle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    host = detect_host(payload)
    hook_evt = str(payload.get("hookEventName") or payload.get("hook_event_name") or "")
    tool_name, args, conv_id = extract_tool(payload, host)
    norm_tool = tool_name.strip()

    # 0. PreCompact / context compaction reset event
    if hook_evt in ("PreCompact", "preCompact", "Compact"):
        from read_guard import reset_conversation_state
        reset_conversation_state(conv_id)
        return format_allow(host)

    # 1. File Read Guard (+ automatic AST/regex outline on >500-line files)
    if norm_tool in ("read_file", "view_file", "Read"):
        from read_guard import evaluate_read
        path = args.get("Path") or args.get("AbsolutePath") or args.get("file_path") or args.get("path") or ""
        start = args.get("StartLine") if args.get("StartLine") is not None else args.get("offset")
        end = args.get("EndLine") if args.get("EndLine") is not None else None
        if end is None and start is not None and args.get("limit") is not None:
            try:
                end = int(start) + int(args["limit"]) - 1
            except (TypeError, ValueError):
                end = None

        res = evaluate_read(
            conv_id,
            str(path),
            int(start) if start is not None else None,
            int(end) if end is not None else None,
            step_idx=int(payload["stepIdx"]) if isinstance(payload.get("stepIdx"), int) else None,
            transcript_path=str(payload.get("transcriptPath") or payload.get("transcript_path") or "") or None,
        )
        if not res.get("allow", True):
            return format_deny(host, res.get("reason") or "Blocked by token-guard read_guard")
        if res.get("overwrite_range"):
            new_start, new_end = res["overwrite_range"]
            if "StartLine" in args or "EndLine" in args or host == "antigravity":
                ov = {"StartLine": new_start, "EndLine": new_end}
            else:
                ov = {"offset": new_start, "limit": max(1, new_end - new_start + 1)}
            return format_allow(host, overwrite=ov, orig_args=args)
        return format_allow(host)

    # 2. Grep / Search Explosion Guard
    if norm_tool in ("grep_search", "find_by_name", "Grep", "Glob"):
        from search_guard import evaluate_search
        s_res = evaluate_search(norm_tool, args)
        if not s_res.get("allow", True):
            return format_deny(host, s_res.get("reason") or "Blocked by token-guard search_guard")
        if s_res.get("overwrite"):
            return format_allow(host, overwrite=s_res["overwrite"], orig_args=args)
        return format_allow(host)

    # 3. Noisy Command Output Filter (allowlist-friendly `--pipe` prefix preservation)
    if norm_tool in ("run_command", "Bash", "Shell", "run_shell_command"):
        from cmd_filter import maybe_rewrite_command
        cmd_key = "CommandLine" if "CommandLine" in args else ("command" if "command" in args else "Command")
        cmd_line = str(args.get(cmd_key) or "")
        rewritten = maybe_rewrite_command(
            cmd_line,
            script_path=os.path.join(SCRIPT_DIR, "cmd_filter.py"),
        )
        if rewritten:
            return format_allow(
                host,
                overwrite={cmd_key: rewritten},
                orig_args=args,
                permission_overrides=[f"command({cmd_line.strip()})", f"command({rewritten})"],
            )
        return format_allow(host)

    return format_allow(host)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            print("{}")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print("{}")
            return 0

        # Fast path: query persistent background worker (<1.5ms round-trip) if active
        if os.environ.get("TOKEN_GUARD_USE_DAEMON", "1") != "0":
            try:
                from hook_daemon import ensure_daemon_started, query_daemon
                resp = query_daemon(payload)
                if resp is not None:
                    print(json.dumps(resp))
                    return 0
                ensure_daemon_started()
            except Exception:
                pass

        response = handle_payload(payload)
        print(json.dumps(response))
        return 0
    except Exception:
        # Strictly fail-open: never break the user's coding agent on hook error
        print('{"decision": "allow"}')
        return 0


if __name__ == "__main__":
    sys.exit(main())
