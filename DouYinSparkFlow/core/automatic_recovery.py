"""Opt-in single-target, same-day recovery orchestration.

The existing sender still owns browser/account locks, recipient identity,
pre-Enter unknown barriers, receipts and cleanup. This layer only schedules
proven unsent/rejected work and claims a durable per-attempt queue token.
"""
from copy import deepcopy
from datetime import datetime, timezone
import os
import uuid

from core import recovery_queue as Q, refusal_retry, streak_state

TICKET = '_automatic_recovery'
PAGE_CATEGORIES = {'browser_stage_timeout', 'friend_list_unavailable',
                   'network_unavailable', 'browser_page_unavailable'}


def _tasks():
    from core import tasks
    return tasks


def _at(value):
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
        return at if isinstance(at, datetime) and at.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def clock():
    t = _tasks()
    return t.datetime.now(t._schedule_timezone())


def _same_day(value, now):
    at = _at(value)
    return bool(at and at <= now and at.astimezone(now.tzinfo).date() == now.date())


def _blocked_target(account, target, now):
    t = _tasks()
    return (t._target_sent_today(account, target, now)
            or t._target_unconfirmed_today(account, target, now)
            or streak_state.is_sent_unverified(account, target, now)
            or streak_state.attempt_in_progress(account, target, now))


def permitted(account, target, now, *, source=None):
    """Recheck against a fresh ledger, never a stale planner result."""
    t = _tasks()
    source = source if source is not None else account
    if (not Q.enabled() or not Q.allowed_now(now)
            or not account.get('enabled', True)
            or not streak_state.preflight_account(account, now).get('healthy')
            or account.get('browser_cleanup_blocked')
            or Q.account_blocked(account, now)
            or t._account_paused_by_failure_today(account, now)
            or _blocked_target(account, target, now)):
        return False
    if str(account.get('login_generation') or '') != str(source.get('login_generation') or ''):
        return False
    if not any(streak_state.target_keys_match(account, target, x) for x in account.get('targets') or []):
        return False
    if (account.get('account_failure') or {}).get('category') in streak_state.LOGIN_FAILURE_CATEGORIES:
        return False
    row = Q.target_entry(account, target, now)
    if row is None or row.get('state') in {'claimed', 'done'}:
        return False
    state = streak_state.target_state(account, target, now)
    if t._target_failed_today(account, target, now):
        return bool(refusal_retry.explicit_retryable_rejection(account, target, now)
                    and row.get('kind') == 'refusal' and Q.due(account, target, now))
    if state.get('status') == streak_state.STATE_FAILED_TERMINAL:
        if not refusal_retry.rejection_precedes_today(account, target, now):
            return False
    if row:
        return bool(row.get('kind') == 'unsent' and Q.due(account, target, now))
    if streak_state.fallback_attempted_today(account, target, now):
        return False
    config = t.get_config(force_reload=True)
    window = t._normalize_send_window(config)
    if not window.get('enabled') or window.get('invalid'):
        return False
    end = _end(window, now)
    return bool(now < end and now >= t._scheduled_send_time(source, target, window, now))


def _end(window, now):
    if window['endHour'] == 24:
        from datetime import timedelta
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return now.replace(hour=window['endHour'], minute=int(window.get('endMinute') or 0),
                       second=0, microsecond=0)


def dispatch_allowed(user, now=None):
    """Called inside the queued Enter task as well as before browser launch."""
    t = _tasks()
    ticket = user.get(TICKET)
    if not Q.enabled():
        return not bool(ticket)  # Revoking the policy revokes queued recovery work.
    now = now or clock()
    if not Q.allowed_now(now, day=ticket.get('day') if isinstance(ticket, dict) else None):
        return False
    if not ticket:
        return True  # Existing explicitly authorized manual semantics remain separate.
    if not isinstance(ticket, dict) or not ticket.get('reservation'):
        return False
    fresh = streak_state.find_matching_account(t.get_userData(force_reload=True), user)
    if fresh is None or str(fresh.get('login_generation') or '') != str(user.get('login_generation') or ''):
        return False
    target = ticket.get('target')
    if user.get('targets') != [target] or not any(
            streak_state.target_keys_match(fresh, target, x) for x in fresh.get('targets') or []):
        return False
    if (not fresh.get('enabled', True)
            or not streak_state.preflight_account(fresh, now).get('healthy')
            or fresh.get('browser_cleanup_blocked') or Q.account_blocked(fresh, now)):
        return False
    return Q.reservation_matches(fresh, target, now, ticket.get('runId'), ticket['reservation'])


