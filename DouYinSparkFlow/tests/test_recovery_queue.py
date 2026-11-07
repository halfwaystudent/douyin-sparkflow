import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core import recovery_queue


CONFIG = {"automaticRecovery": {"enabled": True}}
CN = ZoneInfo("Asia/Shanghai")


def cn(hour, minute=0, day=6):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc).astimezone(CN)


def local(hour, minute=0, day=6):
    return datetime(2026, 11, day, hour, minute, tzinfo=CN)


def account(uid="100", sec=None, generation="g1"):
    binding = {"uid": uid}
    if sec:
        binding["secUid"] = sec
    return {"login_generation": generation, "target_uid_bindings": {"Alice": binding}}


def test_feature_is_off_by_default_and_allowed_now_is_a_compatibility_guard():
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    assert recovery_queue.enabled({}) is False
    assert recovery_queue.allowed_now(now, config={}) is True


def test_refusal_lifecycle_waits_reserves_and_finishes():
    saved = account()
    source = cn(11)
    with (
        patch.object(recovery_queue, "get_config", return_value=CONFIG),
        patch.object(recovery_queue.random, "randint", return_value=45) as delay,
    ):
        row = recovery_queue.schedule_target(saved, "Alice", source, source, "refusal")
        assert row["state"] == "waiting"
        assert row["nextAttemptAt"] == cn(11, 45).isoformat()
        delay.assert_called_once_with(45, 90)
        assert not recovery_queue.due(saved, "Alice", cn(11, 44))
        assert recovery_queue.due(saved, "Alice", cn(11, 45))
        token = recovery_queue.reserve(saved, "Alice", cn(11, 45), "run-1")
        assert token["sequence"] == 1
        assert recovery_queue.reservation_matches(saved, "Alice", cn(11, 46), "run-1", token)
        assert recovery_queue.finish_target(saved, "Alice", cn(11, 46))["state"] == "done"
        assert not recovery_queue.due(saved, "Alice", cn(12))


def test_window_requires_aware_today_and_excludes_exact_cutoff():
    assert not recovery_queue.allowed_now(local(9, 59), config=CONFIG)
    assert recovery_queue.allowed_now(local(10), config=CONFIG)
    assert recovery_queue.allowed_now(local(23, 49), config=CONFIG)
    assert not recovery_queue.allowed_now(local(23, 50), config=CONFIG)
    assert not recovery_queue.allowed_now(datetime(2026, 11, 6, 12), config=CONFIG)
    assert not recovery_queue.allowed_now(local(12), day="2026-11-05", config=CONFIG)


def test_json_roundtrip_and_same_source_only_draws_wait_once():
    saved, source = account(), local(11)
    with (
        patch.object(recovery_queue, "get_config", return_value=CONFIG),
        patch.object(recovery_queue.random, "randint", return_value=50) as delay,
    ):
        first = recovery_queue.schedule_target(saved, "Alice", source, source, "refusal")
        loaded = json.loads(json.dumps(saved))
        again = recovery_queue.schedule_target(loaded, "Alice", local(12), source.isoformat(), "refusal")
    assert first["nextAttemptAt"] == again["nextAttemptAt"]
    delay.assert_called_once_with(45, 90)


def test_explicit_refusals_can_retry_more_than_five_times_but_claim_cannot_repeat():
    saved = account()
    with patch.object(recovery_queue, "get_config", return_value=CONFIG), patch.object(recovery_queue.random, "randint", return_value=45):
        for number in range(7):
            source = local(10 + number)
            recovery_queue.schedule_target(saved, "Alice", source, source, "refusal")
            token = recovery_queue.reserve(saved, "Alice", source + timedelta(minutes=45), f"run-{number}")
            assert token and token["sequence"] == number + 1
            assert recovery_queue.reserve(saved, "Alice", source + timedelta(minutes=46), "other") is None


def test_old_day_does_not_carry_and_generation_change_is_fail_closed():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, "get_config", return_value=CONFIG), patch.object(recovery_queue.random, "randint", return_value=45):
        recovery_queue.schedule_target(saved, "Alice", source, source, "refusal")
        assert recovery_queue.target_entry(saved, "Alice", local(11, day=7)) == {}
        assert recovery_queue.schedule_target(saved, "Alice", local(11, day=7), local(11, day=7), "unsent")["day"] == "2026-11-07"
        saved["login_generation"] = "g2"
        assert recovery_queue.target_entry(saved, "Alice", local(12, day=7)) is None


