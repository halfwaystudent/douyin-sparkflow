"""Exercise the browser send loop against an isolated on-disk ledger."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
from unittest.mock import AsyncMock, patch

import pytest

from core import automatic_recovery, page_readiness, recovery_queue, streak_state, tasks
from tests.test_automatic_recovery import NOW, recovery_store
from tests.test_browser_stage_timeouts import flow
from utils import config as storage


RECEIPT = {"ok": True, "protoParsed": True, "protoOk": True, "httpStatus": 200,
           "call": "message_send", "serverMessageId": "offline-receipt"}
REFUSAL_RECEIPT = {"ok": False, "call": "message_send", "httpStatus": 200,
                   "protoParsed": True, "protoOk": False, "protoStatusCode": 0,
                   "protoSendStatus": 3, "protoCheckCode": 2,
                   "protoCheckCodes": {"status_code": 8610, "raw_check_code": 2},
                   "rejection": {"key": "status_code", "value": 8610, "source": "raw"}}


@contextmanager
def sender(*, receipt=RECEIPT, response_seen=True):
    now = NOW.replace(hour=16)
    with flow() as (user, _page, _context, _browser, chat, _release, run):
        user.update(account_ref="offline-account", login_generation="offline-login",
                    cookies=[{"name": "sessionid", "value": "offline", "expires": -1}],
                    targets=["first", "second"])
        user[automatic_recovery.TICKET] = {"day": now.date().isoformat(),
                                         "runId": "offline-timeout", "targets": user["targets"]}
        summary = {"enabled": True, "send_request_seen": True,
                   "send_response_seen": response_seen, "send_receipt": receipt}
        with recovery_store([user], now) as (path, clock), \
             patch.object(tasks, "_scheduled_send_time", return_value=now - timedelta(minutes=1)), \
             patch.object(page_readiness, "ensure_chat_page_ready", AsyncMock()), \
             patch.object(tasks, "start_im_send_observer", AsyncMock(return_value=AsyncMock(return_value=summary))), \
             patch.object(tasks, "detect_message_already_sent", AsyncMock(return_value=(False, "no bubble"))):
            elapsed = timedelta()

            def advance(delta):
                nonlocal elapsed
                elapsed += delta

            clock.now.side_effect = lambda tz: (now + elapsed).astimezone(tz)
            yield user, path, advance, now, chat, run


def _load(path):
    return json.loads(path.read_text())[0]


def _reject_outcome_write(bucket):
    original_update = tasks.update_user_data

    def update(mutate, **kwargs):
        def reject(accounts):
            result, changed = mutate(accounts)
            if "first" in (accounts[0].get(bucket) or {}):
                return None, False
            return result, changed

        return original_update(reject, **kwargs)

    return patch.object(tasks, "update_user_data", side_effect=update)


def test_success_is_committed_before_second_enter():
    with sender() as (_user, path, _clock, now, chat, run):
        async def press(key):
            if key == "Enter" and chat.press.await_count == 2:
                assert tasks._target_sent_today(_load(path), "first", now)

        chat.press.side_effect = press
        asyncio.run(run())
        assert chat.press.await_count == 2
        assert tasks._target_sent_today(_load(path), "second", now)


@pytest.mark.parametrize("lost_commit", ["write_error", "rejected_transition"])
def test_failed_success_commit_stops_before_second_enter(lost_commit):
    with sender() as (_user, path, _clock, now, chat, run):
        if lost_commit == "write_error":
            original_save = storage._save_json_file

            def save(target, data, **kwargs):
                if target == path and "first" in (data[0].get("message_history") or {}):
                    raise storage.StorageError("synthetic write failure")
                return original_save(target, data, **kwargs)

            with patch.object(storage, "_save_json_file", side_effect=save):
                asyncio.run(run())
        else:
            with _reject_outcome_write("message_history"):
                asyncio.run(run())

        saved = _load(path)
        assert chat.press.await_count == 1
        assert "first" not in (saved.get("message_history") or {})
        assert streak_state.is_sent_unverified(saved, "first", now)
        assert not streak_state.attempt_in_progress(saved, "second", now)


def test_failed_recovered_outcome_commit_stops_before_second_enter():
    with sender(receipt={}, response_seen=False) as (_user, path, _advance, now, chat, run), \
         patch.object(tasks, "detect_message_already_sent", AsyncMock(return_value=(True, "new bubble"))), \
         _reject_outcome_write("message_history"):
        asyncio.run(run())
        saved = _load(path)
        assert chat.press.await_count == 1
        assert not streak_state.history_entry(saved, "first", now.tzinfo)
        assert streak_state.is_sent_unverified(saved, "first", now)


def test_failed_refusal_commit_stops_before_second_enter():
    with sender(receipt=REFUSAL_RECEIPT) as (_user, path, _advance, now, chat, run), \
         _reject_outcome_write("failure_queue"):
        asyncio.run(run())
        saved = _load(path)
        assert chat.press.await_count == 1
        assert not streak_state.failure_entry(saved, "first", now.tzinfo)
        assert streak_state.is_sent_unverified(saved, "first", now)


def test_missing_response_stops_before_second_enter():
    with sender(receipt={}, response_seen=False) as (_user, path, _clock, now, chat, run):
        asyncio.run(run())
        saved = _load(path)
        assert chat.press.await_count == 1
        assert streak_state.is_sent_unverified(saved, "first", now)
        assert not streak_state.failure_entry(saved, "first", now.tzinfo)


def test_cancelled_after_enter_cannot_be_reclaimed_after_lease_expiry():
    with sender() as (user, path, advance, now, chat, run):
        async def interrupted(_key):
            raise asyncio.CancelledError()

        chat.press.side_effect = interrupted
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
        assert chat.press.await_count == 1
        later = now + timedelta(minutes=30)
        advance(later - now)
        saved = _load(path)
        assert streak_state.is_sent_unverified(saved, "first", later)
        assert not streak_state.history_entry(saved, "first", later.tzinfo)
        user[automatic_recovery.TICKET] = {"day": later.date().isoformat(),
                                          "runId": "after-restart", "targets": user["targets"]}
        assert not automatic_recovery.claim(user, "first", "after-restart")
        assert automatic_recovery.claim(user, "second", "after-restart")


def test_explicit_refusal_is_committed_and_can_retry_when_due():
    with sender(receipt=REFUSAL_RECEIPT) as (_user, path, advance, now, chat, run), \
         patch.object(recovery_queue.random, "randint", return_value=45):
        async def press(_key):
            advance(timedelta(seconds=1))

        chat.press.side_effect = press
        asyncio.run(run())
        saved = _load(path)
        assert chat.press.await_count == 2
        assert streak_state.failure_entry(saved, "first", now.tzinfo)["serverReceipt"] == REFUSAL_RECEIPT
        assert not recovery_queue.due(saved, "first", now + timedelta(minutes=44))
        assert recovery_queue.due(saved, "first", now + timedelta(minutes=46))
