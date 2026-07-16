# Clink Tool - CLI-to-CLI Bridge

**Spawn AI subagents, connect external CLIs, orchestrate isolated contexts – all without leaving your session**

The `clink` tool transforms your CLI into a multi-agent orchestrator. Launch isolated Codex instances from _within_ Codex, delegate to Gemini's 1M context, or run specialized Claude agents—all while preserving conversation continuity. Instead of context-switching or token bloat, spawn fresh subagents that handle complex tasks in isolation and return only the results you need.

> **CAUTION**: Clink launches real CLI agents with relaxed permission flags (Gemini ships with `--yolo`, Codex with `--dangerously-bypass-approvals-and-sandbox`, Claude with `--permission-mode acceptEdits`) so they can edit files and run tools autonomously via MCP. If that’s more access than you want, remove those flags—the CLI can still open/read files and report findings, it just won’t auto-apply edits. You can also tighten role prompts or system prompts with stop-words/guardrails, or disable clink entirely. Otherwise, keep the shipped presets confined to workspaces you fully trust.

## Why Use Clink (CLI + Link)?

### Codex-within-Codex: The Ultimate Context Management

**The Problem**: You're deep in a Codex session debugging authentication. Now you need a comprehensive security audit, but that'll consume 50K tokens of context you can't spare.

**The Solution**: Spawn a fresh Codex subagent in an isolated context:
```bash
clink with codex codereviewer to audit auth/ for OWASP Top 10 vulnerabilities
```

The subagent:
- Launches in a **pristine context** with full token budget
- Performs deep analysis using its own MCP tools and web search
- Returns **only the final security report** (not intermediate steps)
- Your main session stays **laser-focused** on debugging

**Works with any supported CLI**: Codex can spawn Codex / Claude Code / Gemini CLI subagents, or mix and match between different CLIs.

---

### Cross-CLI Orchestration

**Scenario 1**: You're in Codex and need Gemini's 1M context window to analyze a massive legacy codebase.

**Without clink**: Open new terminal → run `gemini` → lose conversation context → manually copy/paste findings → context mismatch hell.

**With clink**: `"clink with gemini to map dependencies across this 500-file monorepo"` – Gemini processes, returns insights, conversation flows seamlessly.

**Scenario 2**: Use [`consensus`](consensus.md) to debate features with multiple models, then hand off to Gemini for implementation.

```
"Use consensus with pro and gpt5 to decide whether to add dark mode or offline support next"
[consensus runs, models deliberate, recommendation emerges]

Use continuation with clink - implement the recommended feature
```

Gemini receives the full conversation context from `consensus` including the consensus prompt + replies, understands the chosen feature, technical constraints discussed, and can start implementation immediately. No re-explaining, no context loss - true conversation continuity across tools and models.

## Key Features

- **Stay in one CLI**: No switching between terminal sessions or losing context
- **Full conversation continuity**: Gemini's responses participate in the same conversation thread
- **Role-based prompts**: Pre-configured roles for planning, code review, or general questions
- **Full CLI capabilities**: Gemini can use its own web search, file tools, and latest features
- **Token efficiency**: File references (not full content) to conserve tokens
- **Cross-tool collaboration**: Combine with other PAL tools like `planner` → `clink` → `codereview`
- **Free tier available**: Gemini offers 1,000 requests/day free with a personal Google account - great for cost savings across tools

## Available Roles

**Default Role** - General questions, summaries, quick answers
```
Use clink to ask gemini about the latest React 19 features
```

**Planner Role** - Strategic planning with multi-phase approach
```
clink with gemini with planner role to map out our microservices migration strategy
```

**Code Reviewer Role** - Focused code analysis with severity levels
```
Use clink codereviewer role to review auth.py for security issues
```

You can make your own custom roles in `conf/cli_clients/` or tweak any of the shipped presets.

## Tool Parameters

- `prompt`: Your question or task for the external CLI (required)
- `cli_name`: Which CLI to use - `gemini` (default), `claude`, `codex`, or add your own in `conf/cli_clients/`
- `role`: Preset role - `default`, `planner`, `codereviewer` (default: `default`)
- `files`: Optional file paths for context (references only, CLI opens files itself)
- `images`: Optional image paths for visual context
- `continuation_id`: Continue previous clink conversations

