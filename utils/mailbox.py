"""File-based mailbox for inter-agent messaging.

Each agent (an interactive CLI session or a CLI spawned via clink) has a spool
directory under the mailbox root, named after its agent id. A message is a single
JSON file written atomically (temp file + ``os.replace``) into the recipient's spool,
so concurrent senders never need locks. Reading a message claims it by deleting the
file, giving at-most-once delivery without any coordination primitive, the same
model as a classic maildir.

The mailbox root defaults to ``~/.pal/mailbox`` and can be overridden with the
``PAL_MAILBOX_DIR`` environment variable. Agent identity comes from ``PAL_AGENT_ID``,
typically injected per CLI client via the ``env`` block in ``conf/cli_clients``.

This module is intentionally dependency-free so headless hook scripts can import it
without the server's requirements installed.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

PAL_MAILBOX_DIR_ENV = "PAL_MAILBOX_DIR"
PAL_AGENT_ID_ENV = "PAL_AGENT_ID"
DEFAULT_AGENT_ID = "parent"

_AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Windows reserved device names cannot be used as directory names.
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}

# Temp files older than this are considered orphaned by a crashed sender.
_STALE_TMP_SECONDS = 24 * 3600


class MailboxError(ValueError):
    """Raised for invalid mailbox operations (bad agent ids, empty messages)."""


@dataclass
class Message:
    """A single mailbox message."""

    sender: str
    recipient: str
    text: str
    created_at: str


def mailbox_root() -> Path:
    """Return the mailbox root directory (not necessarily existing yet)."""
    override = os.environ.get(PAL_MAILBOX_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pal" / "mailbox"


def current_agent_id() -> str:
    """Return this process's agent id (PAL_AGENT_ID, or 'parent' when unset)."""
    return os.environ.get(PAL_AGENT_ID_ENV) or DEFAULT_AGENT_ID


def validate_agent_id(agent_id: str) -> str:
    """Validate an agent id so it is safe to use as a directory name."""
    candidate = (agent_id or "").strip()
    if (
        not _AGENT_ID_PATTERN.match(candidate)
        or candidate.endswith(".")
        or candidate.split(".")[0].lower() in _WINDOWS_RESERVED
    ):
        raise MailboxError(
            f"Invalid agent id {agent_id!r}: use 1-64 characters (letters, digits, '.', '_', '-'), "
            "starting with a letter or digit and not ending with '.' or matching a reserved device name"
        )
    return candidate


def send_message(*, sender: str, recipient: str, text: str) -> Message:
    """Write a message into the recipient's spool atomically."""
    sender = validate_agent_id(sender)
    recipient = validate_agent_id(recipient)
    if not text or not text.strip():
        raise MailboxError("Message text must not be empty")

    message = Message(
        sender=sender,
        recipient=recipient,
        text=text,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )

    spool = mailbox_root() / recipient
    spool.mkdir(parents=True, exist_ok=True)

    filename = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    fd, tmp_path = tempfile.mkstemp(prefix=".sending-", dir=spool)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(message), handle, ensure_ascii=False)
        os.replace(tmp_path, spool / filename)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return message


def peek_messages(agent_id: str) -> list[tuple[Path, Message]]:
    """Read pending messages without claiming them, oldest first.

    Malformed message files are deleted on sight so they cannot poison every future
    read, and temp files orphaned by crashed senders are swept. Use ``claim_messages``
    on the returned paths once the messages have actually been handed over; that
    ordering means a crash between read and hand-over re-delivers instead of losing
    mail (at-least-once). ``drain_messages`` gives the inverse (at-most-once) tradeoff.
    """
    agent_id = validate_agent_id(agent_id)
    spool = mailbox_root() / agent_id

    entries: list[tuple[Path, Message]] = []
    try:
        paths = sorted(spool.glob("*.json"))
        stale_tmp = [p for p in spool.glob(".sending-*") if p.stat().st_mtime < time.time() - _STALE_TMP_SECONDS]
    except OSError:
        return []

    for path in stale_tmp:
        try:
            path.unlink()
        except OSError:
            pass

    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Malformed: remove so it never poisons future reads.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        except OSError:
            # Claimed by a concurrent reader (or unreadable); skip.
            continue
        entries.append(
            (
                path,
                Message(
                    sender=str(raw.get("sender", "unknown")),
                    recipient=str(raw.get("recipient", agent_id)),
                    text=str(raw.get("text", "")),
                    created_at=str(raw.get("created_at", "")),
                ),
            )
        )
    return entries


def claim_messages(paths: list[Path]) -> None:
    """Delete handled message files; already-claimed (missing) files are fine."""
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def drain_messages(agent_id: str) -> list[Message]:
    """Read and immediately claim all pending messages for an agent, oldest first.

    At-most-once: a message is only returned if this reader won the deleting race.
    """
    claimed: list[Message] = []
    for path, message in peek_messages(agent_id):
        try:
            path.unlink()
        except OSError:
            continue
        claimed.append(message)
    return claimed


def _roster_dir() -> Path:
    # Agent ids cannot start with '.', so this never collides with a spool directory.
    return mailbox_root() / ".roster"


def register_agent(agent_id: str, *, parent_id: str, client: str, role: str) -> None:
    """Record a live spawned agent in the roster (atomic write)."""
    agent_id = validate_agent_id(agent_id)
    entry = {
        "agent_id": agent_id,
        "parent_id": parent_id,
        "client": client,
        "role": role,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started_ts": time.time(),
    }
    roster = _roster_dir()
    roster.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".sending-", dir=roster)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entry, handle, ensure_ascii=False)
        os.replace(tmp_path, roster / f"{agent_id}.json")
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def unregister_agent(agent_id: str) -> None:
    """Remove an agent from the roster; already gone is fine."""
    try:
        (_roster_dir() / f"{validate_agent_id(agent_id)}.json").unlink()
    except (OSError, MailboxError):
        pass


def _read_roster() -> list[dict]:
    """Return live roster entries, dropping unreadable and stale ones."""
    roster = _roster_dir()
    entries: list[dict] = []
    try:
        paths = sorted(roster.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Entries left behind by a crashed spawner expire instead of lingering.
        if time.time() - float(entry.get("started_ts", 0)) > _STALE_TMP_SECONDS:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        entries.append(entry)
    return entries


def uniquify_agent_id(base_id: str) -> str:
    """Return base_id, or base_id-2/-3/... if a live agent already uses it."""
    base_id = validate_agent_id(base_id)
    taken = {entry.get("agent_id") for entry in _read_roster()}
    if base_id not in taken:
        return base_id
    suffix = 2
    while f"{base_id}-{suffix}" in taken:
        suffix += 1
    return f"{base_id}-{suffix}"


def list_stack(agent_id: str) -> dict:
    """Return this agent's slice of the roster: itself, its parent, its live workers."""
    agent_id = validate_agent_id(agent_id)
    entries = _read_roster()
    own = next((entry for entry in entries if entry.get("agent_id") == agent_id), None)
    return {
        "self": own,
        "parent_id": own.get("parent_id") if own else None,
        "workers": [entry for entry in entries if entry.get("parent_id") == agent_id],
    }


def format_messages(messages: list[Message]) -> str:
    """Render messages as a readable block for injection into an agent's context."""
    blocks = []
    for message in messages:
        blocks.append(f"[{message.created_at}] from {message.sender}:\n{message.text}")
    return "\n\n".join(blocks)
