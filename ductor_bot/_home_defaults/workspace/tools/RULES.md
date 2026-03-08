# Tools Directory

This is the navigation index for workspace tools.

## Global Rules

- Prefer these tool scripts over manual JSON/file surgery.
- Run with `python3`.
- Normal successful runs are JSON-oriented; tutorial/help output may be plain text.
- Open the matching subfolder `CLAUDE.md` before non-trivial changes.

## Routing

- recurring tasks / schedules -> `cron_tools/CLAUDE.md`
- incoming HTTP triggers -> `webhook_tools/CLAUDE.md`
- Telegram file/media processing -> `telegram_tools/CLAUDE.md`
- sub-agent management (create/remove/list/ask) -> `agent_tools/CLAUDE.md`
- background tasks (delegate, list, cancel) -> `task_tools/CLAUDE/GEMINI/AGENTS.md`
- custom user scripts -> `user_tools/CLAUDE.md`

## Bot Restart

To restart the bot (e.g. after config changes or recovery):

```bash
touch ~/.ductor/restart-requested
```

The bot picks up this marker within seconds and restarts cleanly.
No tool script needed — just create the file.

## Output and Memory

- Save user deliverables in `../output_to_user/`.
- Update `../memory_system/MAINMEMORY.md` silently for durable user facts/preferences.