def claim(user, target, run_id):
    """Persist queue reservation and in-flight claim together, then read back."""
    t = _tasks()
    now = clock()
    ticket = user.get(TICKET) or {}
    scope_ok, _ = t._sender_scope_timezone(user)
    if (not scope_ok or ticket.get('runId') != run_id or ticket.get('target') != target
            or not Q.allowed_now(now, day=ticket.get('day'))
            or not t.personal_streak.authorized(user)):
        return False

    def mutate(accounts):
        fresh = streak_state.find_matching_account(accounts, user)
        if (fresh is None or not fresh.get('enabled', True)
                or not streak_state.preflight_account(fresh, now).get('healthy')
                or not permitted(fresh, target, now, source=user)):
            return None, False
        row = Q.target_entry(fresh, target, now)
        # Repetition is allowed only after a proved rejection/unsent queue entry.
        # The permission check above independently refuses confirmed/unknown work.
        state, claimed = streak_state.claim_in_flight(
            fresh, target, run_id=run_id, strategy='browser', now=now,
            allow_retry=bool(row))
        if not claimed:
            return None, False
        token = Q.reserve(fresh, target, now, run_id, first=not bool(row))
        if token is None:
            return None, False
        return (deepcopy(fresh), deepcopy(token)), True

    saved = t.update_user_data(mutate, force_reload=True)
    if saved is None:
        return False
    _saved_account, token = saved
    fresh = streak_state.find_matching_account(t.get_userData(force_reload=True), user)
    if (fresh is None or not Q.reservation_matches(fresh, target, now, run_id, token)):
        return False
    state = streak_state.target_state(fresh, target, now)
    if state.get('status') != streak_state.STATE_IN_FLIGHT or state.get('runId') != run_id:
        return False
    user['target_states'] = deepcopy(fresh.get('target_states') or {})
    user[Q.KEY] = deepcopy(fresh.get(Q.KEY) or {})
    user[TICKET] = dict(ticket, reservation=token)
    return True


def record_refusal(account, target, now):
    if Q.enabled() and refusal_retry.explicit_retryable_rejection(account, target, now):
        failure = streak_state.failure_entry(account, target, now.tzinfo)
        Q.schedule_target(account, target, now, failure['lastAttemptAt'], 'refusal')


def record_account_fault(account, now, category, source_at):
    if Q.enabled() and category in PAGE_CATEGORIES:
        Q.schedule_account(account, now, source_at, category)


def _synchronize(account, window, now):
    t = _tasks()
    failure = account.get('account_failure') or {}
    if failure.get('category') in PAGE_CATEGORIES and _same_day(failure.get('lastAttemptAt'), now):
        Q.schedule_account(account, now, failure['lastAttemptAt'], failure['category'])
    for target in t._dedupe_group_target_aliases(list(account.get('targets') or [])):
        if t._target_sent_today(account, target, now):
            Q.finish_target(account, target, now)
        elif _blocked_target(account, target, now):
            continue
        elif (refusal_retry.explicit_retryable_rejection(account, target, now)
              and _same_day((streak_state.failure_entry(account, target, now.tzinfo) or {}).get('lastAttemptAt'), now)):
            at = (streak_state.failure_entry(account, target, now.tzinfo) or {}).get('lastAttemptAt')
            if _same_day(at, now):
                Q.schedule_target(account, target, now, at, 'refusal')
        elif (now >= _end(window, now)
              and not t._target_failed_today(account, target, now)
              and not streak_state.fallback_attempted_today(account, target, now)):
            Q.schedule_target(account, target, now, _end(window, now), 'unsent')


def _refresh_and_plan(source, config, now):
    t = _tasks()
    window = t._normalize_send_window(config)
    if not window.get('enabled') or window.get('invalid') or not t.personal_streak.authorized(source):
        return None, []

    def mutate(accounts):
        fresh = streak_state.find_matching_account(accounts, source)
        if fresh is None or str(fresh.get('login_generation') or '') != str(source.get('login_generation') or ''):
            return None, False
        before = deepcopy(fresh)
        _synchronize(fresh, window, now)
        return deepcopy(fresh), before != fresh

    fresh = t.update_user_data(mutate, force_reload=True)
    if fresh is None:
        return None, []
    scoped = dict(fresh)
    if '_personal_scope' in source:
        scoped['_personal_scope'] = deepcopy(source['_personal_scope'])
    candidates = []
    if not streak_state.preflight_account(scoped, now).get('healthy'):
        return scoped, candidates
    for target in t._dedupe_group_target_aliases(list(scoped.get('targets') or [])):
        if not permitted(scoped, target, now, source=scoped):
            continue
        row = Q.target_entry(scoped, target, now)
        rank = 1 if row and row.get('kind') == 'refusal' else 0
        due = _at(row.get('nextAttemptAt')) if row else t._scheduled_send_time(scoped, target, window, now)
        candidates.append((rank, due, str(scoped.get('account_ref') or scoped.get('unique_id') or ''),
                           streak_state.resolve_target_ref(scoped, target), target))
    return scoped, candidates


