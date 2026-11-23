<p align="left">
  <img src="./assets/logo.svg" alt="token-guard logo" width="64" height="64" />
</p>

# Token Guard — Cross-Agent Token & AI-Usage Optimizer

**Token Guard (`token-guard`)** is a cross-agent plugin and `SKILL.md` bundle built for **Antigravity, Claude Code, Cursor, OpenAI Codex, and Gemini CLI**. Instead of optimizing only assistant wording (like `caveman`) or only shell stdout (like `RTK`), it enforces savings across all 5 layers of an agent session without breaking your KV prompt prefix cache.

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

### Claude Code / Cursor

- **Claude Code**: Point `--plugin-dir` (or plugin install) to `.claude-plugin/plugin.json`.
- **Cursor**: Copy `.cursor/hooks.json`, `skills/token-guard/`, and `scripts/` into your workspace or config root.

## Verified Benchmark Proof (63 Real Sessions · 15,718 Tool Calls)

Audited directly using `scripts/audit_transcript.py` against **63 real ChatGPT / OpenAI Codex Desktop app transcripts** (`~/.codex/sessions/**/rollout-*.jsonl`, `15,024` tool calls across `561` turns) plus a **610-turn Antigravity production session** (`694` tool calls):

### Aggregate Results (63 ChatGPT / Codex Sessions)

| Metric | Measured Value | Impact |
| :--- | :--- | :--- |
| **Sessions / Turns / Tool Calls Audited** | `63` sessions · `561` turns · `15,024` tool calls | Real multi-project engineering workload |
| **Oversized & Noisy Command / Tool Output** | `47.61 MB` raw → `9.17 MB` filtered | **`-80.7%` stdout/output bloat stripped** (full logs spilled to `%TEMP%`) |
| **Duplicate Unchanged File Reads Caught** | `2,951` redundant reads | **`~20.56 MB` of repeated file slices blocked/trimmed** |
| **Total Single-Pass Payload Eliminated** | `58,995,180` bytes (`59.00 MB`) | **`~14,748,795` tokens eliminated** |
| **Recorded Input Tokens vs. KV Prefix Cache** | `1,216,941,128` total · `1,190,380,544` cached | **`97.8%` KV cache hit rate preserved** (`0` `PreInvocation` cache breaks) |
| **Fresh Uncached Input Token Burn (Quota-Limiting)** | `26,560,584` fresh tokens (`14.75M` eliminated) | **`-55.5%` fresh uncached token burn (~2.25× quota runway)** |

### Sample Per-Session Audit Breakdown

| Session Transcript | Turns / Calls | Recorded Input Tokens (KV Cache Hit) | Duplicate Reads Caught | Command / Tool Bytes Stripped | Net Estimated Savings |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ChatGPT / Codex App** (`rollout-2026-10-30-...-10c6a9c1e421.jsonl`) | `58` turns / `1,476` calls | `197,287,766` (`98.2%` cached) | **`200`** | `2,654,949` bytes (`2.65 MB`) | **`3,998,949` bytes (`~999,737` tokens)** |
| **ChatGPT / Codex App** (`rollout-2026-11-21-...-a91877ba6ab9.jsonl`) | `4` turns / `7` calls | `513,419` (`91.2%` cached) | `0` | `78,668` bytes | **`78,668` bytes (`~19,667` tokens)** |
| **ChatGPT / Codex App** (`rollout-2026-11-22-...-def532965ef4.jsonl`) | `3` turns / `2` calls | `187,746` (`83.5%` cached) | `0` | `40,835` bytes | **`40,835` bytes (`~10,208` tokens)** |
| **Antigravity IDE** (`transcript_full.jsonl`) | `610` turns / `694` calls | N/A | **`7`** | `11` noisy commands wrapped | **`63,300` bytes (`~15,825` tokens)** |

### Reproduce on Your Own Machine

Run `audit_transcript.py` on any Antigravity (`transcript.jsonl`), Claude Code (`~/.claude/projects/**/*.jsonl`), or ChatGPT / Codex Desktop (`~/.codex/sessions/**/rollout-*.jsonl`) session:

```bash
python3 scripts/audit_transcript.py path/to/transcript.jsonl
python3 scripts/audit_transcript.py ~/.codex/sessions/2026/11/22/rollout-*.jsonl --json
```

## 5-Layer Architecture

1. **Cache-Safe Skill (`skills/token-guard/SKILL.md`, `~309` tokens)**: Structural terseness (no preamble, recaps, code-echoing, or pidgin), parallel tool batching, symbol-first navigation, script-first bulk ops, and subagent tiering. Zero dynamic per-turn `PreInvocation` injections so KV prefix caches remain 100% hot.
2. **Universal Host Hook Adapter (`scripts/hook_entry.py`)**: Zero-dependency Python 3 stdlib entrypoint that auto-detects Antigravity, Claude Code, Cursor, or Gemini CLI payloads on stdin and emits native schema responses (fail-open).
3. **Deterministic Read Guard (`scripts/read_guard.py`)**:
   - Blocks duplicate re-reads (and covered subranges) of unchanged files keyed by `(conversationId, path, range, mtime_ns, size)`.
   - Auto-trims partially overlapping bounded reads (e.g. `10–80` already read, `50–120` requested → rewritten to `81–120` via `PreToolUse` `overwrite`).
   - Blocks unbounded reads of lockfiles, minified/bundle/map assets, vendor/build folders (`node_modules`, `.next`, `dist`, etc.), and large files (`>500` lines / `>40 KB`).
4. **Noisy Command Filter (`scripts/cmd_filter.py`)**: Rewrites verbose test/build/lint/install/git commands (including `cd <dir> && ...` and Windows PowerShell `Set-Location <dir>; ...` prefixes) safely via `rtk` (if present) or `--run-b64`, stripping ANSI/progress/passing boilerplate while preserving errors, exact exit codes, and spilling full unfiltered logs to temp files.
5. **Transcript Auditor (`scripts/audit_transcript.py`)**: Parses Antigravity, Claude Code, and ChatGPT / Codex `.jsonl` transcripts (plus live `metrics.jsonl` hook telemetry) to quantify turns, duplicate reads, token bombs blocked, KV cache hit rate, bytes stripped, and net tokens saved:

```bash
python3 scripts/audit_transcript.py path/to/transcript.jsonl --json
```

## Run Unit Tests

```bash
python3 -m unittest discover -s tests -v
```