## Usage Examples

**Architecture Planning:**
```
Use clink with gemini planner to design a 3-phase rollout plan for our feature flags system
```

**Code Review with Context:**
```
clink to gemini codereviewer: Review payment_service.py for race conditions and concurrency issues
```

**Codex Code Review:**
```
"clink with codex cli and perform a full code review using the codereview role"
```

**Quick Research Question:**
```
"Ask gemini via clink: What are the breaking changes in TypeScript 5.5?"
```

**Multi-Tool Workflow:**
```
"Use planner to outline the refactor, then clink gemini planner for validation,
then codereview to verify the implementation"
```

**Leveraging Gemini's Web Search:**
```
"Clink gemini to research current best practices for Kubernetes autoscaling in 2025"
```

## How Clink Works

1. **Your request** - You ask your current CLI to use `clink` with a specific CLI and role
2. **Background execution** - PAL spawns the configured CLI (e.g., `gemini --output-format json`)
3. **Context forwarding** - Your prompt, files (as references), and conversation history are sent as part of the prompt
4. **CLI processing** - Gemini (or other CLI) uses its own tools: web search, file access, thinking modes
5. **Seamless return** - Results flow back into your conversation with full context preserved
6. **Continuation support** - Future tools and models can reference Gemini's findings via [continuation support](../context-revival.md) within PAL.

## Rich Results (Artifacts)

Clink responses are MCP content blocks, not just text. Two mechanisms attach non-text content:

- **Artifact tags**: when the spawned CLI references a file it produced as
  `<ARTIFACT>/absolute/path</ARTIFACT>` (the default role prompt advertises this), clink validates
  the path with the same security rules as user-supplied files and attaches it — small images and
  audio are inlined (base64), everything else becomes a resource link the client can fetch. At most
  4 artifacts per response.
- **Full output on truncation**: when output exceeds the response cap and no `<SUMMARY>` is present,
  the complete output is saved to a temp file and attached as a resource link alongside the excerpt
  (path also in `metadata.output_full_file`), so nothing is lost.

## Best Practices

- **Pre-authenticate CLIs**: Install and configure Gemini CLI first (`npm install -g @google/gemini-cli`)
- **Choose appropriate roles**: Use `planner` for strategy, `codereviewer` for code, `default` for general questions
- **Leverage CLI strengths**: Gemini's 1M context for large codebases, web search for current docs
- **Combine with PAL tools**: Chain `clink` with `planner`, `codereview`, `debug` for powerful workflows
- **File efficiency**: Pass file paths, let the CLI decide what to read (saves tokens)

## Configuration

Clink configurations live in `conf/cli_clients/`. We ship presets for the supported CLIs:

- `gemini.json` – runs `gemini --telemetry false --yolo -o json`
- `claude.json` – runs `claude --print --output-format json --permission-mode acceptEdits --model sonnet`
- `codex.json` – runs `codex exec --json --dangerously-bypass-approvals-and-sandbox`

> **CAUTION**: These flags intentionally bypass each CLI's safety prompts so they can edit files or launch tools autonomously via MCP. Only enable them in trusted sandboxes and tailor role prompts or CLI configs if you need more guardrails.

Each preset points to role-specific prompts in `systemprompts/clink/`. Duplicate those files to add more roles or adjust CLI flags.

