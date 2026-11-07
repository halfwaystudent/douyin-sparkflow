"""Synthetic 8101 refusals use the existing once-per-day final fallback."""
import asyncio
from copy import deepcopy
from datetime import timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from core import im_receipt, refusal_retry, streak_state, tasks
from tests import test_refusal_retry as retry_tests
from tests.test_refusal_retry import account, NOW, WINDOW
from tests import test_im_receipt as receipt_fixtures


def refused_8101():
    user = account()
    parsed = im_receipt.parse_send_response(receipt_fixtures.REFUSED_8101)
    user['failure_queue']['friend']['serverReceipt'] = {
        'call': 'message_send', 'httpStatus': 200, 'ok': False,
        'protoParsed': True, 'protoOk': False,
        'protoStatusCode': parsed['statusCode'], 'protoSendStatus': parsed['sendStatus'],
        'protoCheckCode': parsed['checkCode'], 'protoCheckCodes': parsed['checkCodes'],
        'protoRefusalUnderstood': parsed.get('refusalUnderstood', False),
        'rejection': {'key': 'status_code', 'value': 8101, 'source': 'raw'},
    }
    return user


class Refusal8101FallbackTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {
            'SPARKFLOW_MANUAL_RUN': '', 'SPARKFLOW_FALLBACK_PHASE': '',
            'SPARKFLOW_8610_RETRY_ONLY': '', 'SPARKFLOW_TIMEZONE': 'Asia/Shanghai',
        })
        env.start()
        self.addCleanup(env.stop)

    def test_exact_8101_refusal_is_due_only_at_final_fallback(self):
        user = refused_8101()
        self.assertEqual(tasks._select_due_targets(user, WINDOW, NOW)[0], [])
        with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1', 'SPARKFLOW_MANUAL_RUN': '1'}):
            self.assertEqual(tasks._select_due_targets(user, WINDOW, NOW.replace(hour=17))[0], ['friend'])

    def test_recorded_binary_8101_observer_to_disk_planner_and_claim(self):
        from utils.config import update_user_data
        receipt = asyncio.run(receipt_fixtures.ObserverReceiptTests()._observe(receipt_fixtures.REFUSED_8101))
        self.assertEqual((receipt['protoSendStatus'], receipt['protoCheckCode'],
                          receipt['protoCheckCodes']['raw_check_code']), (0, 0, 1))
        user = {'unique_id': 'demo', 'username': 'demo', 'targets': ['friend'], 'enabled': True}
        rejected_at = NOW - timedelta(minutes=31)
        end = NOW.replace(hour=17)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.json'
            path.write_text(json.dumps([user]))
            with patch.object(tasks, 'update_user_data', side_effect=lambda fn, **kw: update_user_data(fn, path=path)), \
                    patch.object(tasks, 'get_userData', side_effect=lambda **kw: json.loads(path.read_text())), \
                    patch.object(tasks, 'datetime') as clock, \
                    patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1'}):
                clock.now.return_value = end
                tasks._persist_browser_send_failure(user, 'friend', 'synthetic text', 'send_rejected',
                    'synthetic refusal', rejected_at.astimezone(timezone.utc).isoformat(timespec='seconds'), server_receipt=receipt)
                reloaded = json.loads(path.read_text())[0]
                self.assertEqual(tasks._select_due_targets(reloaded, WINDOW, end)[0], ['friend'])
                self.assertEqual(tasks._browser_prelaunch_targets(reloaded, ['friend'], False), ['friend'])
                self.assertTrue(tasks._mark_browser_target_in_flight(reloaded, 'friend', run_id='recorded-8101'))
                self.assertTrue(refusal_retry.used_today(json.loads(path.read_text())[0], 'friend', end))

    def test_raw_code_inside_malformed_or_unknown_payload_does_not_authorize_retry(self):
        f = receipt_fixtures
        texts = [
            '{"status_code":8101,"raw_check_code":1',
            '{"status_code":8101,"raw_check_code":1,"status_code":0}',
            '{"status_code":8101,"raw_check_code":1,"nested":{"check_code":"0"}}',
        ]
        raws = [f.response(body=f.send_body(check_message=text)) for text in texts]
        valid_text = json.dumps({'status_code': 8101, 'raw_check_code': 1})
        raws.append(f.response(body=f.send_body(check_message=valid_text) + f.vfield(99, 0)))
        for raw in raws:
            with self.subTest(raw=raw):
                receipt = asyncio.run(f.ObserverReceiptTests()._observe(raw))
                self.assertFalse(receipt['protoRefusalUnderstood'])
                user = refused_8101()
                user['failure_queue']['friend']['serverReceipt'] = receipt
                self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))

    def test_accepted_binary_response_never_becomes_retryable(self):
        receipt = asyncio.run(receipt_fixtures.ObserverReceiptTests()._observe(receipt_fixtures.ACCEPTED))
        self.assertTrue(receipt['ok'])
        self.assertFalse(receipt['protoRefusalUnderstood'])
        user = refused_8101()
        user['failure_queue']['friend']['serverReceipt'] = receipt
        self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))

    def test_protocol_enabled_configuration_still_routes_8101_to_browser_guard(self):
        user = refused_8101()
        user['targets'].append('new-friend')
        with patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = NOW.replace(hour=17)
            protocol, browser = tasks._split_sender_modes({'useProtocolSender': True}, [user])
        self.assertEqual([row['targets'] for row in browser], [['friend']])
        self.assertEqual([row['targets'] for row in protocol], [['new-friend']])

    def test_actual_scan_prioritizes_normal_work_after_shuffle_across_tabs(self):
        user = refused_8101()
        group = '群聊:7691234567890123401'
        user['targets'] = [group, 'friend', 'new-friend']
        rejected_at = NOW - timedelta(minutes=31)
        streak_state.mark_failed(user, group, category='send_rejected', reason='synthetic', retryable=False, now=rejected_at)
        user['failure_queue'][group] = deepcopy(user['failure_queue']['friend'])
        seen = []

        async def scan(_page, _user, _name, targets, *_args, **_kwargs):
            seen.append(list(targets))
            for target in targets:
                yield target

        async def run():
            return [t async for t in tasks._scan_delivery_targets(None, user, 'synthetic', user['targets'], {})]

        with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1'}), \
                patch.object(tasks, 'datetime') as clock, \
                patch.object(tasks, 'scroll_and_select_user', side_effect=scan):
            clock.now.return_value = NOW.replace(hour=17)
            self.assertEqual(asyncio.run(run()), ['new-friend', group, 'friend'])
        self.assertEqual(seen, [['new-friend'], [group, 'friend']])


