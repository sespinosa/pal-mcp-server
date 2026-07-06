#!/usr/bin/env python3
"""
Mailbox Tool Validation Test

Validates the inter-agent mailbox tool end to end:
- Checking an empty mailbox
- Sending a message to another agent id
- Receiving (and consuming) messages as the recipient
- Error handling for invalid requests
- Installing the Claude Code delivery hook (setup action, idempotent)
"""

import json
import os
import tempfile

from .conversation_base_test import ConversationBaseTest


class MailboxValidationTest(ConversationBaseTest):
    """Test mailbox tool functionality via in-process MCP tool calls"""

    @property
    def test_name(self) -> str:
        return "mailbox_validation"

    @property
    def test_description(self) -> str:
        return "Inter-agent mailbox send/check/setup validation"

    def run_test(self) -> bool:
        """Run mailbox validation scenarios against an isolated spool directory"""
        try:
            self.setUp()
            self.logger.info("Test: mailbox tool validation")

            saved_env = {
                key: os.environ.get(key) for key in ("PAL_MAILBOX_DIR", "PAL_AGENT_ID", "PAL_CLAUDE_SETTINGS_PATH")
            }
            with tempfile.TemporaryDirectory(prefix="pal-mailbox-sim-") as workdir:
                os.environ["PAL_MAILBOX_DIR"] = os.path.join(workdir, "mailbox")
                os.environ["PAL_CLAUDE_SETTINGS_PATH"] = os.path.join(workdir, "settings.json")
                os.environ.pop("PAL_AGENT_ID", None)
                try:
                    if not self._test_check_empty():
                        return False
                    if not self._test_send_and_receive():
                        return False
                    if not self._test_send_requires_recipient():
                        return False
                    if not self._test_setup_is_idempotent(os.environ["PAL_CLAUDE_SETTINGS_PATH"]):
                        return False
                finally:
                    for key, value in saved_env.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value

            self.logger.info("  ✅ All mailbox validation scenarios passed")
            return True
        except Exception as e:
            self.logger.error(f"Mailbox validation test failed: {e}")
            return False

    def _call(self, params: dict):
        response, _ = self.call_mcp_tool_direct("mailbox", params)
        return json.loads(response) if response else None

    def _test_check_empty(self) -> bool:
        self.logger.info("  1: Checking empty mailbox")
        result = self._call({"action": "check"})
        if not result or result["status"] != "success" or result["metadata"]["message_count"] != 0:
            self.logger.error(f"Expected empty mailbox, got: {result}")
            return False
        return True

    def _test_send_and_receive(self) -> bool:
        self.logger.info("  2: Send to a worker id, then receive as that worker")
        sent = self._call({"action": "send", "to": "sim-worker", "message": "simulator says hello"})
        if not sent or sent["status"] != "success" or sent["metadata"]["to"] != "sim-worker":
            self.logger.error(f"Send failed: {sent}")
            return False

        os.environ["PAL_AGENT_ID"] = "sim-worker"
        try:
            received = self._call({"action": "check"})
            if not received or received["metadata"]["message_count"] != 1:
                self.logger.error(f"Expected 1 message for sim-worker, got: {received}")
                return False
            if "simulator says hello" not in received["content"]:
                self.logger.error(f"Message content missing: {received['content']}")
                return False

            # Messages are consumed on read.
            recheck = self._call({"action": "check"})
            if not recheck or recheck["metadata"]["message_count"] != 0:
                self.logger.error(f"Expected consumed mailbox, got: {recheck}")
                return False
        finally:
            os.environ.pop("PAL_AGENT_ID", None)
        return True

    def _test_send_requires_recipient(self) -> bool:
        self.logger.info("  3: Send without recipient is rejected")
        try:
            result = self._call({"action": "send", "message": "no recipient"})
        except Exception:
            return True
        if result and result.get("status") == "error":
            return True
        self.logger.error(f"Expected an error, got: {result}")
        return False

    def _test_setup_is_idempotent(self, settings_path: str) -> bool:
        self.logger.info("  4: Setup installs the Claude Code Stop hook idempotently")
        first = self._call({"action": "setup", "agent": "claude"})
        if not first or first["metadata"]["changed"] is not True:
            self.logger.error(f"First setup should install the hook: {first}")
            return False

        with open(settings_path, encoding="utf-8") as handle:
            settings = json.load(handle)
        commands = [hook["command"] for entry in settings["hooks"]["Stop"] for hook in entry["hooks"]]
        if not any("mailbox_hook.py" in command for command in commands):
            self.logger.error(f"Hook command missing from settings: {commands}")
            return False

        second = self._call({"action": "setup"})
        if not second or second["metadata"]["changed"] is not False:
            self.logger.error(f"Second setup should be a no-op: {second}")
            return False
        return True


def main():
    import sys

    test = MailboxValidationTest(verbose=True)
    success = test.run_test()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
