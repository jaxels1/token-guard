<p align="left">
  <img src="./assets/logo.svg" alt="token-guard logo" width="64" height="64" />
</p>

# Token Guard — Cross-Agent Token & AI-Usage Optimizer

**Token Guard (`token-guard`)** is a cross-agent plugin and `SKILL.md` bundle built for **Antigravity, Claude Code, Cursor, OpenAI Codex, and Gemini CLI**. Instead of optimizing only assistant wording (like `caveman`) or only shell stdout (like `RTK`), it enforces savings across all 5 layers of an agent session without breaking your KV prompt prefix cache.

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

## 5-Layer Architecture

1. **Cache-Safe Skill (`skills/token-guard/SKILL.md`, `<350` tokens)**: Structural terseness (no preamble, recaps, code-echoing, or pidgin), parallel tool batching, symbol-first navigation, script-first bulk ops, and subagent tiering. Zero dynamic per-turn `PreInvocation` injections so KV prefix caches remain 100% hot.
2. **Universal Host Hook Adapter (`scripts/hook_entry.py`)**: Zero-dependency Python 3 stdlib entrypoint that auto-detects Antigravity, Claude Code, Cursor, or Gemini CLI payloads on stdin and emits native schema responses (fail-open).
3. **Deterministic Read Guard (`scripts/read_guard.py`)**:
   - Blocks duplicate re-reads (and covered subranges) of unchanged files keyed by `(conversationId, path, range, mtime_ns, size)`.
   - Blocks unbounded reads of lockfiles, minified/bundle/map assets, vendor/build folders (`node_modules`, `.next`, `dist`, etc.), and large files (`>500` lines / `>40 KB`).
4. **Noisy Command Filter (`scripts/cmd_filter.py`)**: Rewrites verbose test/build/lint/install/git commands safely via `rtk` (if present) or `--run-b64`, stripping ANSI/progress/passing boilerplate while preserving errors, exact exit codes, and spilling full unfiltered logs to temp files.
5. **Transcript Auditor (`scripts/audit_transcript.py`)**: Parses Antigravity and Claude Code `.jsonl` transcripts (plus live `metrics.jsonl` hook telemetry) to quantify turns, duplicate reads, token bombs blocked, bytes stripped, and net tokens saved:

```bash
python3 scripts/audit_transcript.py path/to/transcript.jsonl --json
```

## Run Unit Tests

```bash
python3 -m unittest discover -s tests -v
```
