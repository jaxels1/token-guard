# Token Guard — Architecture & Benchmarks

## Why Single-Layer Optimizers Leave Savings on the Table

| Tool / Approach | What It Optimizes | Blind Spots |
| :--- | :--- | :--- |
| **`caveman`** | Compresses assistant prose output | Ignores input context bloat (`70–85%` of session tokens), injects per-turn `PreInvocation` text, breaks clarity on technical specs |
| **`RTK`** | Compresses CLI stdout in shell | Misses `read_file` duplicate re-reads, lockfile/bundle token bombs, multi-turn sequential tool chatter, and transcript verification |
| **`context-mode`** | Indexes/filters large tool outputs via MCP | Heavyweight MCP setup; does not guard native `read_file` duplicate ranges across turns or optimize assistant reasoning structure |
| **AST-only MCPs** | Symbol lookups instead of full-file reads | No command output filtering, no lockfile/build-artifact gate, no subagent tiering, no post-session transcript savings audit |
| **`token-guard`** | **All 5 layers in 1 zero-dependency bundle** | Combines cache-safe `<350`-token skill rules + deterministic `read_guard` + noisy `cmd_filter` + fail-open host adapter + JSONL auditor |

## KV Prefix Cache Invariance

Prompt caching relies on byte-identical prefixes across turns. Calling `PreInvocation` hooks that inject dynamic turn counters, timestamps, or fluctuating reminder banners invalidates cached input tokens and increases cost/latency. `token-guard` registers **zero `PreInvocation` hooks** and uses static, cache-safe `SKILL.md` progressive disclosure plus deterministic `PreToolUse` guards.

## Transcript Audit Methodology

`scripts/audit_transcript.py` parses both Antigravity (`step_index` / `tool_calls` JSONL) and Claude Code (`message.content` `tool_use` / `tool_result` JSONL) transcripts:

1. Counts user + assistant turns and total tool invocations.
2. Flags duplicate unchanged range reads (exact or covered by an earlier wider read of the same file with no intervening edits).
3. Flags unbounded reads of lockfiles, minified/map artifacts, or `>500`-line files.
4. Estimates raw vs compressed stdout bytes (or exact byte deltas recorded in `metrics.jsonl` by active hooks) and converts byte savings to estimated tokens (`bytes / 4`).
