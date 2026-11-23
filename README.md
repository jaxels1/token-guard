<p align="left">
  <img src="./assets/logo.svg" alt="token-guard" width="56" height="56" />
</p>

# token-guard

PreToolUse hooks and [`SKILL.md`](./skills/token-guard/SKILL.md) for coding agents like Claude Code, Cursor, or Codex that stop agents from re-reading unchanged files, dumping big files for no reason, or filling context with passing unit test logs. Token Guard relies on no third-party dependencies and is Python 3 stdlib only. It doesn't touch `PreInvocation` prompts to keep the KV prefix cache intact.

![token-guard demo](./assets/demo.gif)

## Installation

```bash
npx skills add jaxels1/token-guard
```

Antigravity:

```bash
git clone https://github.com/jaxels1/token-guard.git ~/.gemini/config/plugins/token-guard
```

- For Claude Code: you register the plugin via `.claude-plugin/plugin.json` (sets up `PreToolUse` for `Read` / `Bash` / `Grep` / `Glob` and `PreCompact` resetting).
- For Cursor: you add `.cursor/hooks.json`, `skills/token-guard/`, and `scripts/` to your workspace or user config.
- For Codex or ChatGPT Desktop App: add `skills/token-guard/` and `scripts/` to `~/.codex/skills/token-guard` and merge `hooks.json` into `~/.codex/hooks.json`.
- Windows note: hooks invoke `python3`. If your system only exposes `python` or `py -3`, update the command in your host `hooks.json` accordingly.

## Audit your session logs

You can run a quick retrospective scan on your existing agent chatlogs to get an upper-bound estimate (`bytes / 4`, before any rebound reads) of context spent on duplicate reads and noisy CLI output:

```bash
python3 scripts/audit_transcript.py --scan-all
```

Here's an example scan across 75 local sessions:
- Codex: 15,024 calls, 53 sessions (~14.75M estimated tool-output tokens)
- Antigravity: 1,143 calls, 22 sessions (~33,825 estimated tool-output tokens)
- 2,964 duplicate reads of unchanged files detected in past logs
- 0 unbounded lockfile/bundle reads detected in that sample
- 45.42 MB reduced to 8.76 MB from noisy CLI/test outputs
- KV prefix cache hit rate: 97.8% (1.19B / 1.22B tokens)
- Estimated recoverable tool-output upper bound: 56.39 MB (~14.78M tokens, ~55.7% of raw uncached tool-output burn in those sessions).

## Benchmark

**What to expect in practice:**
- On short, clean 2–5 turn tasks over small files, `token-guard` saves very little (and loading `SKILL.md` adds ~309 tokens to the prompt prefix, cached after turn 1).
- On longer or noisier coding sessions (verbose test/build logs, >500-line files, monorepo searches, or repeated unchanged reads), realistic net input savings are roughly **13–35%**, depending on how much of the session is tool-output noise.

The 3 measurements below separate a small live LLM sanity check, a synthetic stress test of the hook/filter code, and a retrospective log replay.

### Small Live A/B Sanity Check (2 synthetic tasks, `python3 scripts/live_codex_ab.py`)

This is a **2-task synthetic sanity check** (not a full SWE-bench evaluation) run with `gpt-6.1-terra` in Codex with `token-guard` enabled vs. disabled (`codex.exe exec -m gpt-6.1-terra -c model_reasoning_effort="low" --ephemeral --json`) to verify real model behavior, exit accuracy, and prompt-cache warm-up.

- Task 1: Identify an off-by-one fee multiplier bug in a 560-line Python script (`billing_engine.py`) against `test_billing.py`.
- Task 2: Run 140 test cases with `test_billing.py` and find the failing assertion.

| (`gpt-6.1-terra`) Metric | No Token Guard | With Token Guard | Delta |
| :--- | :--- | :--- | :--- |
| T1 File lookup passed | PASS (0.04 vs 0.05) | PASS (0.04 vs 0.05) | 100% accuracy |
| T1 Tool output size | 3,311 Bytes | 2,547 Bytes | 23.1% reduction |
| T2 Test suite passed | PASS (50.0 vs 40.0) | PASS (50.0 vs 40.0) | 100% accuracy |
| T2 Tool output size | 28,294 Bytes (28.3 KB) | 4,321 Bytes (4.3 KB) | 84.7% reduction |
| T2 Total input tokens | 68,204 | 43,359 | 36.4% reduction (24,845 tokens) |
| T2 New uncached tokens | 12,012 | 9,823 | 18.2% reduction |
| T2 Output tokens | 136 | 93 | 31.6% reduction |
| T2 Tool calls and runtime | 2 calls (9.64s) | 1 call (8.96s) | 50% fewer calls, 7.1% faster |
| (T1 & T2) Total tool output size | 31,605 Bytes | 6,868 Bytes | 78.3% reduction |
| Combined (T1 + T2) total input tokens | 150,091 | 129,815 | -13.5% (-20,276 tok) |

Note on T1 cache: With host skills enabled (`features.skip_host_skill_discovery=false`), the `~/.codex/skills` directory is mounted (~7.2k tokens in that test environment). In T1, uncached tokens increased from 2,783 to 10,040 on cold prefix mount. Once the prefix warmed up in T2, total input tokens dropped by 36.4% and new uncached tokens dropped by 18.2%.

