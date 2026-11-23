<p align="left">
  <img src="./assets/logo.svg" alt="token-guard logo" width="64" height="64" />
</p>

# Token Guard — Cross-Agent Token & AI-Usage Optimizer

**Token Guard (`token-guard`)** is a zero-dependency cross-agent plugin and `SKILL.md` bundle built for **Antigravity, Claude Code, Cursor, OpenAI Codex (ChatGPT Desktop), and Gemini CLI**. It combines compaction-aware read deduplication, automatic AST/regex structural outlines on large files, monorepo search explosion capping, allowlist-friendly command stdout filtering, a `<2ms` persistent background hook daemon, and reproducible live A/B + transcript replay benchmarks.

![token-guard skill demo](./assets/demo.gif)

## Quick Install

### Cross-Agent Skill (`skills.sh`)

```bash
npx skills add jaxels1/token-guard
```

### Antigravity Global Plugin

```bash
git clone https://github.com/jaxels1/token-guard.git ~/.gemini/config/plugins/token-guard
```

### Claude Code / Cursor / ChatGPT (Codex)

- **Claude Code**: Point `--plugin-dir` (or plugin install) to `.claude-plugin/plugin.json` (includes `PreToolUse` on `Read|Bash|Grep|Glob` and `PreCompact` reset hooks).
- **Cursor**: Copy `.cursor/hooks.json`, `skills/token-guard/`, and `scripts/` into your workspace or config root.
- **ChatGPT / OpenAI Codex Desktop**: Clone or copy `skills/token-guard/` and `scripts/` into `~/.codex/skills/token-guard`.

---

## Verified Benchmarks: Live `GPT-6.1 Terra` CLI A/B + 24-Task Harness + 63-Session Replay

We publish **three distinct tiers of benchmark evidence**—so you can see real end-to-end LLM behavior on **`gpt-6.1-terra` (`GPT-6.1 Terra` in the ChatGPT / Codex Desktop app)**, deterministic 24-task A/B harness numbers (including rebound calls), and 63-session historical replay bounds.

### 1. Live End-to-End `GPT-6.1 Terra` (`gpt-6.1-terra`) ChatGPT App A/B Benchmark (`scripts/live_codex_ab.py`)

Executed live via the ChatGPT / Codex Desktop app CLI (`codex.exe exec -m gpt-6.1-terra -c model_reasoning_effort="low" --ephemeral --json`) with `token-guard` **OFF** vs. **ON** (`~/.codex/hooks.json` + `~/.codex/skills/token-guard` enabled) across two representative coding tasks:
- **`T1_large_file_bug_lookup`**: Locate an off-by-one fee multiplier bug inside a 560-line Python module (`billing_engine.py`, `compute_fee_tier_14`).
- **`T2_noisy_test_diagnosis`**: Run a 140-test verbose `unittest` suite (`test_billing.py`) and diagnose the single failing test case and expected vs. actual values.

| Task / Metric (`gpt-6.1-terra` Live Run) | `token-guard` OFF | `token-guard` ON | Live Delta |
| :--- | :--- | :--- | :--- |
| **`T1` Large-File Lookup — Task Pass** | `PASS` (`0.04` vs `0.05`) | **`PASS` (`0.04` vs `0.05`)** | **`100%` accuracy** |
| **`T1` Large-File Lookup — Tool Output Bytes** | `3,311` bytes | **`2,547` bytes** | **`-23.1%` tool output bytes** |
| **`T2` Noisy 140-Test Diagnosis — Task Pass** | `PASS` (`50.0` vs `40.0`) | **`PASS` (`50.0` vs `40.0`)** | **`100%` accuracy** |
| **`T2` Noisy 140-Test Diagnosis — Tool Output Bytes** | `28,294` bytes (`28.3 KB`) | **`4,321` bytes (`4.3 KB`)** | **`-84.7%` stdout bytes fed to model** |
| **`T2` Noisy 140-Test Diagnosis — Total Input Tokens** | `68,204` tokens | **`43,359` tokens** | **`-36.4%` total input tokens (`-24,845` tok)** |
| **`T2` Noisy 140-Test Diagnosis — Fresh Uncached Tokens** | `12,012` tokens | **`9,823` tokens** | **`-18.2%` fresh uncached tokens** |
| **`T2` Noisy 140-Test Diagnosis — Output Tokens** | `136` tokens | **`93` tokens** | **`-31.6%` output tokens** |
| **`T2` Noisy 140-Test Diagnosis — Tool Calls & Wall Time** | `2` calls · `9.64s` | **`1` call · `8.96s`** | **`-50%` tool calls · `-7.1%` faster** |
| **Combined (`T1 + T2`) — Total Tool Output Bytes** | `31,605` bytes | **`6,868` bytes** | **`-78.3%` total tool output bytes** |
| **Combined (`T1 + T2`) — Total Input Tokens** | `150,091` tokens | **`129,815` tokens** | **`-13.5%` total input tokens (`-20,276` tok)** |
| **Combined (`T1 + T2`) — Tool Calls & Output Tokens** | `5` calls · `351` out tok | **`4` calls · `311` out tok** | **`-20.0%` calls · `-11.4%` output tokens** |

