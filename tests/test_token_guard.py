import concurrent.futures
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import audit_transcript  # noqa: E402
import cmd_filter  # noqa: E402
import hook_daemon  # noqa: E402
import hook_entry  # noqa: E402
import read_guard  # noqa: E402


class TestReadGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_duplicate_and_subrange_until_modified(self):
        target = self.root / "sample.py"
        target.write_text("\n".join(f"line_{i}" for i in range(1, 200)), encoding="utf-8")

        r1 = read_guard.evaluate_read("conv-1", str(target), 10, 80, state_dir=self.state_dir)
        self.assertTrue(r1["allow"])

        # Exact duplicate blocked on 1st duplicate attempt
        r2 = read_guard.evaluate_read("conv-1", str(target), 10, 80, state_dir=self.state_dir)
        self.assertFalse(r2["allow"])
        self.assertEqual(r2["code"], "duplicate-read")

        # Immediate retry of the exact same range (compaction escape hatch) is ALLOWED
        r2_retry = read_guard.evaluate_read("conv-1", str(target), 10, 80, state_dir=self.state_dir)
        self.assertTrue(r2_retry["allow"])
        self.assertEqual(r2_retry["code"], "compaction-retry-allowed")

        # Subrange covered by 10-80 blocked
        r3 = read_guard.evaluate_read("conv-1", str(target), 20, 40, state_dir=self.state_dir)
        self.assertFalse(r3["allow"])

        # Partial overlap (50-120 overlaps 10-80) -> allowed with trimmed overwrite_range [81, 120]
        r_partial = read_guard.evaluate_read("conv-1", str(target), 50, 120, state_dir=self.state_dir)
        self.assertTrue(r_partial["allow"])
        self.assertEqual(r_partial.get("overwrite_range"), [81, 120])

        # Step-age TTL expiry (>40 steps later) automatically refreshes the window so 20-40 is allowed again
        r_ttl = read_guard.evaluate_read("conv-1", str(target), 20, 40, state_dir=self.state_dir, step_idx=100)
        self.assertTrue(r_ttl["allow"])

        # Disjoint range allowed
        r4 = read_guard.evaluate_read("conv-1", str(target), 130, 150, state_dir=self.state_dir)
        self.assertTrue(r4["allow"])

        # Modify file -> invalidates cache so 10-80 is allowed again
        time.sleep(0.02)
        target.write_text("\n".join(f"edited_{i}" for i in range(1, 205)), encoding="utf-8")
        r5 = read_guard.evaluate_read("conv-1", str(target), 10, 80, state_dir=self.state_dir)
        self.assertTrue(r5["allow"])

    def test_blocks_lockfile_minified_vendor_and_oversized_unbounded(self):
        lockfile = self.root / "package-lock.json"
        lockfile.write_text('{"lockfileVersion": 3}', encoding="utf-8")
        self.assertFalse(
            read_guard.evaluate_read("c", str(lockfile), state_dir=self.state_dir)["allow"]
        )
        # Bounded read of lockfile is allowed
        self.assertTrue(
            read_guard.evaluate_read("c", str(lockfile), 1, 20, state_dir=self.state_dir)["allow"]
        )

        minified = self.root / "app.min.js"
        minified.write_text("var a=1;", encoding="utf-8")
        self.assertFalse(
            read_guard.evaluate_read("c", str(minified), state_dir=self.state_dir)["allow"]
        )

        nm_file = self.root / "node_modules" / "pkg" / "index.js"
        nm_file.parent.mkdir(parents=True, exist_ok=True)
        nm_file.write_text("module.exports = {};", encoding="utf-8")
        self.assertFalse(
            read_guard.evaluate_read("c", str(nm_file), state_dir=self.state_dir)["allow"]
        )

        big_file = self.root / "big.py"
        big_file.write_text(
            "\n".join([f"def func_{i}(x, y):\n" + ("    z = x + y\n" * 35) for i in range(18)]),
            encoding="utf-8",
        )
        big_res = read_guard.evaluate_read("c", str(big_file), state_dir=self.state_dir)
        self.assertFalse(big_res["allow"])
        self.assertEqual(big_res["code"], "oversized-file-lines")
        self.assertIn("Structural outline of big.py", big_res["reason"])
        self.assertIn("def func_0(x, y)", big_res["reason"])
        self.assertTrue(
            read_guard.evaluate_read("c", str(big_file), 1, 100, state_dir=self.state_dir)["allow"]
        )

    def test_binary_media_allowed_unbounded(self):
        img = self.root / "screenshot.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 55000))
        res = read_guard.evaluate_read("conv-media", str(img), state_dir=self.state_dir)
        self.assertTrue(res["allow"])
        self.assertEqual(res["code"], "ok")