### Synthetic Hook & Filter Stress Test (24 fixtures, `python3 scripts/ab_benchmark.py`)

This is a **deterministic simulation (not an LLM benchmark)** calling `handle_payload()` and `filter_output()` directly against 24 worst-case synthetic fixtures (`large_file_bugfix`, `multi_read_refactor`, `monorepo_search_audit`, `compaction_recovery` — e.g., 1,800-line `node_modules` `rg` dumps, lockfiles, and repeated full-file reads) to verify hook logic, rebound slice reads, and daemon latency. Do not expect `-95%` in real coding sessions — that number only reflects synthetic worst-case fixtures.

| Metric | Baseline Fixture | w/ Token Guard | Delta |
| :--- | :--- | :--- | :--- |
| Fixture checks passed | 24 / 24 (100%) | 24 / 24 (100%) | 0% accuracy loss |
| Simulated uncached tokens (incl. 6 rebound slice reads) | 443,112 | 22,202 | -95.0% synthetic worst-case (420,910 tok) |
| Persistent hook (`hook_daemon.py`) latency | n/a | p50 1.84ms (Average 3.24ms) | loopback socket, Win |
| Tool calls per task (with 6 rebound read slices) | 3.25 (78 total) | 3.25 (78 total) | Outline gives target line range |

### Retrospective Log Replay (`python3 scripts/audit_transcript.py`)

Token counts below are `bytes / 4` upper-bound estimates from replaying 63 historical Codex logs (15,024 tool calls, 561 turns) and one 610-turn Antigravity session (694 tool calls), not a live A/B run.

| Metric | 63 Codex Logs | 610-turn Antigravity log |
| :--- | :--- | :--- |
| Turns / tool calls | 561 turns / 15,024 | 610 turns / 694 calls |
| Reduction of noisy tool command output | 47.61MB reduced to 9.17MB (80.7%) | 11 noisy commands wrapped |
| Duplicate reads of unchanged files | 2,951 (20.56MB) | 7 reads |
| Estimated recoverable upper bound (`bytes / 4`) | 59.00MB (~14.75M tokens) | 63.3KB (~15,825 tokens) |
| KV prefix cache hit rate | 97.8% (1.190B of 1.217B) | 100% prefix safe |
| Share of raw uncached tokens in those logs | 55.5% upper bound (14.75M / 26.56M) | n/a (63.3KB recoverable) |

## Operation

### Reads deduplication & AST File outlines (`scripts/read_guard.py` & `outline_gen.py`)
- Track ranges read from files using `(ConversationID, Path, start line, end line, mtime in nanoseconds, size)`.
- Read ranges reset on `PreCompact` hook and in the presence of `compacted` / `<CONTEXT_SUMMARY>` in the transcript (to prevent the model from getting blocked after context summary).
- Entries expire after 40 tool calls or 15 minutes. If a blocked read is retried, the entry may be retried immediately (`compaction-retry-allowed`).
- Overlap reduction (via `PreToolUse` overwrite). For example, reading 40-120 after reading 1-80 will reduce the range to 81-120.
- If reading a large file (> 500 lines) and an unbounded read is requested, return an outline of what symbols exist in the file (`class`, `def`, `L<start>-<end>`) using Python AST or a fallback to regex patterns.

### Search limitation (`scripts/search_guard.py`)
- `Grep`, `Glob`, `grep_search`, `find_by_name`, or shell `rg` / `grep -r` / `find .` commands intercepted.
- Direct searches are not allowed in `node_modules`, `dist`, `.next`, lockfiles, or minified assets. Sets the `head_limit` to 80 and also limits piped results to 80 matches.

### Allowlist command filter (`scripts/cmd_filter.py`)
- Wraps noisy commands with `python3 ".../cmd_filter.py" --run -- <cmd>` (or `--run-b64` when shell quoting is complex) so exact non-zero exit codes are preserved across POSIX `sh`, `bash`, `cmd.exe`, and PowerShell. Set `TOKEN_GUARD_REWRITE_MODE=pipe` if you prefer prefix-matching `<cmd> 2>&1 | python3 ".../cmd_filter.py" --pipe` for strict command allowlists (note: shells without `pipefail` return exit code `0` in pipe mode).
- If `rtk` binary is available in `PATH`, delegates to `rtk`.
- ANSI codes and successful test lines stripped. Stack traces and exit codes preserved. Full logs written to `%TEMP%/token-guard-logs/`.

### Loopback Socket Daemon (`scripts/hook_daemon.py` & `hook_entry.py`)
- Runs a multithreaded background worker on `127.0.0.1` (`TCP_NODELAY`, per-session random auth token in `daemon.json`, p50 of 1.95 ms), which closes after 30 min inactivity and fails open on any error.

### Skill (`skills/token-guard/SKILL.md`, ~309 Tokens)
- Parallel tool calls, bounded symbolic reads, script-first bulk edits.

## Using with RTK
`cmd_filter.py` checks if `rtk` is on the system using `shutil.which("rtk")`. If RTK is detected, shell compression delegates to `rtk`. `read_guard.py` and `outline_gen.py` will perform read reduction and AST file outlines.

## Using with Caveman or Ponytail
Caveman / Ponytail reduce assistant output, while Token guard reduces inputs from tool calls into context, so they can be used together.
