# Jobs Tool - Asynchronous Clink Job Tracking

Companion to [`clink`](clink.md)'s detached mode. When a pollable dispatch backend is invoked with
`wait: false`, clink returns a `job_id` immediately; this tool polls and collects that job in later
calls, so long-running remote tasks (cloud coding agents, task queues) never block a session.

The tool only appears when at least one configured CLI client has a pollable `dispatch`
configuration — see [Asynchronous (Dispatch/Poll) Backends](clink.md#asynchronous-dispatchpoll-backends).

## Actions

| Action | Parameters | Behavior |
|--------|------------|----------|
| `list` | — | All known jobs with state, CLI, handle, and age. |
| `status` | `job_id` | Runs **one** poll cycle and reports `running` / `done` / `failed`. Terminal states answer from the stored record without polling. |
| `collect` | `job_id` | Fetches the final result once done (polls once first if still running). Identical post-processing to a blocking clink call: output limits, `<SUMMARY>` compression, artifact attachment, continuation recording. Idempotent — repeated collects return the stored result. |

## Usage Examples

```
clink with cli_name=mycloud, wait=false: "Refactor the auth module and open a PR"
→ metadata: {"job_id": "3f2a9c1b8d04", "state": "running", ...}

jobs status job_id=3f2a9c1b8d04     → "Job '3f2a9c1b8d04' is running (poll #1)."
jobs collect job_id=3f2a9c1b8d04    → final task output (or "Not done yet; try again later.")
```

## Lifecycle & Storage

- States: `running` → `done` | `failed` → `collected`.
- Records are one JSON file each under `~/.pal/jobs/`, written atomically, swept after 48 hours.
- Records store the task handle and routing metadata only — never client configuration or secrets.
  The CLI client is re-resolved from the registry on every access, so a job dispatched in one
  session can be collected from another (or after a server restart).
- A failing poll *command* is reported as a tool error but leaves the job `running` (transient
  infrastructure errors don't fail the remote task); a match on the client's `failed_pattern`
  marks the job `failed`.

## Related Guides

- [Clink Tool](clink.md) - dispatching the jobs this tool tracks