class TestCmdFilter(unittest.TestCase):
    def test_rewrite_and_skip_pipelines(self):
        # Default mode uses `--run -- <cmd>` so failing exit codes are preserved across shells
        rewritten = cmd_filter.maybe_rewrite_command("pytest -q tests/", prefer_rtk=False)
        self.assertIsNotNone(rewritten)
        self.assertTrue(rewritten.startswith("python3 "))
        self.assertIn("--run -- pytest -q tests/", rewritten)

        # Unbounded rg / grep -r defaults to `--run-search`
        rg_cmd = cmd_filter.maybe_rewrite_command("rg my_symbol", prefer_rtk=False)
        self.assertIsNotNone(rg_cmd)
        self.assertIn("--run-search -- rg my_symbol", rg_cmd)

        # Leading cd stays in cleartext at the front and wraps the tail command
        cd_cmd = cmd_filter.maybe_rewrite_command("cd /repo && pytest -q", prefer_rtk=False)
        self.assertIsNotNone(cd_cmd)
        self.assertTrue(cd_cmd.startswith("cd /repo && python3 "))
        self.assertIn("--run -- pytest -q", cd_cmd)

        # Explicit pipe mode & TOKEN_GUARD_REWRITE_MODE=pipe keep command prefix at the start
        piped = cmd_filter.maybe_rewrite_command("pytest -q tests/", prefer_rtk=False, mode="pipe")
        self.assertIsNotNone(piped)
        self.assertTrue(piped.startswith("pytest -q tests/ 2>&1 | python3 "))
        self.assertIn("--pipe", piped)
        with mock.patch.dict(os.environ, {"TOKEN_GUARD_REWRITE_MODE": "pipe"}):
            env_piped = cmd_filter.maybe_rewrite_command("rg my_symbol", prefer_rtk=False)
            self.assertIsNotNone(env_piped)
            self.assertTrue(env_piped.startswith("rg my_symbol 2>&1 | python3 "))
            self.assertIn("--pipe-search", env_piped)

        # Complex PowerShell cmdlets fall back to `--run-b64` with `--shell pwsh`
        pwsh_cmd = cmd_filter.maybe_rewrite_command('Set-Location "C:\\repo"; python3 -m unittest', prefer_rtk=False)
        self.assertIsNotNone(pwsh_cmd)
        if os.name == "nt":
            self.assertIn("--shell pwsh", pwsh_cmd)
            self.assertIn("--run-b64", pwsh_cmd)

        # Multi-step pipelines & arbitrary chains must still not be rewritten
        self.assertIsNone(cmd_filter.maybe_rewrite_command("pytest && echo done"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git log | head -n 5"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git log --oneline -n 10"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git status -s"))

    def test_default_run_rewrite_preserves_nonzero_exit_code(self):
        with tempfile.TemporaryDirectory() as td:
            failing_test = Path(td) / "test_fail.py"
            failing_test.write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_boom(self):\n"
                "        self.fail('intentional failure')\n",
                encoding="utf-8",
            )
            rewritten = cmd_filter.maybe_rewrite_command(
                f"{sys.executable} -m unittest test_fail.py",
                script_path=str(SCRIPTS_DIR / "cmd_filter.py"),
                prefer_rtk=False,
            )
            # If sys.executable contains spaces (e.g., C:\Program Files\...), rewrite uses --run-b64; otherwise --run
            if rewritten is None:
                # Fallback direct test if executable path wasn't matched as `python3 -m unittest`
                rewritten = cmd_filter.maybe_rewrite_command(
                    "python3 -m unittest test_fail.py",
                    script_path=str(SCRIPTS_DIR / "cmd_filter.py"),
                    prefer_rtk=False,
                )
            self.assertIsNotNone(rewritten)
            proc = subprocess.run(
                rewritten,
                shell=True,
                cwd=td,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("intentional failure", proc.stdout)

    def test_filter_output_strips_ansi_preserves_errors_and_spills_log(self):
        with tempfile.TemporaryDirectory() as td:
            passing = "\n".join(f"\x1b[32m✓\x1b[0m PASS tests/test_{i}.spec.ts" for i in range(120))
            failure = "\n\x1b[31mFAIL tests/auth.spec.ts\x1b[0m\nAssertionError: expected 200 got 500\n"
            raw = f"Collecting logs\rProgress 10%\rProgress 100%\n{passing}\n{failure}\nTest Suites: 1 failed, 120 passed"
            out, meta = cmd_filter.filter_output(raw, "vitest run", log_dir=td)

            self.assertNotIn("\x1b[", out)
            self.assertIn("AssertionError: expected 200 got 500", out)
            self.assertIn("full log:", out)
            self.assertGreater(meta["saved_bytes"], 0)
            self.assertIsNotNone(meta["log_path"])
            self.assertTrue(os.path.isfile(meta["log_path"]))

    def test_run_b64_preserves_nonzero_exit_code(self):
        script_path = str(SCRIPTS_DIR / "cmd_filter.py")
        cmd = f'"{sys.executable}" -c "import sys; print(\'boom error\'); sys.exit(3)"'
        import base64
        b64 = base64.urlsafe_b64encode(cmd.encode("utf-8")).decode("ascii")
        proc = subprocess.run(
            [sys.executable, script_path, "--run-b64", b64],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("boom error", proc.stdout)


class TestHookEntryAdapters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.target = Path(self.tmp.name) / "app.py"
        self.target.write_text("print('hi')\n", encoding="utf-8")
        self.lock = Path(self.tmp.name) / "package-lock.json"
        self.lock.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_antigravity_and_claude_and_cursor_and_gemini_adapters(self):
        # Antigravity allow -> deny on duplicate read_file
        with mock.patch.dict(os.environ, {"PLUGIN_DATA": self.tmp.name}):
            p1 = {
                "hookEventName": "PreToolUse",
                "conversationId": "agy-conv",
                "toolCall": {"name": "read_file", "args": {"Path": str(self.target), "StartLine": 1, "EndLine": 10}},
            }
            self.assertEqual(hook_entry.handle_payload(p1)["decision"], "allow")
            deny = hook_entry.handle_payload(p1)
            self.assertEqual(deny["decision"], "deny")
            self.assertIn("duplicate", deny["reason"].lower())

            # Antigravity run_command rewrite (defaults to `--run --` when no rtk or `rtk` when installed, emits permissionOverrides)
            p_cmd = {
                "hookEventName": "PreToolUse",
                "conversationId": "agy-conv",
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            }
            out_cmd = hook_entry.handle_payload(p_cmd)
            self.assertEqual(out_cmd["decision"], "allow")
            self.assertIn("overwrite", out_cmd)
            rewritten_cmd = out_cmd["overwrite"]["CommandLine"]
            self.assertTrue(
                "--run -- pytest tests/" in rewritten_cmd or rewritten_cmd.startswith("rtk ")
            )
            self.assertIn("permissionOverrides", out_cmd)

            # PreCompact event resets read_guard state so the previously blocked read is allowed again
            compact_evt = {"hookEventName": "PreCompact", "conversationId": "agy-conv"}
            self.assertEqual(hook_entry.handle_payload(compact_evt)["decision"], "allow")
            self.assertEqual(hook_entry.handle_payload(p1)["decision"], "allow")

            # Search guard blocks search inside node_modules and injects head_limit on Claude Grep
            s_block = {
                "hookEventName": "PreToolUse",
                "conversationId": "agy-conv",
                "toolCall": {"name": "grep_search", "args": {"SearchPath": "/repo/node_modules/pkg", "Query": "foo"}},
            }
            self.assertEqual(hook_entry.handle_payload(s_block)["decision"], "deny")

            cl_grep = {
                "hook_event_name": "PreToolUse",
                "session_id": "cl-1",
                "tool_name": "Grep",
                "tool_input": {"pattern": "auth"},
            }
            cl_grep_out = hook_entry.handle_payload(cl_grep)
            self.assertEqual(cl_grep_out["hookSpecificOutput"]["updatedInput"]["head_limit"], 80)

            # Claude Code deny on lockfile + Bash rewrite
            cl_read = {
                "hook_event_name": "PreToolUse",
                "session_id": "cl-1",
                "tool_name": "Read",
                "tool_input": {"file_path": str(self.lock)},
            }
            cl_out = hook_entry.handle_payload(cl_read)
            self.assertEqual(cl_out["hookSpecificOutput"]["permissionDecision"], "deny")

            cl_bash = {
                "hook_event_name": "PreToolUse",
                "session_id": "cl-1",
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            }
            cl_bash_out = hook_entry.handle_payload(cl_bash)
            self.assertIn("updatedInput", cl_bash_out["hookSpecificOutput"])

            # Cursor schema
            cu_read = {
                "hook_event_name": "beforeReadFile",
                "cursor_version": "0.50",
                "conversation_id": "cu-1",
                "tool_name": "Read",
                "tool_input": {"file_path": str(self.lock)},
            }
            self.assertEqual(hook_entry.handle_payload(cu_read)["permission"], "deny")

            # Gemini CLI schema
            gem_cmd = {
                "host": "gemini",
                "hook_event_name": "PreToolUse",
                "session_id": "gem-1",
                "tool_name": "run_shell_command",
                "tool_input": {"command": "pytest -k smoke"},
            }
            self.assertIn("tool_input", hook_entry.handle_payload(gem_cmd))

    def test_fail_open_on_malformed_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("{not-valid-json")):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                code = hook_entry.main()
        self.assertEqual(code, 0)
        self.assertIn("allow", buf.getvalue())


class TestAuditTranscript(unittest.TestCase):
    def test_audits_antigravity_and_claude_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            agy = td_path / "agy.jsonl"
            lines = [
                json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "check"}),
                json.dumps({
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/repo/app.py", "StartLine": 1, "EndLine": 50}},
                        {"name": "read_file", "args": {"Path": "/repo/app.py", "StartLine": 10, "EndLine": 30}},
                        {"name": "read_file", "args": {"Path": "/repo/package-lock.json"}},
                        {"name": "run_command", "args": {"CommandLine": "pytest"}},
                    ],
                }),
                json.dumps({
                    "step_index": 2,
                    "type": "TOOL_RESPONSE",
                    "content": "\n".join(f"PASS test_{i}" for i in range(140)) + "\n1 failed, 140 passed",
                }),
            ]
            agy.write_text("\n".join(lines), encoding="utf-8")

            rep = audit_transcript.audit_transcript(agy)
            self.assertEqual(rep["turns"], 2)
            self.assertEqual(rep["duplicate_reads_detected"], 1)
            self.assertEqual(rep["unbounded_token_bomb_reads"], 1)
            self.assertEqual(rep["noisy_commands_detected"], 1)
            self.assertGreater(rep["net_estimated_tokens_saved"], 0)

            claude = td_path / "claude.jsonl"
            c_lines = [
                json.dumps({"type": "user", "message": {"content": "audit"}}),
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.ts", "offset": 1, "limit": 20}},
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.ts", "offset": 1, "limit": 20}},
                        ]
                    },
                }),
            ]
            claude.write_text("\n".join(c_lines), encoding="utf-8")
            c_rep = audit_transcript.audit_transcript(claude)
            self.assertEqual(c_rep["duplicate_reads_detected"], 1)

    def test_scan_all_discovers_and_deduplicates_antigravity_transcripts(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            claude_dir = home / ".claude" / "projects" / "proj1"
            claude_dir.mkdir(parents=True)
            (claude_dir / "s1.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "message": {"content": "hi"}}),
                        json.dumps(
                            {
                                "type": "assistant",
                                "message": {
                                    "content": [
                                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
                                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
                                    ]
                                },
                            }
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            agy_logs = home / ".gemini" / "antigravity" / "brain" / "conv-1" / ".system_generated" / "logs"
            agy_logs.mkdir(parents=True)
            sample_agy = json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/repo/package-lock.json"}},
                    ],
                }
            )
            # Write both transcript_full.jsonl and transcript.jsonl to verify only one is counted
            (agy_logs / "transcript_full.jsonl").write_text(sample_agy, encoding="utf-8")
            (agy_logs / "transcript.jsonl").write_text(sample_agy, encoding="utf-8")

            summary = audit_transcript.scan_all_transcripts(home_dir=home)
            self.assertEqual(summary["sessions_scanned"], 2)
            self.assertEqual(summary["duplicate_reads_detected"], 1)
            self.assertEqual(summary["unbounded_token_bomb_reads"], 1)
            card = audit_transcript.format_scan_all_scorecard(summary)
            self.assertIn("TOKEN-GUARD", card)
            self.assertIn("Sessions scanned: 2", card)