def project_due(user, window, now):
    """Read-only dashboard/planner projection; never redraw a retry deadline."""
    t = _tasks()
    due, confirmed, pending, excluded = [], [], [], []
    for target in t._dedupe_group_target_aliases(list(user.get('targets') or [])):
        if t._target_sent_today(user, target, now):
            confirmed.append(target)
            continue
        row = Q.target_entry(user, target, now)
        if row is None:
            excluded.append(target)
            continue
        if window.get('enabled') and permitted(user, target, now, source=user):
            due.append(target)
        elif _blocked_target(user, target, now) or row.get('state') == 'claimed':
            excluded.append(target)
        else:
            at = _at(row.get('nextAttemptAt')) if row else (
                t._scheduled_send_time(user, target, window, now) if window.get('enabled') else now)
            pending.append((target, at))
    due.sort(key=lambda target: (Q.target_entry(user, target, now).get('kind') == 'refusal',
                                str((Q.target_entry(user, target, now) or {}).get('nextAttemptAt') or '')))
    return due, confirmed, pending, excluded


async def run(config, *, reconcile_only=False, close=False):
    t = _tasks()
    now = clock()
    if not Q.enabled(config):
        return 'skipped_recovery_disabled'
    if not reconcile_only and not Q.allowed_now(now):
        return 'skipped_recovery_protection'
    if config.get('useProtocolSender', True):
        return 'skipped_recovery_requires_browser'
    run_id = uuid.uuid4().hex
    started = now.astimezone(timezone.utc)
    previous_run = t._ACTIVE_RUN_ID
    status, attempted = 'skipped_no_runnable_accounts', False
    try:
        with t.task_run_lock():
            all_rows = t.get_userData(force_reload=True)
            refs = t._requested_account_refs()
            accounts = [a for a in all_rows if a.get('enabled', True)
                        and (refs is None or a.get('account_ref') in refs)]
            groups = t.personal_streak.account_groups(config, accounts,
                manual=False,
                profile_key=os.getenv('SPARKFLOW_PERSONAL_USER_ID', ''),
                revision=os.getenv('SPARKFLOW_PERSONAL_REVISION', ''),
                claim=os.getenv('SPARKFLOW_PERSONAL_CLAIM', ''))
            choices = []
            for group_config, group in groups:
                for source in group:
                    scoped, candidates = _refresh_and_plan(source, group_config, now)
                    for candidate in candidates:
                        choices.append((candidate, group_config, scoped))
            if reconcile_only:
                status = 'reconciled'
                t.logger.info('Recovery local reconciliation completed; browser launches=0 close=%s', close)
            elif choices:
                candidate, group_config, scoped = min(choices, key=lambda value: value[0])
                target = candidate[-1]
                scoped = dict(scoped, targets=[target])
                scoped[TICKET] = {'day': now.date().isoformat(), 'runId': run_id, 'target': target}
                t._ACTIVE_RUN_ID = run_id
                t.logger.info('Recovery selected one target account=%s target=%s tier=%s',
                              scoped.get('username'), target, candidate[0])
                attempted = True
                await t.run_browser_tasks(dict(group_config, multiTask=False, taskCount=1),
                                          [scoped], run_id=run_id)
                status = 'completed'
    except t.TaskRunAlreadyInProgress:
        return 'skipped_task_in_progress'
    finally:
        t._ACTIVE_RUN_ID = previous_run
        if attempted or reconcile_only:
            users = t.get_userData(force_reload=True)
            try:
                t._append_streak_run_report(run_id, started, config, users, run_status=status)
                t.notifications.notify_run_finished(run_id, status, config, users,
                    schedule_tz=t._schedule_timezone(), send_window=t._normalize_send_window(config),
                    manual_run=False, fallback_run=bool(close), now=clock())
            except Exception as exc:
                t.logger.warning('Recovery reporting failed category=%s', type(exc).__name__)
    return status