> **Transparency note on `T1` prompt cache**: Toggling `features.skip_host_skill_discovery` from `true` (`OFF`) to `false` (`ON`) loads the host `~/.codex/skills` catalog (~7.2k tokens of installed skill descriptions) into the initial turn-1 prefix (`T1` uncached tokens `2,783` warm cache → `10,040` on skill-catalog mount). Once the prefix is warm (`T2`), `token-guard` cuts both total input tokens (`-36.4%`) and fresh uncached tokens (`-18.2%`) while completing `7.1%` faster.

Reproduce locally with your ChatGPT / Codex app CLI:

```bash
python3 scripts/live_codex_ab.py
```

### 2. Deterministic 24-Task A/B Benchmark Harness (`python3 scripts/ab_benchmark.py`)

Runs 24 deterministic coding tasks across four categories (`large_file_bugfix`, `multi_read_refactor`, `monorepo_search_audit`, and `compaction_recovery`) against a multi-file fixture repo—explicitly counting **behavioral rebound tool calls** (e.g. when `read_guard.py` blocks an unbounded `>500`-line read and returns an AST structural outline, the agent issues a follow-up bounded `StartLine`/`EndLine` read for the target function):

| Metric | Baseline Agent | `token-guard` Enabled | Net Delta |
| :--- | :--- | :--- | :--- |
| **1. Task Pass Rate** | `24 / 24` (`100.0%`) | **`24 / 24` (`100.0%`)** | **`0.0%` accuracy loss** |
| **2. Fresh Uncached Tokens (incl. rebound calls)** | `443,112` tokens | **`22,292` tokens** | **`-95.0%` (`420,820` tokens saved)** |
| **3. Persistent Daemon Hook Latency (`hook_daemon.py`)** | N/A | **`p50 = 1.95 ms` (`avg = 3.33 ms`)** | Sub-2ms median overhead on Windows |
| **4. Tool Calls per Task (incl. 6 rebound slice reads)** | `3.25` (`78` total) | **`3.25` (`78` total)** | AST outlines eliminate extra search turns |

Reproduce locally:

```bash
python3 scripts/ab_benchmark.py
python3 scripts/ab_benchmark.py --json
```

### 3. Offline Transcript Replay Analysis (63 Real ChatGPT / Codex Sessions + 610-Turn Antigravity Session)

Running `scripts/audit_transcript.py` retroactively over **63 real ChatGPT / OpenAI Codex Desktop transcripts** (`~/.codex/sessions/**/rollout-*.jsonl`, `15,024` tool calls across `561` turns) plus a **610-turn Antigravity session** (`694` tool calls) measures the upper-bound duplicate-read and command-output waste present in unguarded sessions:

| Metric (Offline Replay Upper Bound) | 63 ChatGPT / Codex Sessions | 610-Turn Antigravity Session |
| :--- | :--- | :--- |
| **Turns / Tool Calls Audited** | `561` turns · `15,024` calls | `610` turns · `694` calls |
| **Oversized & Noisy Command / Tool Output** | `47.61 MB` raw → `9.17 MB` filtered (**`-80.7%`**) | `11` noisy commands wrapped |
| **Duplicate Unchanged File Reads Flagged** | **`2,951` redundant reads** (`~20.56 MB`) | **`7` redundant reads** |
| **Total Recoverable Payload** | **`59.00 MB` (`~14,748,795` tokens)** | **`63,300` bytes (`~15,825` tokens)** |
| **Recorded KV Prefix Cache Hit Rate** | **`97.8%`** (`1.190B / 1.217B` input tokens) | **`100%` prefix-safe** (`0` `PreInvocation` hooks) |
| **Recoverable Share of Fresh Uncached Input Tokens** | **`55.5%`** (`14.75M` of `26.56M` uncached tokens) | **`~2.25×` theoretical quota runway** |

