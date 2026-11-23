# Token Guard — Architecture, Live `GPT-6.1 Terra` A/B, 24-Task Harness & Replay Audit

## 0. Live End-to-End `GPT-6.1 Terra` (`gpt-6.1-terra`) ChatGPT App A/B (`scripts/live_codex_ab.py`)

Executed live via `codex.exe exec -m gpt-6.1-terra` comparing `token-guard` **OFF** vs. **ON**:
- **`T1_large_file_bug_lookup` (560-line `billing_engine.py`)**: `PASS` on both; tool output bytes dropped **`3,311` → `2,547` bytes (`-23.1%`)**.
- **`T2_noisy_test_diagnosis` (140-test verbose `unittest` suite)**: `PASS` on both; tool output bytes dropped **`28,294` → `4,321` bytes (`-84.7%`)**, total input tokens dropped **`68,204` → `43,359` (`-36.4%`, `-24,845` tok)**, fresh uncached tokens dropped **`12,012` → `9,823` (`-18.2%`)**, output tokens dropped **`136` → `93` (`-31.6%`)**, tool calls dropped **`2` → `1` (`-50%`)**, and wall time dropped **`9.64s` → `8.96s` (`-7.1%`)**.
- **Combined (`T1 + T2`)**: `2/2 (100%)` pass rate, **`-78.3%` tool output bytes** (`31,605` → `6,868` bytes), **`-13.5%` total input tokens** (`150,091` → `129,815`, saving `20,276` tokens), **`-20.0%` tool calls**, and **`-11.4%` output tokens**.

## 1. Deterministic 24-Task A/B Benchmark (`scripts/ab_benchmark.py`)

Measures **Baseline Agent** vs. **`token-guard` Enabled** across 24 deterministic coding tasks (`large_file_bugfix`, `multi_read_refactor`, `monorepo_search_audit`, `compaction_recovery`), explicitly including **behavioral rebound tool calls** (e.g. bounded follow-up slice reads after an automatic AST outline block):

| Metric | Baseline Agent | `token-guard` Enabled | Net Delta |
| :--- | :--- | :--- | :--- |
| **Task Pass Rate** | `24 / 24` (`100.0%`) | **`24 / 24` (`100.0%`)** | **`0.0%` accuracy drop** |
| **Fresh Uncached Tokens (net of rebound calls)** | `443,112` tok | **`22,292` tok** | **`-95.0%` (`420,820` tok saved)** |
| **Persistent Daemon Hook Latency (`hook_daemon.py`)** | N/A | **`p50 = 1.95 ms` (`avg = 3.33 ms`)** | Sub-2ms median socket round-trip |
| **Tool Calls per Task (incl. 6 rebound slice reads)** | `3.25` (`78` total) | **`3.25` (`78` total)** | AST outlines replace blind grep turns |

## 2. Offline Transcript Replay Analysis (`scripts/audit_transcript.py`)

Audited retroactively across **63 real ChatGPT / OpenAI Codex Desktop transcripts** (`15,024` tool calls across `561` turns) and a **610-turn Antigravity session** (`694` tool calls) to quantify upper-bound recoverable waste:

| Metric (Offline Replay Upper Bound) | 63 ChatGPT / Codex App Sessions | 610-Turn Antigravity Session |
| :--- | :--- | :--- |
| **Turns / Tool Calls** | `561` turns / `15,024` calls | `610` turns / `694` calls |
| **Duplicate Unchanged Reads Flagged** | **`2,951` redundant reads** (`~20.56 MB`) | **`7` redundant reads** |
| **Oversized / Noisy Tool Output Compression** | `47.61 MB` → `9.17 MB` (**`-80.7%`**) | `11` noisy commands wrapped |
| **Total Recoverable Payload** | **`59.00 MB` (`~14,748,795` tokens)** | **`63,300` bytes (`~15,825` tokens)** |
| **KV Prefix Cache Hit Rate** | **`97.8%`** (`1.190B / 1.217B` input tokens) | **`100%` prefix-safe** (`0` `PreInvocation` hooks) |
| **Recoverable Share of Fresh Uncached Input Tokens** | **`55.5%`** (`14.75M` of `26.56M` fresh tokens) | **`~2.25×` theoretical quota runway** |

## 3. Five Production Safeguards

1. **Compaction-Aware Read Dedup**: Resets on `PreCompact` hook events and transcript `"compacted"` / `<CONTEXT_SUMMARY>` markers, expires entries after `40` steps or `15` minutes, and allows an immediate retry (`compaction-retry-allowed`) so the agent never deadlocks after context eviction.
2. **Automatic AST & Regex Structural Outlines (`outline_gen.py`)**: Returns function/class signatures and exact `L<start>-<end>` line numbers whenever an unbounded read of a `>500`-line file is intercepted.
3. **Monorepo Search Explosion Guard (`search_guard.py`)**: Blocks searches targeting `node_modules`, `dist`, `.next`, lockfiles, and minified assets, injects `head_limit: 80` / `Excludes`, and caps piped `rg` / `grep -r` / `find .` output at `80` matches.
4. **Allowlist-Friendly `--pipe` Rewriting (`cmd_filter.py`)**: Preserves the original command prefix (`<cmd> 2>&1 | python3 cmd_filter.py --pipe`) so prefix allowlists match without popups, and delegates to `rtk` when installed.
5. **Sub-2ms Persistent Background Worker (`hook_daemon.py`)**: Keeps state and compiled rules hot in memory over a persistent `TCP_NODELAY` loopback socket (`p50 = 1.95 ms`).
