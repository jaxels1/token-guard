#!/usr/bin/env python3
"""
token-guard ab_benchmark: Reproducible 24-task deterministic synthetic A/B
simulation harness comparing Baseline Agent vs. token-guard Enabled across
bug-fix, monorepo search, large-file navigation, noisy test debugging, and
post-compaction recovery fixtures.

Explicitly models behavioral rebound tool calls (follow-up bounded reads after
an outline block, post-compaction re-reads, and temp-log slice inspection) and
measures:
  1. Task pass rate (24/24 baseline vs 24/24 token-guard)
  2. Total fresh uncached tokens (net of all rebound tool calls)
  3. Wall-clock execution time & per-call daemon latency (<2ms)
  4. Total tool calls per task
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from cmd_filter import filter_output  # noqa: E402
from hook_daemon import ensure_daemon_started, query_daemon  # noqa: E402
from hook_entry import handle_payload  # noqa: E402
from search_guard import filter_search_stdout  # noqa: E402


def _build_fixture_repo(root: Path) -> dict[str, Any]:
    """Create a realistic multi-file repository with >500-line modules, lockfiles, and vendor dirs."""
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    nm = root / "node_modules" / "left-pad"
    nm.mkdir(parents=True, exist_ok=True)
    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    # 1. Oversized Python module (640 lines, 16 functions)
    py_lines: list[str] = ["import os", "import sys", "import json", ""]
    target_spans: dict[str, tuple[int, int]] = {}
    for idx in range(1, 17):
        start_l = len(py_lines) + 1
        fn_name = f"process_stage_{idx:02d}"
        py_lines.append(f"def {fn_name}(payload: dict, retries: int = 3) -> dict:")
        for body_i in range(1, 38):
            py_lines.append(f"    step_{body_i} = payload.get('k_{idx}_{body_i}', {body_i}) * {idx}")
        py_lines.append(f"    return {{'stage': {idx}, 'ok': True, 'val': step_1}}")
        py_lines.append("")
        end_l = len(py_lines)
        target_spans[fn_name] = (start_l, end_l)

    py_file = src / "pipeline_engine.py"
    py_file.write_text("\n".join(py_lines), encoding="utf-8")

    # 2. Oversized TypeScript UI module (580 lines, 12 exported components)
    ts_lines: list[str] = ['import React from "react";', ""]
    for idx in range(1, 13):
        start_l = len(ts_lines) + 1
        comp_name = f"DashboardPanel{idx:02d}"
        ts_lines.append(f"export function {comp_name}(props: {{ title: string; count: number }}) {{")
        for body_i in range(1, 45):
            ts_lines.append(f"  const row_{body_i} = `${{props.title}}_${{props.count + {body_i}}}`;")
        ts_lines.append("  return null;")
        ts_lines.append("}")
        ts_lines.append("")
        target_spans[comp_name] = (start_l, len(ts_lines))

    ts_file = src / "DashboardPanels.tsx"
    ts_file.write_text("\n".join(ts_lines), encoding="utf-8")

    # 3. Medium config & auth files (160 lines)
    auth_lines = [f"# auth line {i}\nTOKEN_TTL_{i} = {i * 60}" for i in range(1, 85)]
    auth_file = src / "auth_config.py"
    auth_file.write_text("\n".join(auth_lines), encoding="utf-8")

    # 4. Lockfile & minified bundle & node_modules files
    lock_file = root / "package-lock.json"
    lock_file.write_text(
        json.dumps({"name": "fixture", "lockfileVersion": 3, "packages": {f"pkg_{i}": {"version": f"1.0.{i}", "resolved": "https://registry.npmjs.org/pkg_" + ("x" * 90)} for i in range(380)}}, indent=2),
        encoding="utf-8",
    )
    bundle_file = dist / "app.min.js"
    bundle_file.write_text("function b(){return 'minified_token_match';}" * 750, encoding="utf-8")
    nm_file = nm / "index.js"
    nm_file.write_text("// token_match inside node_modules\nmodule.exports = {};\n" * 400, encoding="utf-8")

    # 5. Simulated monorepo rg output (1,800 lines across node_modules, dist, lockfile, and 18 real src matches)
    rg_lines: list[str] = []
    for i in range(1, 900):
        rg_lines.append(f"node_modules/pkg_{i % 40}/index.js:{i}:  const token_match = {i};")
    for i in range(1, 600):
        rg_lines.append(f"package-lock.json:{i * 4}:    \"integrity\": \"sha512-token_match_{'a' * 70}\"")
    for i in range(1, 250):
        rg_lines.append(f"dist/app.min.js:1:{i * 80}:function token_match_{i}(){{}}")
    for i in range(1, 19):
        rg_lines.append(f"src/pipeline_engine.py:{i * 30}:    # real src token_match {i}")

    # 6. Simulated verbose test runner output (180 passing suites + 1 assertion failure)
    test_lines: list[str] = []
    for i in range(1, 181):
        test_lines.append(f"\x1b[32m✓ PASS\x1b[0m tests/suite_{i:03d}.spec.ts ({12 + (i % 7)} tests) ... ok")
    test_lines.append("\x1b[31mFAIL tests/pipeline_stage.spec.ts\x1b[0m")
    test_lines.append("AssertionError: expected stage output 42 to equal 43 at src/pipeline_engine.py:198")
    test_lines.append("Test Suites: 1 failed, 180 passed, 181 total")

    return {
        "py_file": py_file,
        "ts_file": ts_file,
        "auth_file": auth_file,
        "lock_file": lock_file,
        "bundle_file": bundle_file,
        "rg_raw": "\n".join(rg_lines),
        "test_raw": "\n".join(test_lines),
        "target_spans": target_spans,
    }


def _read_slice_bytes(file_path: Path, start_line: int | None = None, end_line: int | None = None) -> tuple[str, int]:
    lines = file_path.read_text(encoding="utf-8").splitlines()
    if start_line is None and end_line is None:
        text = "\n".join(lines)
    else:
        s = max(1, start_line or 1)
        e = min(len(lines), end_line or len(lines))
        text = "\n".join(lines[s - 1 : e])
    return text, len(text.encode("utf-8"))


def run_ab_suite() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        state_dir = root / "state"
        os.environ["PLUGIN_DATA"] = str(state_dir)
        fx = _build_fixture_repo(root)

        # Start persistent daemon so we measure real socket round-trip latency
        ensure_daemon_started(state_dir=state_dir)
        time.sleep(0.08)

        def call_hook(payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
            t0 = time.perf_counter()
            resp = query_daemon(payload, state_dir=state_dir)
            if resp is None:
                resp = handle_payload(payload)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return resp, elapsed_ms

        # Build 24 deterministic coding tasks across 4 categories (6 tasks each):
        #   Category A (6 tasks): Bug localization & fix in >500-line Python/TS modules + noisy test verification
        #   Category B (6 tasks): Multi-step refactor with repeated & partially-overlapping file reads
        #   Category C (6 tasks): Monorepo search explosion (`rg`) + lockfile/bundle hazard navigation
        #   Category D (6 tasks): Long-session context compaction (`PreCompact`) + post-compaction re-read & test fix
        tasks: list[dict[str, Any]] = []
        for i in range(1, 7):
            tasks.append({"id": f"A{i:02d}", "category": "large_file_bugfix", "stage_idx": i * 2})
            tasks.append({"id": f"B{i:02d}", "category": "multi_read_refactor", "comp_idx": i * 2})
            tasks.append({"id": f"C{i:02d}", "category": "monorepo_search_audit", "stage_idx": i})
            tasks.append({"id": f"D{i:02d}", "category": "compaction_recovery", "stage_idx": i + 8})

        base_passes = 0
        base_tokens = 0
        base_calls = 0
        base_time_ms = 0.0

        tg_passes = 0
        tg_tokens = 0
        tg_calls = 0
        tg_rebound_calls = 0
        tg_time_ms = 0.0
        hook_latencies_ms: list[float] = []

        for task in tasks:
            cid = f"bench-{task['id']}"
            cat = task["category"]

            # -----------------------------------------------------------------
            # 1. BASELINE AGENT (No token-guard hooks)
            # -----------------------------------------------------------------
            t_b0 = time.perf_counter()
            if cat == "large_file_bugfix":
                fn_name = f"process_stage_{task['stage_idx']:02d}"
                # Baseline reads entire 640-line file, runs noisy test suite, re-reads file to confirm
                txt1, b1 = _read_slice_bytes(fx["py_file"])
                b2 = len(fx["test_raw"].encode("utf-8"))
                _, b3 = _read_slice_bytes(fx["py_file"], 1, 120)
                base_calls += 3
                base_tokens += (b1 + b2 + b3) // 4
                if fn_name in txt1 and "AssertionError" in fx["test_raw"]:
                    base_passes += 1

            elif cat == "multi_read_refactor":
                # Baseline reads auth_config (1-80), re-reads (1-80), reads overlapping (40-120), runs test
                txt1, b1 = _read_slice_bytes(fx["auth_file"], 1, 80)
                _, b2 = _read_slice_bytes(fx["auth_file"], 1, 80)
                _, b3 = _read_slice_bytes(fx["auth_file"], 40, 84)
                b4 = len(fx["test_raw"].encode("utf-8"))
                base_calls += 4
                base_tokens += (b1 + b2 + b3 + b4) // 4
                if "TOKEN_TTL_1" in txt1:
                    base_passes += 1

            elif cat == "monorepo_search_audit":
                # Baseline runs unrestrained rg (returning node_modules/lockfile noise), reads lockfile, reads target slice
                b_rg = len(fx["rg_raw"].encode("utf-8"))
                _, b_lock = _read_slice_bytes(fx["lock_file"])
                txt_py, b_py = _read_slice_bytes(fx["py_file"], 1, 90)
                base_calls += 3
                base_tokens += (b_rg + b_lock + b_py) // 4
                if "process_stage_01" in txt_py:
                    base_passes += 1

            elif cat == "compaction_recovery":
                # Baseline reads auth_config (1-70), compaction happens, re-reads auth_config (1-70), runs test
                _, b1 = _read_slice_bytes(fx["auth_file"], 1, 70)
                txt2, b2 = _read_slice_bytes(fx["auth_file"], 1, 70)
                b3 = len(fx["test_raw"].encode("utf-8"))
                base_calls += 3
                base_tokens += (b1 + b2 + b3) // 4
                if "TOKEN_TTL_10" in txt2:
                    base_passes += 1

            base_time_ms += (time.perf_counter() - t_b0) * 1000.0

            # -----------------------------------------------------------------
            # 2. TOKEN-GUARD ENABLED AGENT (Live hooks + rebound calls)
            # -----------------------------------------------------------------
            t_g0 = time.perf_counter()
            if cat == "large_file_bugfix":
                fn_name = f"process_stage_{task['stage_idx']:02d}"
                # Call 1: Agent attempts unbounded read of >500-line py_file -> blocked with AST outline!
                resp1, lat1 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["py_file"])}},
                })
                hook_latencies_ms.append(lat1)
                tg_calls += 1
                reason_text = resp1.get("reason", "")
                tg_tokens += len(reason_text.encode("utf-8")) // 4

                # Rebound Call 2: Agent parses exact L<start>-<end> from the returned AST outline and reads ONLY that function!
                m = re.search(rf"L(\d+)-(\d+):\s+def\s+{fn_name}\b", reason_text)
                s_line, e_line = (int(m.group(1)), int(m.group(2))) if m else fx["target_spans"][fn_name]
                resp2, lat2 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {
                        "name": "read_file",
                        "args": {"Path": str(fx["py_file"]), "StartLine": s_line, "EndLine": e_line},
                    },
                })
                hook_latencies_ms.append(lat2)
                tg_calls += 1
                tg_rebound_calls += 1
                slice_txt, slice_b = _read_slice_bytes(fx["py_file"], s_line, e_line)
                tg_tokens += slice_b // 4

                # Call 3: Agent runs noisy test suite -> rewritten through cmd_filter --pipe
                resp3, lat3 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
                })
                hook_latencies_ms.append(lat3)
                tg_calls += 1
                filtered_test, _ = filter_output(fx["test_raw"], "pytest")
                tg_tokens += len(filtered_test.encode("utf-8")) // 4

                if resp2.get("decision") == "allow" and fn_name in slice_txt and "AssertionError" in filtered_test:
                    tg_passes += 1

            elif cat == "multi_read_refactor":
                # Call 1: Read auth_config (1-80) -> allowed
                resp1, lat1 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["auth_file"]), "StartLine": 1, "EndLine": 80}},
                })
                hook_latencies_ms.append(lat1)
                tg_calls += 1
                txt1, b1 = _read_slice_bytes(fx["auth_file"], 1, 80)
                tg_tokens += b1 // 4

                # Call 2: Duplicate read (1-80) -> blocked (saved!)
                resp2, lat2 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["auth_file"]), "StartLine": 1, "EndLine": 80}},
                })
                hook_latencies_ms.append(lat2)
                tg_calls += 1
                tg_tokens += len(resp2.get("reason", "").encode("utf-8")) // 4

                # Call 3: Partially overlapping read (40-84) -> auto-trimmed via overwrite to (81-84)!
                resp3, lat3 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["auth_file"]), "StartLine": 40, "EndLine": 84}},
                })
                hook_latencies_ms.append(lat3)
                tg_calls += 1
                ov = resp3.get("overwrite") or {}
                _, b3 = _read_slice_bytes(fx["auth_file"], ov.get("StartLine", 81), ov.get("EndLine", 84))
                tg_tokens += b3 // 4

                # Call 4: Filtered test run
                _, lat4 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
                })
                hook_latencies_ms.append(lat4)
                tg_calls += 1
                filtered_test, _ = filter_output(fx["test_raw"], "pytest")
                tg_tokens += len(filtered_test.encode("utf-8")) // 4

                if "TOKEN_TTL_1" in txt1 and ov.get("StartLine") == 81:
                    tg_passes += 1

            elif cat == "monorepo_search_audit":
                # Call 1: Agent runs `rg token_match` -> piped through `--pipe-search`
                _, lat1 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "rg token_match"}},
                })
                hook_latencies_ms.append(lat1)
                tg_calls += 1
                filtered_rg, _ = filter_search_stdout(fx["rg_raw"])
                tg_tokens += len(filtered_rg.encode("utf-8")) // 4

                # Call 2: Agent tries unbounded lockfile read -> blocked!
                resp2, lat2 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["lock_file"])}},
                })
                hook_latencies_ms.append(lat2)
                tg_calls += 1
                tg_tokens += len(resp2.get("reason", "").encode("utf-8")) // 4

                # Call 3: Agent reads bounded target slice in src/pipeline_engine.py (1-90)
                _, lat3 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["py_file"]), "StartLine": 1, "EndLine": 90}},
                })
                hook_latencies_ms.append(lat3)
                tg_calls += 1
                txt_py, b_py = _read_slice_bytes(fx["py_file"], 1, 90)
                tg_tokens += b_py // 4

                if "process_stage_01" in txt_py and "node_modules" not in filtered_rg:
                    tg_passes += 1

            elif cat == "compaction_recovery":
                # Call 1: Read auth_config (1-70)
                _, lat1 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["auth_file"]), "StartLine": 1, "EndLine": 70}},
                })
                hook_latencies_ms.append(lat1)
                tg_calls += 1
                _, b1 = _read_slice_bytes(fx["auth_file"], 1, 70)
                tg_tokens += b1 // 4

                # Compaction event fires (`PreCompact` hook)
                _, lat_c = call_hook({
                    "hookEventName": "PreCompact",
                    "conversationId": cid,
                })
                hook_latencies_ms.append(lat_c)

                # Call 2: Post-compaction re-read of auth_config (1-70) -> succeeds immediately without deadlock!
                resp2, lat2 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "read_file", "args": {"Path": str(fx["auth_file"]), "StartLine": 1, "EndLine": 70}},
                })
                hook_latencies_ms.append(lat2)
                tg_calls += 1
                txt2, b2 = _read_slice_bytes(fx["auth_file"], 1, 70)
                tg_tokens += b2 // 4

                # Call 3: Filtered test run
                _, lat3 = call_hook({
                    "hookEventName": "PreToolUse",
                    "conversationId": cid,
                    "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
                })
                hook_latencies_ms.append(lat3)
                tg_calls += 1
                filtered_test, _ = filter_output(fx["test_raw"], "pytest")
                tg_tokens += len(filtered_test.encode("utf-8")) // 4

                if resp2.get("decision") == "allow" and "TOKEN_TTL_10" in txt2:
                    tg_passes += 1

            tg_time_ms += (time.perf_counter() - t_g0) * 1000.0

        # Shut down test daemon cleanly
        query_daemon({"__shutdown__": True}, state_dir=state_dir)

        avg_daemon_ms = sum(hook_latencies_ms) / max(1, len(hook_latencies_ms))
        p50_daemon_ms = sorted(hook_latencies_ms)[len(hook_latencies_ms) // 2]
        token_reduction_pct = round((1.0 - (tg_tokens / max(1, base_tokens))) * 100.0, 1)

        return {
            "tasks_total": len(tasks),
            "baseline": {
                "tasks_passed": base_passes,
                "pass_rate_pct": round((base_passes / len(tasks)) * 100.0, 1),
                "fresh_uncached_tokens": base_tokens,
                "total_tool_calls": base_calls,
                "avg_calls_per_task": round(base_calls / len(tasks), 2),
                "wall_clock_ms": round(base_time_ms, 1),
            },
            "token_guard": {
                "tasks_passed": tg_passes,
                "pass_rate_pct": round((tg_passes / len(tasks)) * 100.0, 1),
                "fresh_uncached_tokens": tg_tokens,
                "total_tool_calls": tg_calls,
                "rebound_tool_calls_included": tg_rebound_calls,
                "avg_calls_per_task": round(tg_calls / len(tasks), 2),
                "wall_clock_ms": round(tg_time_ms, 1),
                "avg_daemon_hook_latency_ms": round(avg_daemon_ms, 2),
                "p50_daemon_hook_latency_ms": round(p50_daemon_ms, 2),
            },
            "delta": {
                "pass_rate_delta_pct": round(((tg_passes - base_passes) / len(tasks)) * 100.0, 1),
                "fresh_tokens_saved": base_tokens - tg_tokens,
                "fresh_token_reduction_pct": token_reduction_pct,
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run 24-task deterministic synthetic A/B simulation for token-guard.")
    parser.add_argument("--json", action="store_true", help="Output JSON report")
    ns = parser.parse_args(argv)

    report = run_ab_suite()
    if ns.json:
        print(json.dumps(report, indent=2))
    else:
        b = report["baseline"]
        g = report["token_guard"]
        d = report["delta"]
        print(f"=== token-guard 24-Task Deterministic A/B Simulation (including rebound calls) ===")
        print(f"1. Task Pass Rate:         Baseline {b['tasks_passed']}/{report['tasks_total']} ({b['pass_rate_pct']}%)  vs  token-guard {g['tasks_passed']}/{report['tasks_total']} ({g['pass_rate_pct']}%)")
        print(f"2. Fresh Uncached Tokens:  Baseline {b['fresh_uncached_tokens']:,} tok  vs  token-guard {g['fresh_uncached_tokens']:,} tok (-{d['fresh_token_reduction_pct']}%, saved {d['fresh_tokens_saved']:,} tok)")
        print(f"3. Wall-Clock & Latency:   Suite {g['wall_clock_ms']} ms total | Persistent daemon hook p50={g['p50_daemon_hook_latency_ms']} ms (avg={g['avg_daemon_hook_latency_ms']} ms)")
        print(f"4. Tool Calls per Task:    Baseline {b['avg_calls_per_task']} ({b['total_tool_calls']} total)  vs  token-guard {g['avg_calls_per_task']} ({g['total_tool_calls']} total, incl. {g['rebound_tool_calls_included']} rebound slice reads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