class TestHookDaemon(unittest.TestCase):
    def test_daemon_concurrent_requests_and_auth_token(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            t = threading.Thread(
                target=hook_daemon.serve_forever,
                args=(state_dir, 10.0),
                daemon=True,
            )
            t.start()

            meta_file = hook_daemon.daemon_meta_path(state_dir)
            deadline = time.time() + 3.0
            while not meta_file.is_file() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(meta_file.is_file())
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            self.assertTrue(meta.get("token"))

            # Unauthenticated raw TCP client must be rejected
            with socket.create_connection(("127.0.0.1", int(meta["port"])), timeout=1.0) as bad_sock:
                bad_sock.sendall(b'{"__ping__": true}\n')
                raw_reply = bad_sock.recv(4096).decode("utf-8", errors="replace")
            self.assertIn("unauthorized", raw_reply)

            # 8 concurrent authenticated requests must all succeed without blocking/timing out
            def _ping_and_eval(idx: int):
                f = state_dir / f"file_{idx}.py"
                f.write_text("x = 1\n", encoding="utf-8")
                payload = {
                    "hookEventName": "PreToolUse",
                    "conversationId": f"parallel-{idx}",
                    "toolCall": {
                        "name": "read_file",
                        "args": {"Path": str(f), "StartLine": 1, "EndLine": 10},
                    },
                }
                try:
                    return hook_daemon.query_daemon(payload, state_dir=state_dir, timeout=1.0)
                finally:
                    hook_daemon.close_client_sockets()

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(_ping_and_eval, range(8)))
                for r in results:
                    self.assertIsNotNone(r)
                    self.assertEqual(r["decision"], "allow")
            finally:
                hook_daemon.query_daemon({"__shutdown__": True}, state_dir=state_dir, timeout=1.0)
                t.join(timeout=3.0)


if __name__ == "__main__":
    unittest.main()
