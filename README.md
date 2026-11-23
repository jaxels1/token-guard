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

You can run a quick check to see the context you wasted on useless stuff like double reading or noisy CLI output in your existing agent chatlogs:

```bash
python3 scripts/audit_transcript.py --scan-all
```

Here's an example output across 75 local sessions:
- Codex: 15,024 calls, 53 sessions which is ~14.75M tokens
- Antigravity: 1,143 calls, 22 sessions which is ~33,825 tokens
- 2,964 double reads of identical files stopped before tokens are wasted
- 0 successful lockfile/bundle reads without bounds
- 45.42 MB reduced to 8.76 MB from noisy CLI/test outputs
- KV prefix cache hit rate: pretty good 97.8% (1.19B / 1.22B tokens)
- Recoverable context waste: 56.39 MB, which is roughly 14.78M tokens, -55.7% of new uncached token burn.

## Benchmark

Shown here are 3 different benchmarks to separate live model performance (incl. rebound tool calls) from replayed logs.

### `gpt-6.1-terra` benchmark (A/B, `python3 scripts/live_codex_ab.py`)

Selected tasks run using `gpt-6.1-terra` in Codex with token-guard enabled versus the same task with token-guard disabled (`codex.exe exec -m gpt-6.1-terra -c model_reasoning_effort="low" --ephemeral --json`).

- Task 1: Identify a hidden off-by-one error in a fee multiplier within a 560-line Python script called `billing_engine.py`.
- Task 2: Run 140 testing cases with `test_billing.py` and find the failing assertion.

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

Note on T1 cache: With host skills enabled (`features.skip_host_skill_discovery=false`), the user's `~/.codex/skills` directory is automatically mounted (~7.2k tokens). In turn 1, uncached tokens increased from 2,783 (with previous prefix) to 10,040 (as new skills mounted). Once the prefix warmed up (T2), total input tokens and also new uncached tokens dropped by 36.4% and 18.2%.

### Deterministic Task Benchmark (24 tasks, `python3 scripts/ab_benchmark.py`)

24 tasks are run (`large_file_bugfix`, `multi_read_refactor`, `monorepo_search_audit`, `compaction_recovery`) in a fixture project. This benchmark counts the rebound tool calls made before and after outline blocks.

| Metric | Baseline | w/ Token Guard | Delta |
| :--- | :--- | :--- | :--- |
| Task complete | 24 / 24 (100%) | 24 / 24 (100%) | 0% accuracy loss |
| New uncached tokens including rebound reads | 443,112 | 22,292 | 95.0% reduction (420,820 tokens) |
| Persistent hook (`hook_daemon.py`) latency | n/a | p50 1.95ms (Average 3.33ms) | loopback socket, Win |
| Tool calls per task (with 6 rebound read slices) | 3.25 (78 total) | 3.25 (78 total) | Tool outline means no grep loops |

### Log Benchmark (`python3 scripts/audit_transcript.py`)

An audit of 63 Codex logs that include actual code development (15,024 tool calls, 561 turns) and one Antigravity session with 610 turns and 694 tool calls.

| Metric | 63 Codex Logs | 610-turn Antigravity log |
| :--- | :--- | :--- |
| Turns / tool calls | 561 turns / 15,024 | 610 turns / 694 calls |
| Reduction of noisy tool command output | 47.61MB reduced to 9.17MB (80.7%) | 11 noisy commands wrapped |
| Double reads of identical files | 2,951 (20.56MB) | 7 reads |
| Total recoverable payload | 59.00MB (14.75M tokens) | 63.3KB (15,825 tokens) |
| KV prefix cache hit rate | 97.8% (1.190B of 1.217B) | 100% prefix safe |
| Recoverable proportion of new uncached tokens | 55.5% (14.75M / 26.56M) | ~2.25x more quota |

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
