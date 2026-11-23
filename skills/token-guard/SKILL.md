---
name: token-guard
description: >-
  Token and context efficiency rules: parallel tool batching, symbol-first navigation,
  deduped narrow reads, concise output, script-first bulk ops, and subagent tiering.
---

# Token Guard

1. **Terse structure**: Skip preambles, post-edit recaps, and verbatim echoes of code or logs already in context. Use compact lists or file links; never use pidgin.
2. **Cache-safe**: Never inject dynamic per-turn `PreInvocation` text; preserve prefix byte identity.
3. **Batch calls**: Run independent searches, reads, and edits in one parallel turn.
4. **Symbol-first reads**: Locate lines via `rg` or `code-index`, then read `<150`-line slices (`StartLine`/`EndLine`). Block unbounded reads of lockfiles, bundles, build dirs, or `>500`-line files.
5. **No re-reads**: Trust file slices already read in this session unless edited.
6. **Script bulk work**: Replace `>5` similar searches/edits with one short script.
7. **Tier subagents**: Delegate `>5`-file exploration to `flash`/`flash_lite`; run 1–2 targeted lookups directly.

Hooks enforce read dedup and stdout filtering cleanly. Load [Benchmarks](./references/benchmarks.md) or run `python3 scripts/audit_transcript.py <transcript.jsonl>` on demand.
