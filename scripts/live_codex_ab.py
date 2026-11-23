#!/usr/bin/env python3
"""
token-guard live_codex_ab: Ultra-low-usage live A/B benchmark on OpenAI Codex CLI
(`gpt-6.1-terra`) comparing Baseline (token-guard OFF) vs. token-guard ON across
2 real coding tasks (4 short ephemeral calls total, `--ignore-user-config`,
`model_reasoning_effort="low"` to minimize Plus plan usage).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_ENTRY = REPO_ROOT / "scripts" / "hook_entry.py"
SKILL_MD = REPO_ROOT / "skills" / "token-guard" / "SKILL.md"

DEFAULT_CODEX_EXE = (
    r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1117.4957.0_x64__2p2nqsd0c76g0\app\resources\codex.exe"
)


def _setup_workspace(ws: Path, enable_token_guard: bool) -> None:
    ws.mkdir(parents=True, exist_ok=True)

    # 1. 560-line Python module where `compute_fee_tier_14` has a bug (0.04 instead of 0.05)
    lines: list[str] = ['"""Fee & billing engine module."""', "import math", ""]
    for idx in range(1, 19):
        fn = f"compute_fee_tier_{idx:02d}"
        mult = "0.04" if idx == 14 else f"{0.01 * idx:.2f}"
        lines.append(f"def {fn}(amount: float) -> float:")
        for step in range(1, 28):
            lines.append(f"    adj_{step} = amount + {step * 0.5}")
        lines.append(f"    return round(amount * {mult}, 2)")
        lines.append("")
    (ws / "billing_engine.py").write_text("\n".join(lines), encoding="utf-8")

    # 2. Standard unittest suite with 140 verbose passing tests + 1 failing test
    test_lines = [
        "import unittest",
        "from billing_engine import compute_fee_tier_14",
        "",
        "class TestBillingSuite(unittest.TestCase):",
    ]
    for i in range(1, 141):
        if i == 14:
            test_lines.append("    def test_billing_tier_014(self):")
            test_lines.append("        self.assertEqual(compute_fee_tier_14(1000.0), 50.0, 'expected 50.0 got 40.0 in compute_fee_tier_14')")
        else:
            test_lines.append(f"    def test_billing_tier_{i:03d}_ok_verbose_check_case(self):")
            test_lines.append(f"        print('PASS test_billing_tier_{i:03d} verified clean stage output ... ok')")
            test_lines.append(f"        self.assertEqual({i} * 2, {i * 2})")
    test_lines.append("")
    test_lines.append("if __name__ == '__main__':")
    test_lines.append("    unittest.main(verbosity=2)")
    (ws / "test_billing.py").write_text("\n".join(test_lines), encoding="utf-8")

    if enable_token_guard:
        # Install workspace skill (.agents/skills/token-guard/SKILL.md)
        skill_dir = ws / ".agents" / "skills" / "token-guard"
        skill_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_MD, skill_dir / "SKILL.md")

        # Install Codex PreToolUse hooks (.codex/hooks.json)
        codex_dir = ws / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        hook_cmd = f'python3 "{HOOK_ENTRY}"'
        hooks_cfg = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_cmd,
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        }
        (codex_dir / "hooks.json").write_text(json.dumps(hooks_cfg, indent=2), encoding="utf-8")


def _run_codex_task(
    codex_exe: str,
    ws: Path,
    prompt: str,
    enable_token_guard: bool,
    model: str = "gpt-6.1-terra",
) -> dict[str, Any]:
    cmd = [
        codex_exe,
        "exec",
        "-m",
        model,
        "-c",
        'model_reasoning_effort="low"',
        "-c",
        f"features.skip_host_skill_discovery={'false' if enable_token_guard else 'true'}",
        "-c",
        f"features.hooks={'true' if enable_token_guard else 'false'}",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--json",
        "-C",
        str(ws),
        prompt,
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    wall_s = round(time.perf_counter() - t0, 2)

    input_tokens = 0
    cached_tokens = 0
    output_tokens = 0
    tool_calls = 0
    tool_output_bytes = 0
    last_msg = ""

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = ev.get("type") or ""
        # Codex JSONL events: turn.completed usage or token_count / response_item
        usage = ev.get("usage") or (ev.get("payload") or {}).get("usage")
        if isinstance(usage, dict):
            in_t = int(usage.get("input_tokens") or 0)
            ca_t = int(usage.get("cached_input_tokens") or 0)
            out_t = int(usage.get("output_tokens") or 0)
            # Keep cumulative or latest turn total
            input_tokens += in_t
            cached_tokens += ca_t
            output_tokens += out_t

        item = ev.get("item") or ev.get("payload") or {}
        if isinstance(item, dict):
            itype = item.get("type") or ""
            if itype in ("command_execution", "custom_tool_call", "function_call", "mcp_tool_call"):
                if ev.get("type") in ("item.completed", "response_item"):
                    tool_calls += 1
                    agg_out = str(item.get("aggregated_output") or item.get("output") or "")
                    tool_output_bytes += len(agg_out.encode("utf-8", errors="replace"))
            elif itype in ("agent_message", "message"):
                txt = item.get("text") or ""
                if txt:
                    last_msg = txt

    fresh_tokens = max(0, input_tokens - cached_tokens)
    return {
        "returncode": proc.returncode,
        "wall_seconds": wall_s,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "fresh_uncached_tokens": fresh_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "tool_output_bytes": tool_output_bytes,
        "last_message": last_msg.strip()[:240],
        "raw_stdout_lines": len((proc.stdout or "").splitlines()),
        "stderr_preview": (proc.stderr or "").strip()[:300],
    }


def main() -> int:
    codex_exe = DEFAULT_CODEX_EXE if os.path.isfile(DEFAULT_CODEX_EXE) else (shutil.which("codex") or "")
    if not codex_exe:
        print("codex.exe not found", file=sys.stderr)
        return 2

    # Make Task 1 & Task 2 prompts 100% identical between OFF and ON for a fair A/B test
    fair_tasks = [
        {
            "id": "T1_large_file_bug_lookup",
            "prompt": (
                "In billing_engine.py and test_billing.py, find `compute_fee_tier_14` and reply in ONE short sentence "
                "stating its current multiplier and what multiplier `test_billing.py` expects. Do not edit files."
            ),
            "expect": ("0.04", "0.05"),
        },
        {
            "id": "T2_noisy_test_diagnosis",
            "prompt": (
                "Run `python3 -m unittest -v` in this folder to diagnose the test suite failure, "
                "and reply in ONE short sentence with the failing test name and expected vs actual values. Do not edit files."
            ),
            "expect": ("50.0", "40.0"),
        },
    ]

    # Toggle global ~/.codex/skills/token-guard and ~/.codex/hooks.json between OFF and ON
    global_skill = Path.home() / ".codex" / "skills" / "token-guard"
    backup_skill = Path.home() / ".codex" / "skills" / "_token-guard.off_bak"
    global_hooks = Path.home() / ".codex" / "hooks.json"
    hook_cmd = f'python3 "{HOOK_ENTRY}"'
    hooks_cfg_text = json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": ".*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_cmd,
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        },
        indent=2,
    )

    results: dict[str, Any] = {"model": "gpt-6.1-terra", "tasks": []}

    try:
        for t in fair_tasks:
            row: dict[str, Any] = {"id": t["id"]}
            for mode_name, enabled in (("off", False), ("on", True)):
                if not enabled:
                    if global_skill.exists():
                        if backup_skill.exists():
                            shutil.rmtree(backup_skill)
                        global_skill.rename(backup_skill)
                    if global_hooks.exists():
                        global_hooks.unlink()
                else:
                    if backup_skill.exists() and not global_skill.exists():
                        backup_skill.rename(global_skill)
                    global_hooks.write_text(hooks_cfg_text, encoding="utf-8")

                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    ws = Path(td)
                    _setup_workspace(ws, enable_token_guard=enabled)
                    res = _run_codex_task(codex_exe, ws, t["prompt"], enable_token_guard=enabled)
                    passed = all(sub in res["last_message"] for sub in t["expect"])
                    res["passed"] = passed
                    row[mode_name] = res
            results["tasks"].append(row)
    finally:
        if backup_skill.exists() and not global_skill.exists():
            backup_skill.rename(global_skill)
        global_hooks.write_text(hooks_cfg_text, encoding="utf-8")

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
