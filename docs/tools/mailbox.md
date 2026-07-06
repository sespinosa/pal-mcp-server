# Mailbox Tool - Inter-Agent Messaging

**Let agent sessions talk to each other — peer-to-peer, not just hub-and-spoke**

With [`clink`](clink.md), a parent session can spawn other CLI agents and collect their results. What
spawned agents *cannot* do is talk to each other (or back to the parent) while running. The `mailbox`
tool closes that gap with a shared, file-based mailbox that every session using PAL can reach: any
agent can queue a message for any other by id, and messages are delivered either on demand or
automatically at turn boundaries via lifecycle hooks.

## How It Works

- Every agent session has an **agent id**, taken from the `PAL_AGENT_ID` environment variable
  (default: `parent`). Give clink-spawned CLIs an address by adding an `env` block to their
  `conf/cli_clients` config, e.g. `"env": {"PAL_AGENT_ID": "claude-worker"}`.
- Workers discover the mailbox automatically: when a clink client has `PAL_AGENT_ID` configured,
  clink appends a `=== PAL MAILBOX ===` briefing to the spawned prompt — the worker's own id, how
  to check and send mail, and that messages from `parent` are authoritative follow-ups. Clients
  without the env key get exactly the same prompt as before.
- Messages are single JSON files in a spool directory (`~/.pal/mailbox/<agent-id>/`, override with
  `PAL_MAILBOX_DIR`). Writes are atomic and reads claim the file, so concurrent sessions need no
  locking and each message is delivered at most once.
- Because the mailbox is on disk, it works across separate MCP server processes — every session
  (parent or spawned) sees the same mailbox even though each runs its own PAL instance.

## Tool Parameters

- `action`: What to do - `send`, `check`, `list`, or `setup` (required)
- `to`: Recipient agent id (required for `send`)
- `message`: Message text (required for `send`)
- `agent`: Which coding agent CLI to install delivery hooks for (for `setup`, default: `claude`)

## Usage Examples

**`send`** — queue a message for another agent:
```
Use mailbox to send "the API schema changed, regenerate your client" to claude-worker
```

**`check`** — read (and consume) your own pending messages:
```
Check my mailbox
```

**`list`** — see the live agents in your own stack (your workers, your parent):
```
List my agents
```

**`setup`** — install automatic turn-end delivery for a coding agent CLI (see below).

## Automatic Delivery

Polling with `check` works everywhere, but the point of a mailbox is delivery without a human
relaying messages. Delivery hooks degrade gracefully by agent capability:

| Tier | Agent | Mechanism |
|------|-------|-----------|
| Inject-in-place | Claude Code | `Stop` hook adds pending messages as `additionalContext`, so the session continues working with the new information — no human involvement |
| On-demand | Everything else | Instruct the agent (in its task prompt or role prompt) to run `mailbox` `check` at natural boundaries |

Run the `setup` action once to install the Claude Code hook:

```
Use mailbox setup for claude
```

This registers `scripts/mailbox_hook.py` as a `Stop` hook in your Claude Code settings
(`~/.claude/settings.json`, honoring `CLAUDE_CONFIG_DIR`; override with
`PAL_CLAUDE_SETTINGS_PATH`). The change is additive and idempotent, and the response reports
exactly what was written and how to undo it. The installed command no-ops safely if the PAL
installation is later moved or removed; to uninstall, remove the hook entry from `settings.json`.

**The hook is inert for humans**: it only activates in sessions where `PAL_AGENT_ID` is set, which
normally means clink-spawned workers. Your own interactive sessions are untouched unless you export
`PAL_AGENT_ID` yourself (which is also how you make your interactive session addressable).

## Example: Coordinated Workers

1. Parent session spawns two workers via clink (configs give them `PAL_AGENT_ID` of
   `claude-frontend` / `claude-backend`) and runs `mailbox` `setup` once.
2. The backend worker changes an API contract and sends: *"POST /users now returns 201 with a
   Location header"* to `claude-frontend`.
3. When the frontend worker finishes its current turn, the Stop hook injects the message and it
   keeps working with the updated contract — no round-trip through the parent.
4. Workers send results or questions to `parent`; the parent sees them on its next `check` (or its
   own Stop hook, if you gave the parent session an id).

> **Tip**: clink's automatic `=== PAL MAILBOX ===` briefing already tells spawned workers that
> parent messages are authoritative follow-ups. If you spawn agents through anything other than
> clink, include that framing yourself — models are (rightly) wary of instructions arriving through
> side channels, and a session that expects parent mail treats it as part of the job.

## Notes & Limits

- Message text and agent ids are validated (ids: 1-64 chars, letters/digits/`._-`); the spool
  directory never leaves the mailbox root.
- The mailbox is a local, same-machine, same-user trust domain — it is not authenticated messaging.
- Delivered messages are consumed on read. If a session dies mid-task, unread messages simply wait
  for the next session with the same agent id.
- Restart running Claude Code sessions after `setup` so the new hook is picked up.

## Orchestration Conventions

When the mailbox tool is enabled, PAL adds a short coordination briefing to the server's MCP
instructions so orchestrating models design parallel work around messaging. The conventions it
establishes:

- Every clink spawn with a mailbox identity gets a **unique instance id** (`worker`, `worker-2`, ...)
  reported back as `mailbox_agent_id` in the clink response — address workers by that exact id.
- clink maintains a **stack-scoped roster**: `list` shows only your own live workers and your
  parent, not every agent on the machine. Entries are registered at spawn and removed on return.
- Message when it **changes another agent's work** (blockers, interface changes, shared-file
  conflicts, results a sibling waits on); don't message final results (the return value carries
  them) or routine progress.
- When spawning workers that must coordinate, tell each one in its task prompt which sibling ids
  matter and what to message them about.

## When to Use Mailbox vs Other Tools

- **Use `mailbox`** for: Coordinating live agent sessions — follow-up instructions to a running
  worker, worker-to-worker updates, results reported to the parent mid-run
- **Use `clink`** for: Spawning an agent and collecting its final result (the mailbox reaches it
  *while* it runs)
- **Use `continuation_id`** for: Carrying conversation context across PAL tools and models within
  your own session
- **Use `chat`** for: Direct model-to-model conversations within PAL

## Related Guides

- [Clink Tool](clink.md) - Spawning the agent sessions that the mailbox connects
- [Context Revival](../context-revival.md) - Sharing conversation context across PAL tools
- [Advanced Usage](../advanced-usage.md) - Model configuration and conversation threading