> **Note on Replay vs. Live Sessions**: Offline replay measures exact bytes sent in historical logs without re-running the model. In live sessions, follow-up slice reads after an outline block consume a small rebound budget (~35–80 lines per slice), which is why `scripts/ab_benchmark.py` is provided alongside `scripts/audit_transcript.py`.

---

## Architecture & Key Capabilities

1. **Compaction-Aware Read Guard + Automatic AST Outlines (`scripts/read_guard.py`, `scripts/outline_gen.py`)**:
   - **Context Compaction Reset**: Listens for `PreCompact` lifecycle events and scans transcript tails (`64 KB`) for `"compacted"` / `<CONTEXT_SUMMARY>` markers, resetting cached read ranges automatically so the agent never suffers post-compaction amnesia.
   - **Step & Wall-Clock TTL + Retry Escape Hatch**: Expires cached ranges after `40` tool steps or `15` minutes, and allows an immediate second attempt (`compaction-retry-allowed`) if the model explicitly retries a blocked range.
   - **Automatic Partial-Overlap Trimming**: If lines `1–80` were read and the agent requests `40–120`, `PreToolUse` `overwrite` trims the read to `81–120`.
   - **Automatic AST / Regex Structural Outlines on Large Files (`>500` lines)**: Instead of a dead-end error on oversized files, returns a compact symbol outline (classes, functions, methods, and exact `L<start>-<end>` line numbers via Python `ast` or regex heuristics) so the agent jumps straight to the target 30-line slice in one follow-up call.
2. **Grep / Search Explosion Guard (`scripts/search_guard.py`)**:
   - Intercepts `grep_search`, `find_by_name`, `Grep`, `Glob`, and shell `rg` / `grep -r` / `find .` calls.
   - Blocks direct searches inside `node_modules`, `dist`, `.next`, lockfiles, and minified bundles; auto-injects exclusion lists / `head_limit: 80`; and caps piped search stdout at `80` matches with a summary footer (`[token-guard search cap: ...and N more matches across M files]`).
3. **Allowlist-Friendly Command Output Filter (`scripts/cmd_filter.py`)**:
   - Preserves the **original command prefix at the start of the command string**:
     ```bash
     pytest -q tests/ 2>&1 | python3 "/path/to/cmd_filter.py" --pipe
     ```
     so workspace permission allowlists (`"allow": ["npm test", "pytest", "git diff"]`) match cleanly without opaque base64 popups (`--run-b64` is reserved strictly as a fallback for multi-statement Windows PowerShell cmdlets).
   - Automatically delegates to **`rtk`** when the `rtk` binary is installed on `PATH`.
4. **Sub-2ms Persistent Background Worker (`scripts/hook_daemon.py` + `scripts/hook_entry.py`)**:
   - Eliminates cold file-I/O and state-deserialization latency by keeping `read_guard`, `cmd_filter`, `search_guard`, and `outline_gen` hot in memory over a persistent local `TCP_NODELAY` loopback socket (`p50 = 1.95 ms`), auto-shutting down after 30 minutes of idle time and failing open if unavailable.
5. **Cache-Safe Skill (`skills/token-guard/SKILL.md`, `~309` tokens)**:
   - Structural terseness, parallel tool batching, symbol-first narrow reads, script-first bulk ops, and subagent model tiering—with **zero `PreInvocation` hooks** so the KV prompt prefix cache remains untouched.

## How `token-guard` Fits With RTK, Caveman & Ponytail

| Tool | Strength | How `token-guard` Integrates / Complements |
| :--- | :--- | :--- |
| **`RTK`** | High-speed compiled Rust CLI & pipeline proxy for shell/tool output | `cmd_filter.py` auto-detects `rtk` on `PATH` (`prefer_rtk=True`) and delegates supported CLI commands directly to `rtk`, while adding `PreCompact`-aware read-range trimming and AST file outlines |
| **`caveman` / `ponytail`** | Output brevity, code-mode discipline, and minimal diffs | Pair with `token-guard`'s `PreToolUse` guards (`read_guard.py`, `search_guard.py`, `outline_gen.py`) for full input + output coverage |

## Run Tests & Benchmarks

```bash
python3 -m unittest discover -s tests -v
python3 scripts/ab_benchmark.py
python3 scripts/audit_transcript.py path/to/transcript.jsonl --json
```
