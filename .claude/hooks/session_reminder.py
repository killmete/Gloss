"""SessionStart(compact) hook: re-inject the rules that must survive context compaction."""

print(
    "Gloss reminders after compaction:\n"
    "- You are the orchestrator. Delegate specialist work to the subagents in .claude/agents/.\n"
    "- Contracts in src/gloss/core/contracts/ are frozen (ADR + user unlock to change).\n"
    "- tests/golden/expected/ is protected. Never weaken or skip tests to get green.\n"
    "- Nothing external is hard-coded (models, URLs, languages, hotkeys): use config.\n"
    "- Run the safeguard agent before declaring any phase or slice done.\n"
    "- Stop at each checkpoint in KICKOFF_PROMPT.md and wait for the user's 'go'.\n"
    "- Re-read CLAUDE.md and docs/SPEC.md if unsure."
)
