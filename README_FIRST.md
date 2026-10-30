# Gloss starter kit for Claude Code — read me first

This folder is a ready-made project setup. When you open it in Claude Code, it automatically
loads:

- **`CLAUDE.md`**: the project rules Claude reads every session
- **`docs/SPEC.md`**: what to build, the architecture and the acceptance criteria
- **`.claude/agents/`**: your 9-agent team
- **`.claude/settings.json` + `.claude/hooks/`**: the safeguards (automatic blocks on risky edits)
- **`docs/glossary/zh_gaming.yaml`**: the Chinese slang glossary (板板, 单飞了, 粥 …) used for translations
- **`KICKOFF_PROMPT.md`**: the prompt you paste to start

## Steps

1. **Unzip into a new empty folder**, e.g. `C:\Projects\Gloss`. Make sure the hidden-looking
   `.claude` folder comes along, because it holds the agent team and the safeguards. You don't
   need to clone the repo first: Phase 0 connects this folder to
   https://github.com/killmete/Gloss and pushes to it.
2. **Add screenshots.** Put 10–30 real screenshots into `tests/golden/images/`: chat rooms,
   order cards, menus, profiles. These are what the agents test against. Leave out any with
   bank details.
3. **Open the folder in the Claude Code desktop app** and start a new session in it.
4. **Paste the prompt.** Open `KICKOFF_PROMPT.md`, copy everything below the line, paste it and
   send.
5. Claude works in phases and **stops at each ⛳ checkpoint** for your "go". Phase 0 checks your
   PC and tells you exactly what to install (Python 3.12, uv) if anything is missing.
6. **API key (needed in Phase 3):** create one at console.anthropic.com. API usage is billed
   separately from a Claude subscription. The app stores it in Windows Credential Manager, never
   in a file.

## The agent team

```
                         You
                          │  "go" at each checkpoint
                    ┌─────▼──────┐
                    │Orchestrator│  (main Claude Code session)
                    └─────┬──────┘
      ┌──────────┬────────┼─────────┬──────────────┬───────────┐
 researcher  architect  capture-   ocr-       translation-   ui-
 (evidence)  (contracts) engineer  engineer    engineer      engineer
                          │          │             │            │
                          └──── test-engineer (tests first) ────┘
                                       │
                          safeguard (PASS / BLOCK gate)
                                       │
                    update-steward (keeps updates from breaking things)
```

## The safeguards, in plain English

| What | How it's protected |
|---|---|
| Module "contracts" (how parts talk to each other) | Frozen after you approve them. Claude can't edit them unless you allow it |
| Expected test answers | Frozen after you confirm them, so Claude can't "cheat" a failing test |
| Hooks, settings, agent definitions | Claude can never edit them |
| Your API key / `.env` files | Claude can't read them |
| Dependencies | Pinned. Updated one at a time by `update-steward`, with before/after benchmarks |
| Every phase | Must get a PASS from the `safeguard` agent |

**If Claude asks you to unlock something**, it will say which file. In PowerShell, from the
project folder:

```powershell
New-Item .claude\CONTRACT_UNLOCK      # allow the change
Remove-Item .claude\CONTRACT_UNLOCK   # lock again when done
```

(The same goes for `GOLDEN_UNLOCK`: test answers, and `TEAM_UNLOCK`: agent definitions.)

## Optional: experimental agent teams
By default the agents run as **subagents**, which is stable and cheaper. Claude Code also has
an experimental "agent teams" mode, where agents message each other directly. It's useful for
the Phase 1 research debate, but it uses many more tokens. You don't need it. To try it, add
`"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}` to your personal
`%USERPROFILE%\.claude\settings.json`. Claude can't change this for you, because project settings
are protected.
