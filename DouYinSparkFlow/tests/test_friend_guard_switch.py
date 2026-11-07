"""Release gate: the identity guard is off unless explicitly enabled."""
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core import friend_chat_guard
from core import tasks


class FriendGuardSwitchTests(unittest.TestCase):
    def test_default_is_off(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPARKFLOW_FRIEND_GUARD", None)
            self.assertFalse(friend_chat_guard.guard_enabled())

    def test_on_values(self):
        for value in ["on", "ON", "1", "true", "enforce", "yes"]:
            with self.subTest(value=value), patch.dict(os.environ, {"SPARKFLOW_FRIEND_GUARD": value}):
                self.assertTrue(friend_chat_guard.guard_enabled())

    def test_off_values(self):
        for value in ["", "off", "0", "false", "no", "anything-else"]:
            with self.subTest(value=value), patch.dict(os.environ, {"SPARKFLOW_FRIEND_GUARD": value}):
                self.assertFalse(friend_chat_guard.guard_enabled())

    def test_require_friend_chat_is_a_no_op_when_off(self):
        page = Mock()
        with patch.dict(os.environ, {"SPARKFLOW_FRIEND_GUARD": "off"}), \
             patch.object(friend_chat_guard, "verify_friend", AsyncMock(side_effect=AssertionError("must not verify"))):
            import asyncio
            asyncio.run(tasks._require_friend_chat(page, {"targets": ["x"]}, "x", arm=True, wait=True))

    def test_require_friend_chat_verifies_when_on(self):
        page = Mock()
        verified = AsyncMock(return_value=(True, ""))
        with patch.dict(os.environ, {"SPARKFLOW_FRIEND_GUARD": "on"}), \
             patch.object(tasks, "_find_matching_account", return_value={"targets": ["x"]}), \
             patch.object(friend_chat_guard, "verify_friend", verified):
            import asyncio
            asyncio.run(tasks._require_friend_chat(page, {"targets": ["x"]}, "x", arm=True, wait=True))
        self.assertTrue(verified.await_count == 1)


if __name__ == "__main__":
    unittest.main()
