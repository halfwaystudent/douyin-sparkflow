import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from core import tasks, streak_state

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=TZ)
WINDOW = {'enabled': True, 'startHour': 10, 'endHour': 17, 'scheduleIntervalMinutes': 20}

def account():
    a = {'unique_id': 'demo', 'username': 'demo', 'targets': ['friend'], 'enabled': True}
    t = NOW - timedelta(minutes=31)
    streak_state.mark_failed(a, 'friend', category='send_rejected', reason='8610', retryable=False, now=t)
    a['failure_queue'] = {'friend': {'category': 'send_rejected', 'lastAttemptAt': t.isoformat(timespec='seconds'), 'serverReceipt': {
        'call': 'message_send', 'ok': False, 'protoParsed': True, 'protoOk': False,
        'protoStatusCode': 0, 'protoSendStatus': 3, 'protoCheckCode': 2,
        'protoCheckCodes': {'status_code': 8610, 'raw_check_code': 2},
        'rejection': {'key': 'status_code', 'value': 8610, 'source': 'raw'},
    }}}
    return a

class RefusalRetryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'SPARKFLOW_MANUAL_RUN': '', 'SPARKFLOW_FALLBACK_PHASE': '', 'SPARKFLOW_TIMEZONE': 'Asia/Shanghai'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_cooled_8610_is_due_only_at_final_fallback(self):
        self.assertEqual(tasks._select_due_targets(account(), WINDOW, NOW)[0], [])
        with patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'}):
            self.assertEqual(tasks._select_due_targets(account(), WINDOW, NOW.replace(hour=17))[0], ['friend'])

    @patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'})
    def test_dispatch_reserves_retry_and_second_dispatch_is_denied(self):
        from core import refusal_retry
        a = account(); disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk]); return result
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data', side_effect=update), patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]):
            clock.now.return_value = NOW
            self.assertTrue(tasks._mark_browser_target_in_flight(a, 'friend', run_id='retry-one'))
            self.assertTrue(refusal_retry.used_today(disk, 'friend', NOW))
            streak_state.mark_failed(disk, 'friend', category='timeout', reason='timeout', retryable=True, now=NOW+timedelta(seconds=1))
            self.assertFalse(tasks._mark_browser_target_in_flight(deepcopy(disk), 'friend', run_id='retry-two'))

    @patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'})
    def test_dispatch_before_cooldown_is_denied(self):
        a = account()
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data') as update:
            clock.now.return_value = NOW - timedelta(minutes=12)
            self.assertFalse(tasks._mark_browser_target_in_flight(a, 'friend', run_id='too-early'))
            update.assert_not_called()

    def test_malformed_and_other_receipts_stay_terminal(self):
        from core import refusal_retry
        variants = [
            ('protoParsed', False), ('protoOk', True), ('ok', True),
            ('protoStatusCode', '0'), ('protoSendStatus', 0), ('jsonOk', True),
            ('protoCheckCodes', {'status_code': '8610', 'raw_check_code': 2}),
            ('protoCheckCodes', {'status_code': 8610, 'raw_check_code': 2, 'other': 1}),
            ('rejection', {'key': 'status_code', 'value': '8610'}),
            ('rejection', {'key': 'status_code', 'value': 9999}),
        ]
        for key, value in variants:
            with self.subTest(key=key, value=value):
                a = account(); a['failure_queue']['friend']['serverReceipt'][key] = value
                self.assertFalse(refusal_retry.eligible(a, 'friend', NOW))
                self.assertEqual(tasks._select_due_targets(a, WINDOW, NOW)[0], [])
        a = account(); a['failure_queue']['friend'].pop('serverReceipt')
        self.assertFalse(refusal_retry.eligible(a, 'friend', NOW))

    def test_exact_cooldown_boundary(self):
        from core import refusal_retry
        self.assertFalse(refusal_retry.eligible(account(), 'friend', NOW - timedelta(minutes=11, seconds=1)))
        self.assertTrue(refusal_retry.eligible(account(), 'friend', NOW - timedelta(minutes=11)))

    def test_only_current_day_rejections_qualify(self):
        from core import refusal_retry
        self.assertFalse(refusal_retry.eligible(account(), 'friend', NOW+timedelta(days=1)))

    def test_reserved_then_timeout_cannot_reenter_planner(self):
        from core import refusal_retry
        import json
        a = account(); refusal_retry.reserve(a, 'friend', NOW, 'reserved')
        a = json.loads(json.dumps(a))  # restart with only persisted data
        streak_state.mark_failed(a, 'friend', category='timeout', reason='timeout', retryable=True, now=NOW)
        self.assertEqual(tasks._select_due_targets(a, WINDOW, NOW+timedelta(minutes=40))[0], [])

    def test_reserved_quota_is_per_schedule_day(self):
        from core import refusal_retry
        a = account(); refusal_retry.reserve(a, 'friend', NOW, 'reserved')
        self.assertTrue(refusal_retry.used_today(a, 'friend', NOW.astimezone(timezone.utc)))
        self.assertFalse(refusal_retry.used_today(a, 'friend', NOW+timedelta(days=1)))

    def test_confirmed_target_never_retries(self):
        from core import refusal_retry
        a = account(); streak_state.mark_send_confirmed(a, 'friend', now=NOW)
        self.assertFalse(refusal_retry.eligible(a, 'friend', NOW))
        self.assertEqual(tasks._select_due_targets(a, WINDOW, NOW)[0], [])

    def test_retired_extra_retry_pass_does_not_send(self):
        a = account(); a['targets'].append('normal-pending')
        with patch.dict('os.environ', {'SPARKFLOW_8610_RETRY_ONLY': '1'}):
            self.assertEqual(tasks._select_due_targets(a, WINDOW, NOW.replace(hour=18))[0], [])

    @patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'})
    def test_missing_readback_blocks_dispatch_and_keeps_quota(self):
        from core import refusal_retry
        a = account(); disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk]); return result
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data', side_effect=update), patch.object(tasks, 'get_userData', return_value=[]):
            clock.now.return_value = NOW
            self.assertFalse(tasks._mark_browser_target_in_flight(a, 'friend', run_id='lost-readback'))
        self.assertTrue(refusal_retry.used_today(disk, 'friend', NOW))

    def test_manual_retry_consumes_automatic_quota(self):
        from core import refusal_retry
        a = account(); disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk]); return result
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data', side_effect=update), patch.object(tasks, 'get_userData', side_effect=lambda **kw: [deepcopy(disk)]):
            clock.now.return_value = NOW - timedelta(minutes=20)
            self.assertTrue(tasks._mark_browser_target_in_flight(a, 'friend', run_id='manual', allow_retry=True))
        self.assertTrue(refusal_retry.used_today(disk, 'friend', NOW))

    def test_alias_cannot_bypass_reserved_quota(self):
        from core import refusal_retry
        a = account()
        a['target_uid_bindings'] = {'friend': {'uid': '123456'}, 'renamed': {'uid': '123456'}}
        refusal_retry.reserve(a, 'friend', NOW, 'reserved')
        self.assertTrue(refusal_retry.used_today(a, 'renamed', NOW))

    def test_refusal_retry_uses_configured_fallback_not_extra_hour(self):
        from webui import ops
        for end_hour in [17, 18, 23, 24]:
            with self.subTest(end_hour=end_hour), patch.object(ops, 'running_in_container', return_value=True):
                text = ops.replace_douyin_cron_schedule('', f'10:00-{end_hour}:00/20m')
                self.assertNotIn('SPARKFLOW_8610_RETRY_ONLY=1', text)
                fallback = [line for line in text.splitlines() if 'SPARKFLOW_FALLBACK_PHASE=1' in line]
                self.assertEqual(len(fallback), 1)
                self.assertTrue(fallback[0].startswith('59 23 ' if end_hour == 24 else f'0 {end_hour} '))

    def test_schedule_replacement_removes_old_extra_pass_and_preserves_unrelated(self):
        from webui import ops
        with patch.object(ops, 'running_in_container', return_value=True):
            original = ops.replace_douyin_cron_schedule('5 2 * * * /opt/demo-backup.sh\n', '10:00-18:00/20m')
            old = "0 19 * * * env SPARKFLOW_8610_RETRY_ONLY=1 bash /app/scripts/run_scheduled_task.sh >> /app/logs/douyin-sparkflow.log 2>&1\n"
            updated = ops.replace_douyin_cron_schedule(original+old, '10:00-17:00/20m')
        self.assertNotIn('SPARKFLOW_8610_RETRY_ONLY=1', updated)
        self.assertIn('5 2 * * * /opt/demo-backup.sh', updated)
        fallback = [l for l in updated.splitlines() if 'SPARKFLOW_FALLBACK_PHASE=1' in l]
        self.assertEqual(len(fallback), 1)
        self.assertTrue(fallback[0].startswith('0 17 '))

    def test_end_of_window_includes_only_cooled_unused_refusals(self):
        end = NOW.replace(hour=17)
        with patch.dict('os.environ', {'SPARKFLOW_MANUAL_RUN': '1', 'SPARKFLOW_FALLBACK_PHASE': '1'}):
            self.assertEqual(tasks._select_due_targets(account(), WINDOW, end)[0], ['friend'])
            late = account()
            late_time = end - timedelta(minutes=10)
            streak_state.mark_failed(late, 'friend', category='send_rejected', reason='8610', retryable=False, now=late_time)
            late['failure_queue']['friend']['lastAttemptAt'] = late_time.isoformat(timespec='seconds')
            self.assertEqual(tasks._select_due_targets(late, WINDOW, end)[0], [])
            from core import refusal_retry
            reserved = account()
            refusal_retry.reserve(reserved, 'friend', NOW, 'already-used')
            self.assertEqual(tasks._select_due_targets(reserved, WINDOW, end)[0], [])

    def test_late_pass_with_disabled_window_cannot_send_general_targets(self):
        a = account(); a['targets'].append('normal-pending')
        with patch.dict('os.environ', {'SPARKFLOW_8610_RETRY_ONLY': '1', 'SPARKFLOW_MANUAL_RUN': '1'}), patch.object(tasks, 'datetime') as clock, patch.object(streak_state, 'preflight_account', return_value={'healthy': True}):
            clock.now.return_value = NOW.replace(hour=18)
            result = tasks._prepare_active_users_for_run({'dailySendWindow': {'enabled': False}}, [a])
        self.assertEqual([u['targets'] for u in result], [])

    @patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'})
    def test_overwritten_checkpoint_blocks_dispatch(self):
        a = account(); disk = deepcopy(a)
        def update(fn, **kw):
            result, changed = fn([disk]); return result
        def read(**kw):
            copy = deepcopy(disk)
            streak_state.mark_failed(copy, 'friend', category='timeout', reason='other event', retryable=True, now=NOW+timedelta(seconds=1))
            return [copy]
        with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data', side_effect=update), patch.object(tasks, 'get_userData', side_effect=read):
            clock.now.return_value = NOW
            self.assertFalse(tasks._mark_browser_target_in_flight(a, 'friend', run_id='overwritten'))

    def test_identity_upgrade_cannot_reset_quota(self):
        from core import refusal_retry
        a = account()
        a['target_uid_bindings'] = {'friend': {'uid': '123456'}}
        refusal_retry.reserve(a, 'friend', NOW, 'reserved')
        a['target_uid_bindings']['friend']['secUid'] = 'demo-sec-uid'
        self.assertTrue(refusal_retry.used_today(a, 'friend', NOW))

    @patch.dict('os.environ', {'SPARKFLOW_FALLBACK_PHASE': '1'})
    def test_real_file_reservation_survives_reload(self):
        from core import refusal_retry
        from utils.config import update_user_data
        from pathlib import Path
        import tempfile, json
        a = account()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'accounts.json'
            path.write_text(json.dumps([a]))
            with patch.object(tasks, 'datetime') as clock, patch.object(tasks, 'update_user_data', side_effect=lambda fn, **kw: update_user_data(fn, path=path)), patch.object(tasks, 'get_userData', side_effect=lambda **kw: json.loads(path.read_text())):
                clock.now.return_value = NOW
                self.assertTrue(tasks._mark_browser_target_in_flight(a, 'friend', run_id='file-retry'))
            reloaded = json.loads(path.read_text())[0]
            self.assertTrue(refusal_retry.used_today(reloaded, 'friend', NOW))
            self.assertEqual(tasks._select_due_targets(reloaded, WINDOW, NOW+timedelta(minutes=40))[0], [])

    def test_legacy_reservation_without_timezone_remains_protective(self):
        from core import refusal_retry
        from zoneinfo import ZoneInfo
        user = account()
        at = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
        user[refusal_retry.KEY] = {"friend": {"day": "2026-10-30", "at": at.isoformat(), "runId": "old"}}

        self.assertTrue(refusal_retry.used_today(user, "friend", at, schedule_timezone=ZoneInfo("Asia/Tokyo")))

    def test_personal_timezone_keeps_same_day_8610_eligible_across_global_midnight(self):
        from core import refusal_retry
        from zoneinfo import ZoneInfo
        attempt = datetime(2026, 11, 3, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        fallback = datetime(2026, 11, 3, 23, 59, tzinfo=ZoneInfo("Asia/Tokyo"))
        user = account()
        receipt = deepcopy(user["failure_queue"]["friend"]["serverReceipt"])
        streak_state.mark_failed(user, "friend", category="send_rejected", reason="8610", retryable=False, now=attempt)
        user["failure_queue"] = {"friend": {"category": "send_rejected", "lastAttemptAt": attempt.isoformat(timespec="seconds"), "serverReceipt": receipt}}

        self.assertTrue(refusal_retry.eligible(user, "friend", fallback, schedule_timezone=ZoneInfo("Asia/Tokyo")))

    def test_timezone_change_cannot_bypass_existing_reservation(self):
        from core import refusal_retry
        from zoneinfo import ZoneInfo
        tokyo = ZoneInfo("Asia/Tokyo")
        shanghai = ZoneInfo("Asia/Shanghai")
        user = account()
        reserved_at = datetime(2026, 11, 3, 12, 0, tzinfo=tokyo)
        refusal_retry.reserve(user, "friend", reserved_at, "reserved", schedule_timezone=tokyo)

        # Tokyo has already rolled into Nov 4, but the reservation instant and
        # current Shanghai policy are both still Nov 3.  Editing the policy
        # must not create a second retry in that current policy day.
        self.assertTrue(refusal_retry.used_today(
            user, "friend", datetime(2026, 11, 3, 23, 30, tzinfo=shanghai), schedule_timezone=shanghai,
        ))
        self.assertTrue(refusal_retry.used_today(
            user, "friend", datetime(2026, 11, 3, 23, 30, tzinfo=shanghai), schedule_timezone="Asia/Shanghai",
        ))

    def test_timezone_reservation_is_fail_closed_for_bad_or_future_provenance(self):
        from core import refusal_retry
        from zoneinfo import ZoneInfo
        user = account()
        now = datetime(2026, 11, 3, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        base = {
            "day": "2026-11-03", "timezone": "Asia/Tokyo",
            "at": datetime(2026, 11, 3, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")).isoformat(),
            "runId": "reserved", "identityRefs": ["friend"],
        }
        for field, value in (
            ("day", "not-a-date"), ("timezone", "not/a-zone"),
            ("at", "not-a-time"),
            ("at", datetime(2026, 11, 5, tzinfo=timezone.utc).isoformat()),
        ):
            with self.subTest(field=field, value=value):
                row = dict(base)
                row[field] = value
                user[refusal_retry.KEY] = {"friend": row}
                self.assertTrue(refusal_retry.used_today(user, "friend", now, schedule_timezone=now.tzinfo))