> **Why `--yolo` for Gemini?** The Gemini CLI currently requires automatic approvals to execute its own tools (for example `run_shell_command`). Without the flag it errors with `Tool "run_shell_command" not found in registry`. See [issue #5382](https://github.com/google-gemini/gemini-cli/issues/5382) for more details.

**Adding new CLIs**: Drop a JSON config into `conf/cli_clients/`, create role prompts in `systemprompts/clink/`, and register a parser/agent if the CLI outputs a new format. Custom CLIs that aren't built in must set `"parser"` explicitly (use `"text"` for plain-text output).

## Asynchronous (Dispatch/Poll) Backends

Some coding agents don't answer inline — they dispatch work to a remote or background service
(cloud coding agents such as Codex Cloud or Google Jules, CI-driven agents, task queues) and expose
`submit` / `status` / `result` style subcommands. Clink can drive these through the same interface
as local CLIs by adding a `dispatch` block to the client config. A generic example (map the args
and patterns onto your CLI's actual subcommands and output):

```json
{
  "name": "mycloud",
  "command": "mycloud-cli",
  "parser": "text",
  "additional_args": ["submit", "--task", "{prompt}"],
  "timeout_seconds": 3600,
  "dispatch": {
    "handle_pattern": "task_id:\\s*([A-Za-z0-9-]+)",
    "poll_args": ["status", "{handle}"],
    "poll_interval_seconds": 30,
    "done_pattern": "state:\\s*(completed|done)",
    "failed_pattern": "state:\\s*(failed|cancelled)",
    "collect_args": ["result", "{handle}"]
  }
}
```

How it runs:

1. **Dispatch** – the regular configured command submits the task. The prompt is passed on stdin,
   or substituted into arguments when any of them contains `{prompt}` (for CLIs that can't read
   stdin). The task handle is extracted from the output with `handle_pattern` (first capture group).
2. **Poll** (optional) – `poll_args` runs against the bare executable every `poll_interval_seconds`
   (default 15) with `{handle}` substituted, until `done_pattern` matches, `failed_pattern` matches
   (error), or the client's `timeout_seconds` budget runs out. Errors and timeouts always include
   the task handle so a still-running remote task stays trackable. Anchor `handle_pattern` to the
   exact id format — extracted handles are validated against a conservative charset before being
   substituted into commands. A non-zero exit from a poll command aborts the run (no retry state
   in v1).
3. **Collect** (optional) – `collect_args` fetches the final result; without it the last poll output
   is used. The result is parsed with the configured parser and returned like any other clink
   response, with `dispatch_handle` and `poll_attempts` in the metadata.

Omit `poll_args` entirely for **fire-and-forget** services that expose no status API and deliver
results out of band (e.g. a cloud agent that opens a pull request): clink then returns the dispatch
acknowledgement — typically containing the task id and session URL — immediately.

### Detached mode (`wait: false`)

By default clink blocks until the remote task finishes. For pollable dispatch backends you can
instead detach: pass `wait: false` and the call returns right after the dispatch phase with a
`job_id` (plus the raw task handle) in the metadata. The remote service keeps running on its own —
nothing in the session is blocked, and the job record survives server restarts.

Resolve the job later with the companion **`jobs` tool** (registered automatically when any
configured client has a pollable `dispatch` block):

- `jobs {action: "status", job_id}` — runs exactly **one** poll cycle and reports
  `running` / `done` / `failed`. You own the polling cadence; no server-side loop.
- `jobs {action: "collect", job_id}` — fetches, parses, and returns the final result (identical
  post-processing to a blocking clink call: summary/truncation limits, artifact attachment,
  continuation recording). Collect is idempotent — repeat calls return the stored result.
- `jobs {action: "list"}` — all known jobs with state and age.

Job records are one JSON file each under `~/.pal/jobs/` and are swept after 48 hours. They store
only the handle and routing metadata (never client configuration or secrets); the CLI client is
re-resolved from the registry on every poll, and jobs dispatched in one session can be collected
from another.

> **Note**: A spawned cloud task usually runs on the account the backing CLI is authenticated with
> and may consume paid quota. Clink never dispatches on its own — calls still go through your MCP
> client's regular tool approval.

## When to Use Clink vs Other Tools

- **Use `clink`** for: Leveraging external CLI capabilities (Gemini's web search, 1M context), specialized CLI features, cross-CLI collaboration
- **Use `chat`** for: Direct model-to-model conversations within PAL
- **Use `planner`** for: PAL's native planning workflows with step validation
- **Use `codereview`** for: PAL's structured code review with severity levels

## Setup Requirements

Ensure the relevant CLI is installed and configured:

- [Claude Code](https://www.anthropic.com/claude-code)
- [Gemini CLI](https://github.com/google-gemini/gemini-cli)
- [Codex CLI](https://docs.sourcegraph.com/codex)

## Related Guides

- [Chat Tool](chat.md) - Direct model conversations
- [Planner Tool](planner.md) - PAL's native planning workflows
- [CodeReview Tool](codereview.md) - Structured code reviews
- [Context Revival](../context-revival.md) - Continuing conversations across tools
- [Advanced Usage](../advanced-usage.md) - Complex multi-tool workflows