def test_stable_alias_upgrade_survives_but_name_rebinding_and_bad_rows_do_not():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, "get_config", return_value=CONFIG), patch.object(recovery_queue.random, "randint", return_value=45):
        recovery_queue.schedule_target(saved, "Alice", source, source, "refusal")
        saved["target_uid_bindings"] = {"Renamed": {"uid": "100", "secUid": "sec-100"}}
        assert recovery_queue.target_entry(saved, "Renamed", local(12))["state"] == "waiting"
        saved["target_uid_bindings"] = {"Renamed": {"uid": "other", "secUid": "sec-other"}}
        assert recovery_queue.target_entry(saved, "Renamed", local(12)) is None
        saved["target_uid_bindings"] = {"Alice": {"uid": "100"}}
        saved[recovery_queue.KEY]["peer:100"]["sequence"] = True
        assert recovery_queue.target_entry(saved, "Alice", local(12)) is None


def test_first_reservation_replaces_provably_stale_row():
    saved, yesterday = account(), local(11, day=5)
    with patch.object(recovery_queue, "get_config", return_value=CONFIG), patch.object(recovery_queue.random, "randint", return_value=45):
        recovery_queue.schedule_target(saved, "Alice", yesterday, yesterday, "refusal")
        token = recovery_queue.reserve(saved, "Alice", local(11, day=6), "fresh-run", first=True)
    assert token and token["day"] == "2026-11-06"


def test_account_cooldown_is_independent_and_bad_record_blocks():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, "get_config", return_value=CONFIG), patch.object(recovery_queue.random, "randint", return_value=60) as delay:
        row = recovery_queue.schedule_account(saved, source, source, "network_unavailable")
        assert recovery_queue.schedule_account(saved, local(12), source, "network_unavailable")["nextAttemptAt"] == row["nextAttemptAt"]
        assert recovery_queue.account_blocked(saved, local(11, 59))
        recovery_queue.schedule_target(saved, "Alice", source, source, "unsent")
        recovery_queue.finish_target(saved, "Alice", local(12))
        assert recovery_queue.account_blocked(saved, local(11, 59))
        assert not recovery_queue.account_blocked(saved, local(12))
        delay.assert_called_once_with(45, 90)
    saved[recovery_queue.ACCOUNT_KEY]["sourceAt"] = "bad"
    with patch.object(recovery_queue, "get_config", return_value=CONFIG):
        assert recovery_queue.account_blocked(saved, local(12))


def test_reconciliation_cannot_unlock_an_unresolved_claim():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, 'get_config', return_value=CONFIG):
        assert recovery_queue.reserve(saved, 'Alice', source, 'crashed', first=True)
        assert recovery_queue.schedule_target(saved, 'Alice', local(17), local(17), 'unsent') is None
        assert recovery_queue.target_entry(saved, 'Alice', local(17))['state'] == 'claimed'


def test_first_claim_cannot_bypass_an_existing_retry_deadline():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, 'get_config', return_value=CONFIG), patch.object(recovery_queue.random, 'randint', return_value=90):
        recovery_queue.schedule_target(saved, 'Alice', source, source, 'refusal')
        assert recovery_queue.reserve(saved, 'Alice', local(11, 1), 'bypass', first=True) is None


def test_later_rejection_keeps_all_previously_observed_identity_aliases():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, 'get_config', return_value=CONFIG), patch.object(recovery_queue.random, 'randint', return_value=45):
        recovery_queue.schedule_target(saved, 'Alice', source, source, 'refusal')
        saved['target_uid_bindings'] = {'Renamed': {'uid': '100', 'secUid': 'sec-100'}}
        assert recovery_queue.target_entry(saved, 'Renamed', local(12))
        saved['target_uid_bindings'] = {'Renamed': {'secUid': 'sec-100'}}
        recovery_queue.schedule_target(saved, 'Renamed', local(12), local(12), 'refusal')
        saved['target_uid_bindings']['Old Alias'] = {'uid': '100'}
        assert recovery_queue.target_entry(saved, 'Old Alias', local(12))['state'] == 'waiting'


def test_malformed_kind_or_claim_chronology_is_not_authority():
    from copy import deepcopy
    saved, source = account(), local(11)
    with patch.object(recovery_queue, 'get_config', return_value=CONFIG):
        recovery_queue.reserve(saved, 'Alice', source, 'first', first=True)
        for field, value in [('kind', 'unrecognized'), ('claimedAt', local(10).isoformat()), ('sequence', 0)]:
            bad = deepcopy(saved)
            next(iter(bad[recovery_queue.KEY].values()))[field] = value
            assert recovery_queue.target_entry(bad, 'Alice', local(12)) is None


def test_immediate_proven_first_refusal_can_share_the_claim_second():
    saved, source = account(), local(11)
    with patch.object(recovery_queue, 'get_config', return_value=CONFIG), patch.object(recovery_queue.random, 'randint', return_value=45) as random_wait:
        recovery_queue.reserve(saved, 'Alice', source, 'fast', first=True)
        row = recovery_queue.schedule_target(saved, 'Alice', source, source, 'refusal')
        assert row['state'] == 'waiting' and row['kind'] == 'refusal'
        recovery_queue.schedule_target(saved, 'Alice', source, source, 'refusal')
        random_wait.assert_called_once_with(45, 90)