class Refusal8101SafetyTests(retry_tests.RefusalRetryTests):
    """Run all existing quota/readback/restart safety contracts for 8101 too."""
    def setUp(self):
        super().setUp()
        fixture = patch.object(retry_tests, 'account', side_effect=refused_8101)
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_both_codes_share_quota_even_after_new_refusal_overwrites_outcome(self):
        user = refused_8101()
        refusal_retry.reserve(user, 'friend', NOW, 'reserved-8101')
        later = NOW + timedelta(minutes=1)
        streak_state.mark_failed(user, 'friend', category='send_rejected', reason='synthetic', retryable=False, now=later)
        entry = user['failure_queue']['friend']
        entry['lastAttemptAt'] = later.isoformat(timespec='seconds')
        entry['serverReceipt'] = deepcopy(account()['failure_queue']['friend']['serverReceipt'])
        self.assertTrue(refusal_retry.explicit_8610(user, 'friend', later))
        self.assertFalse(refusal_retry.eligible(user, 'friend', later + timedelta(minutes=30)))

    def test_legacy_8610_predicate_stays_code_specific(self):
        self.assertFalse(refusal_retry.explicit_8610(refused_8101(), 'friend', NOW))
        self.assertTrue(refusal_retry.explicit_8610(account(), 'friend', NOW))

    def test_self_visible_unknown_and_contradictory_receipts_stay_blocked(self):
        for change in [
            {'protoSendStatus': 4}, {'protoSendStatus': 3},
            {'protoCheckCode': True}, {'protoCheckCodes': {'status_code': '8101', 'raw_check_code': 2}},
            {'rejection': {'key': 'status_code', 'value': 8610}},
            {'ok': True}, {'protoParsed': False},
        ]:
            with self.subTest(change=change):
                user = refused_8101()
                user['failure_queue']['friend']['serverReceipt'].update(change)
                self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))

    def test_malformed_and_other_receipts_stay_terminal(self):
        variants = [
            ('protoParsed', False), ('protoOk', True), ('ok', True),
            ('protoStatusCode', '0'), ('protoSendStatus', 3), ('jsonOk', True),
            ('protoCheckCodes', {'status_code': '8101', 'raw_check_code': 1}),
            ('protoCheckCodes', {'status_code': 8101, 'raw_check_code': 1, 'other': 1}),
            ('protoRefusalUnderstood', False), ('protoRefusalUnderstood', None),
            ('httpStatus', 500), ('httpStatus', '200'),
            ('rejection', {'key': 'status_code', 'value': '8101'}),
            ('rejection', {'key': 'status_code', 'value': 9999}),
        ]
        for key, value in variants:
            with self.subTest(key=key, value=value):
                user = refused_8101()
                user['failure_queue']['friend']['serverReceipt'][key] = value
                self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))
        user = refused_8101()
        user['failure_queue']['friend'].pop('serverReceipt')
        self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))

    def test_stale_failure_queue_does_not_reopen_an_unknown_send(self):
        user = refused_8101()
        streak_state.mark_sent_unverified(user, 'friend', now=NOW, detail='pre_enter_outcome_unknown_barrier')
        with patch.dict(os.environ, {'SPARKFLOW_FALLBACK_PHASE': '1'}):
            self.assertEqual(tasks._select_due_targets(user, WINDOW, NOW.replace(hour=17))[0], [])
        self.assertFalse(refusal_retry.eligible(user, 'friend', NOW))

    def test_regular_prelaunch_and_durable_claim_deny_8101_without_opening_quota(self):
        user = refused_8101()
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data') as write:
            clock.now.return_value = NOW
            self.assertEqual(tasks._browser_prelaunch_targets(user, ['friend'], False), [])
            self.assertFalse(tasks._mark_browser_target_in_flight(user, 'friend', run_id='ordinary-run'))
        write.assert_not_called()

    def test_valid_previous_day_8101_is_normal_new_day_not_a_retry(self):
        user = refused_8101()
        tomorrow = NOW + timedelta(days=1)
        with patch.object(tasks, 'datetime') as clock:
            clock.now.return_value = tomorrow
            self.assertEqual(tasks._select_due_targets(user, WINDOW, tomorrow)[0], ['friend'])
            self.assertEqual(tasks._browser_prelaunch_targets(user, ['friend'], False), ['friend'])
        self.assertFalse(refusal_retry.used_today(user, 'friend', tomorrow))
