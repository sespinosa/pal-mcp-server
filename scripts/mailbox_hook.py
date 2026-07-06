#!/usr/bin/env python3
"""Agent lifecycle hook that delivers pending PAL mailbox messages.

Wire this script into a coding agent's turn-end hook so that messages sent via the
PAL ``mailbox`` tool are injected into the agent's context as soon as it finishes a
turn, with no human relay needed. Currently supported:

* ``claude``: Claude Code ``Stop`` hook. Emits ``hookSpecificOutput.additionalContext``
  so the session continues with the delivered messages in context.

The script is inert unless the ``PAL_AGENT_ID`` environment variable is set, so it is
safe to register globally: interactive human sessions (which don't set the variable)
are unaffected. Identity and mailbox location come from ``PAL_AGENT_ID`` and
``PAL_MAILBOX_DIR`` (see ``utils/mailbox.py``). Messages are only claimed after the
delivery payload has been written out, so a failure re-delivers instead of losing mail.

Usage (registered automatically by the ``mailbox`` tool's ``setup`` action):
    python3 /path/to/pal-mcp-server/scripts/mailbox_hook.py claude
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

_MAILBOX_MODULE_PATH = Path(__file__).resolve().parent.parent / "utils" / "mailbox.py"


def _load_mailbox_module():
    # Load by file path (not package import) so this script needs nothing beyond the
    # standard library, regardless of what the wider utils package imports.
    spec = importlib.util.spec_from_file_location("_pal_mailbox", _MAILBOX_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves the defining module through sys.modules at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    # Consume hook input first so the calling agent never blocks on a full pipe.
    try:
        sys.stdin.read()
    except OSError:
        pass

    agent_kind = sys.argv[1] if len(sys.argv) > 1 else "claude"
    if agent_kind != "claude":
        print(f"mailbox_hook: unsupported agent kind '{agent_kind}'", file=sys.stderr)
        return 1

    agent_id = os.environ.get("PAL_AGENT_ID")
    if not agent_id:
        return 0

    try:
        mailbox = _load_mailbox_module()
        entries = mailbox.peek_messages(agent_id)
        if not entries:
            return 0

        messages = [message for _, message in entries]
        context = (
            f"You have {len(messages)} new message(s) in your PAL mailbox. "
            "Address them before stopping, and use the PAL mailbox tool to reply if a response is expected. "
            "IMPORTANT: if you are running headless, only your final message is returned to the caller; "
            "after handling the mail, restate your complete deliverable for the original task in your final "
            "response so this notice does not displace it.\n\n" + mailbox.format_messages(messages)
        )
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context}},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.flush()

        # Claim only after the payload is out: crash-safety over exactly-once.
        mailbox.claim_messages([path for path, _ in entries])
    except Exception:  # never break the host agent's turn
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
