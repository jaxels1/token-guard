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

## Transcript Audit Methodology & Verified Production Benchmark

`scripts/audit_transcript.py` parses Antigravity (`step_index` / `tool_calls` JSONL), Claude Code (`message.content` `tool_use` / `tool_result` JSONL), and ChatGPT / OpenAI Codex Desktop (`response_item` + `token_usage_record` `rollout-*.jsonl`) transcripts:

1. Counts user + assistant turns, total tool invocations, and recorded `input_tokens` / `cached_input_tokens`.
2. Flags duplicate unchanged range reads (exact or covered by an earlier wider read of the same file with no intervening edits) and auto-trims partial range overlaps.
3. Flags unbounded reads of lockfiles, minified/map artifacts, or `>500`-line files.
4. Measures raw vs compressed stdout/tool output bytes (plus exact byte deltas recorded in `metrics.jsonl` by active hooks) and converts byte savings to estimated tokens (`bytes / 4`).

### Verified Benchmark Across 63 Real ChatGPT / Codex Sessions + 610-Turn Antigravity Session

| Metric | 63 ChatGPT / Codex App Sessions | 610-Turn Antigravity Session |
| :--- | :--- | :--- |
| **Turns / Tool Calls** | `561` turns / `15,024` calls | `610` turns / `694` calls |
| **Duplicate Unchanged Reads Caught** | **`2,951` redundant reads** | **`7` redundant reads** |
| **Oversized / Noisy Tool Output Compression** | `47.61 MB` → `9.17 MB` (**`-80.7%`**) | `11` noisy commands wrapped |
| **Total Payload Eliminated** | **`59.00 MB` (`~14,748,795` tokens)** | **`63,300` bytes (`~15,825` tokens)** |
| **KV Prefix Cache Hit Rate** | **`97.8%`** (`1.190B / 1.217B` input tokens) | **`100%` prefix-safe** (`0` `PreInvocation` hooks) |
| **Fresh Uncached Input Token Burn Reduction** | **`-55.5%`** (`14.75M` of `26.56M` fresh tokens) | **`~2.25×` longer quota runway** |
