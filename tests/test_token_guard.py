import io
import json
import os
import subprocess
import sys
import tempfile
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

        # Exact duplicate blocked
        r2 = read_guard.evaluate_read("conv-1", str(target), 10, 80, state_dir=self.state_dir)
        self.assertFalse(r2["allow"])
        self.assertEqual(r2["code"], "duplicate-read")

        # Subrange covered by 10-80 blocked
        r3 = read_guard.evaluate_read("conv-1", str(target), 20, 40, state_dir=self.state_dir)
        self.assertFalse(r3["allow"])

        # Partial overlap (50-120 overlaps 10-80) -> allowed with trimmed overwrite_range [81, 120]
        r_partial = read_guard.evaluate_read("conv-1", str(target), 50, 120, state_dir=self.state_dir)
        self.assertTrue(r_partial["allow"])
        self.assertEqual(r_partial.get("overwrite_range"), [81, 120])

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

        big_file = self.root / "big.txt"
        big_file.write_text("\n".join("row" for _ in range(620)), encoding="utf-8")
        big_res = read_guard.evaluate_read("c", str(big_file), state_dir=self.state_dir)
        self.assertFalse(big_res["allow"])
        self.assertEqual(big_res["code"], "oversized-file-lines")
        self.assertTrue(
            read_guard.evaluate_read("c", str(big_file), 1, 100, state_dir=self.state_dir)["allow"]
        )


class TestCmdFilter(unittest.TestCase):
    def test_rewrite_and_skip_pipelines(self):
        rewritten = cmd_filter.maybe_rewrite_command("pytest -q tests/", prefer_rtk=False)
        self.assertIsNotNone(rewritten)
        self.assertIn("--run-b64", rewritten)

        # Leading cd / Set-Location before a noisy command MUST be detected and rewritten
        cd_cmd = cmd_filter.maybe_rewrite_command('cd /repo && pytest -q', prefer_rtk=False)
        self.assertIsNotNone(cd_cmd)
        pwsh_cmd = cmd_filter.maybe_rewrite_command('Set-Location "C:\\repo"; python3 -m unittest', prefer_rtk=False)
        self.assertIsNotNone(pwsh_cmd)
        if os.name == "nt":
            self.assertIn("--shell pwsh", pwsh_cmd)

        # Multi-step pipelines & arbitrary chains must still not be rewritten
        self.assertIsNone(cmd_filter.maybe_rewrite_command("pytest && echo done"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git log | head -n 5"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git log --oneline -n 10"))
        self.assertIsNone(cmd_filter.maybe_rewrite_command("git status -s"))

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

            # Antigravity run_command rewrite
            p_cmd = {
                "hookEventName": "PreToolUse",
                "conversationId": "agy-conv",
                "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest tests/"}},
            }
            out_cmd = hook_entry.handle_payload(p_cmd)
            self.assertEqual(out_cmd["decision"], "allow")
            self.assertIn("overwrite", out_cmd)
            self.assertIn("--run-b64", out_cmd["overwrite"]["CommandLine"])

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


if __name__ == "__main__":
    unittest.main()
