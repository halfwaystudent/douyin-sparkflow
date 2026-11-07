"""Exercise the sender's actual message builder boundary, without a browser."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import msg_builder, tasks
from tests.test_browser_stage_timeouts import flow


class SenderMessageHistoryTests(unittest.TestCase):
    def test_stable_id_history_excludes_previous_message(self):
        with flow() as (user, page, context, browser, chat, release, run):
            user['targets'] = ['first']
            user['target_uid_bindings'] = {
                'first': {'uid': '10001', 'secUid': 'synthetic-sec-first'},
            }
            user['target_refs'] = {'first': 'sec:synthetic-sec-first'}
            user['message_history'] = {
                'sec:synthetic-sec-first': {
                    'message': 'previous message',
                    'sentAt': (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                    'status': 'confirmed',
                },
            }
            with patch.object(tasks, 'build_message', wraps=msg_builder.build_message) as build, \
                 patch.object(msg_builder, 'build_message_candidates',
                              return_value=['previous message', 'different message']), \
                 patch.object(msg_builder.random, 'choice', side_effect=lambda choices: choices[0]):
                asyncio.run(run())
            self.assertEqual(build.call_args.kwargs['previous_message'], 'previous message')
            chat.type.assert_awaited_once_with('different message', delay=50)
            chat.press.assert_awaited_once_with('Enter')

    def _assert_previous(self, history, expected, *, target='first', bindings=None):
        with flow() as (user, page, context, browser, chat, release, run):
            user['targets'] = [target]
            user['target_uid_bindings'] = bindings or {
                target: {'uid': '10001', 'secUid': 'synthetic-sec-first'},
            }
            user['message_history'] = history
            with patch.object(tasks, 'build_message', return_value='new message') as build:
                asyncio.run(run())
            self.assertEqual(build.call_args.kwargs['previous_message'], expected)
            chat.press.assert_awaited_once_with('Enter')

    @staticmethod
    def _entry(message, days=1):
        return {'message': message,
                'sentAt': (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
                'status': 'confirmed'}

    def test_legacy_nickname_history_is_still_read(self):
        self._assert_previous({'first': self._entry('legacy message')}, 'legacy message')

    def test_renamed_friend_retains_stable_id_history(self):
        self._assert_previous({'sec:synthetic-sec-first': self._entry('before rename')},
                              'before rename', target='renamed friend')

    def test_newer_stable_history_wins_over_old_nickname_alias(self):
        self._assert_previous({'first': self._entry('older nickname message', days=2),
                               'sec:synthetic-sec-first': self._entry('newer stable message')},
                              'newer stable message')

    def test_group_history_remains_compatible(self):
        self._assert_previous({'群聊：111': self._entry('previous group message')},
                              'previous group message', target='群聊：111')

    def test_missing_history_starts_with_empty_previous(self):
        self._assert_previous({}, '')

    def test_other_recipient_stable_history_is_not_used(self):
        self._assert_previous({'sec:other-recipient': self._entry('not this recipient')}, '')
