"""Public synthetic behavior counterexamples, invoked only by child_harness."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch
from tests import test_identity_trust as f
from core import identity, login, streak_state
from utils import config
from webui import app as app


class SaveGuards(unittest.TestCase):
    def cli(self, store):
        with patch.object(login, 'update_user_data', side_effect=store.update):
            return login.save_cli_login(f.trusted(), {'username': 'new name', 'display_id': 'abc_123', 'cookies': f.cookies('new')}, ['replacement'])

    def test_no_automatic_deletion_or_reference_removal(self):
        a = f.legacy_row(unique_id='1001')
        b = f.legacy_row(account_ref='acc-2', unique_id='1001', targets=['second target'])
        store = f.FakeStore([a, b])
        refs = ['acc-1', 'acc-2']
        def remove(removed):
            refs[:] = [ref for ref in refs if ref not in removed]
        with (patch.object(app, 'update_user_data', side_effect=store.update), patch.object(app, 'remove_account_refs_from_users', side_effect=remove)):
            app.save_exported_login_result({'username': 'new', 'cookies': f.cookies('new')}, relogin_account_ref='acc-1', identity=f.trusted(), legacy_check={'account_ref': 'acc-1', 'fingerprint': identity.record_fingerprint(a), 'uid': f.UID})
        self.assertEqual(['acc-1', 'acc-2'], [row['account_ref'] for row in store.accounts])
        self.assertEqual(b, store.accounts[1])
        self.assertEqual(['acc-1', 'acc-2'], refs)

    def test_cli_key_collision_is_refused(self):
        store = f.FakeStore([f.legacy_row(unique_id=f.UID, **f.bound(f.OTHER_UID))])
        before = copy.deepcopy(store.accounts)
        with self.assertRaises(RuntimeError):
            self.cli(store)
        self.assertEqual(before, store.accounts)
        self.assertEqual(0, store.writes)

    def test_cli_recovery_restores_scheduled_preflight_and_preserves_ledger(self):
        store = f.FakeStore([f.legacy_row(**f.bound(), login_required=True, pending_login_verification=True, account_failure={'category': 'account_identity_mismatch'}, identity_mismatch={'expected': 'x', 'actual': 'y'}, account_health={'healthy': False, 'category': 'login_required'}, failure_queue={'friend': {'category': 'protocol_user_blocked', 'attempts': 2}})])
        before = copy.deepcopy(store.accounts[0])
        self.cli(store)
        row = store.accounts[0]
        self.assertTrue(streak_state.preflight_account(row)['healthy'])
        for field in ('targets', 'target_states', 'failure_queue', 'account_ref', 'unique_id'):
            self.assertEqual(before[field], row[field])
        self.assertNotIn('account_failure', row)

    def test_old_cli_background_token_cannot_write_a_conflict(self):
        store = f.FakeStore([f.legacy_row(**f.bound(), login_verification={'state': 'verifying', 'token': 'old-token'})])
        self.cli(store)
        before = copy.deepcopy(store.accounts)
        with patch.object(app, 'update_user_data', side_effect=store.update):
            app.write_login_verification('acc-1', 'old-token', 'identity_conflict', expected_uid=f.UID, actual_uid=f.OTHER_UID)
        self.assertEqual(before, store.accounts)

    def test_non_login_account_health_survives_web_and_cli_login(self):
        health = {'healthy': False, 'category': 'protocol_rate_limited', 'reason': 'synthetic rate limit'}
        for path in ('web', 'cli'):
            with self.subTest(path=path):
                store = f.FakeStore([f.legacy_row(**f.bound(), account_health=health, account_failure={'category': 'protocol_rate_limited'})])
                if path == 'cli':
                    self.cli(store)
                else:
                    with patch.object(app, 'update_user_data', side_effect=store.update):
                        app.save_exported_login_result({'username': 'new', 'cookies': f.cookies('new')}, relogin_account_ref='acc-1', identity=f.trusted())
                self.assertEqual(health, store.accounts[0].get('account_health'))
                self.assertEqual('protocol_rate_limited', store.accounts[0]['account_failure']['category'])

    def test_cli_original_binding_changed_during_network_check_refuses_creation(self):
        store = f.FakeStore([f.legacy_row(**f.bound())])
        page = Mock(goto=AsyncMock())
        context = Mock(new_page=AsyncMock(return_value=page))
        browser = Mock(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = Mock(stop=AsyncMock())
        def fetch(_cookies):
            store.accounts[0].update(f.bound(f.OTHER_UID))
            return f.trusted()
        with (patch.object(login, 'get_browser', new=AsyncMock(return_value=(playwright, browser))), patch.object(login, 'collect_login_result', new=AsyncMock(return_value={'username': 'new', 'display_id': 'abc_123', 'cookies': f.cookies('new')})), patch.object(login, 'fetch_trusted_identity', side_effect=fetch), patch.object(config, 'get_userData', side_effect=lambda **kw: copy.deepcopy(store.accounts)), patch.object(login, 'update_user_data', side_effect=store.update)):
            with self.assertRaises(RuntimeError):
                asyncio.run(login.userLogin(targets=['new target']))
        self.assertEqual(0, store.writes)
        self.assertEqual(1, len(store.accounts))


    def test_cli_new_same_uid_row_after_snapshot_is_not_overwritten(self):
        for existing in ([], [f.legacy_row(account_ref='acc-other', **f.bound(f.OTHER_UID))]):
            with self.subTest(existing=bool(existing)):
                expected = {row['account_ref']: identity.record_fingerprint(row) for row in existing}
                newer = f.legacy_row(account_ref='acc-new', cookies=f.cookies('newer-web-login'), **f.bound())
                store = f.FakeStore(existing + [newer])
                before = copy.deepcopy(store.accounts)
                with patch.object(login, 'update_user_data', side_effect=store.update):
                    with self.assertRaises(identity.AccountChangedConcurrently):
                        login.save_cli_login(f.trusted(), {'username': 'older-cli', 'cookies': f.cookies('older-cli')}, [], expected_records=expected)
                self.assertEqual(before, store.accounts)
                self.assertEqual(0, store.writes)


class CookieGuards(f._CookieRouteBase):
    def post(self, *, cookie='sessionid=synthetic; sid_guard=g', extra=None, verify=None, operator=None, exported_username='火花号'):
        operator = operator or self._principal()
        answer = (True, '', {'unique_id': f.UID, 'display_id': 'abc_123', 'username': exported_username, 'trusted': f.trusted()}, '')
        data = {'csrf_token': 't', 'cookie_input': cookie, 'relogin_unique_id': '31415926535'}
        data.update(extra or {})
        with (patch.object(app, 'current_principal', side_effect=lambda req: operator), patch.object(app, 'validate_csrf', return_value=True), patch.object(app, 'verify_login_result', new=AsyncMock(side_effect=verify) if verify else AsyncMock(return_value=answer)), patch.object(identity, 'fetch_trusted_identity', side_effect=identity.IdentityUnavailable('synthetic expired')), patch.object(app, 'call_login_desktop', return_value={'ok': True})):
            return self.client.post('/accounts/cookies', data=data)

    def ask(self, operator=None):
        response = self.post(operator=operator)
        self.assertEqual(409, response.status_code, response.text)
        return response.json()['identity_confirmation']['token']

    def test_same_uid_cookie_candidate_change_is_refused(self):
        token = self.ask()
        before = self.accounts_path.read_bytes()
        response = self.post(cookie='sessionid=changed; sid_guard=g', extra={'confirm_identity': token})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.accounts_path.read_bytes())

    def test_confirmation_with_display_name_override_binds_original_username(self):
        asked = self.post(extra={'display_name': 'fixed alias'})
        self.assertEqual(409, asked.status_code, asked.text)
        token = asked.json()['identity_confirmation']['token']
        before = self.accounts_path.read_bytes()
        response = self.post(extra={'display_name': 'fixed alias', 'confirm_identity': token}, exported_username='different exported username')
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.accounts_path.read_bytes())

    def test_confirmation_cannot_repoint_explicit_target(self):
        self.accounts_path.write_text(json.dumps([f.legacy_row(), f.legacy_row(account_ref='acc-2', unique_id='2002')]), encoding='utf-8')
        users = json.loads(self.users_path.read_text(encoding='utf-8'))
        users['users'][0]['account_refs'] = ['acc-1', 'acc-2']
        self.users_path.write_text(json.dumps(users), encoding='utf-8')
        operator = self._principal(refs=('acc-1', 'acc-2'))
        token = self.ask(operator)
        before = self.accounts_path.read_bytes()
        response = self.post(operator=operator, extra={'confirm_identity': token, 'relogin_unique_id': '2002'})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.accounts_path.read_bytes())

    def test_bound_concurrent_cookie_write_is_not_overwritten(self):
        self.accounts_path.write_text(json.dumps([f.legacy_row(**f.bound())]), encoding='utf-8')
        def verify(*args, **kwargs):
            rows = json.loads(self.accounts_path.read_text(encoding='utf-8'))
            rows[0]['cookies'] = f.cookies('concurrent-login')
            self.accounts_path.write_text(json.dumps(rows), encoding='utf-8')
            return (True, '', {'username': '火花号', 'display_id': 'abc_123', 'trusted': f.trusted()}, '')
        response = self.post(verify=verify)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual('concurrent-login', self._row()['cookies'][0]['value'])

    def test_lock_time_permission_recheck_after_network_await(self):
        self.accounts_path.write_text(json.dumps([f.legacy_row(**f.bound())]), encoding='utf-8')
        before = self.accounts_path.read_bytes()
        operator = self._principal()
        real_update = app.update_user_data
        def update_with_permission_race(mutator, **kwargs):
            def revoke_then_save(accounts):
                # The last pre-save permission check already passed. Ownership
                # changes immediately before the actual locked save mutator.
                operator['account_refs'] = []
                users = json.loads(self.users_path.read_text(encoding='utf-8'))
                users['users'][0]['account_refs'] = []
                users['users'][1]['account_refs'] = ['acc-1']
                self.users_path.write_text(json.dumps(users), encoding='utf-8')
                return mutator(accounts)
            return real_update(revoke_then_save, **kwargs)
        self.client = f.TestClient(app.app, raise_server_exceptions=False)
        with patch.object(app, 'update_user_data', side_effect=update_with_permission_race):
            response = self.post(operator=operator)
        self.assertEqual(before, self.accounts_path.read_bytes(), 'Unauthorized save changed account data before reference update failed')
        self.assertEqual(403, response.status_code, response.text)
        users = json.loads(self.users_path.read_text(encoding='utf-8'))['users']
        self.assertEqual([], users[0]['account_refs'])
        self.assertEqual(['acc-1'], users[1]['account_refs'])

    def test_other_account_revocation_is_not_written_back_by_login(self):
        for reassigned in (False, True):
            with self.subTest(reassigned=reassigned):
                self.accounts_path.write_text(json.dumps([f.legacy_row(**f.bound()), f.legacy_row(account_ref='acc-2', unique_id='2002')]), encoding='utf-8')
                users = json.loads(self.users_path.read_text(encoding='utf-8'))
                users['users'][0]['account_refs'] = ['acc-1', 'acc-2']
                users['users'][1]['account_refs'] = []
                self.users_path.write_text(json.dumps(users), encoding='utf-8')
                operator = self._principal(refs=('acc-1', 'acc-2'))
                def verify(*args, **kwargs):
                    live = json.loads(self.users_path.read_text(encoding='utf-8'))
                    live['users'][0]['account_refs'] = ['acc-1']
                    live['users'][1]['account_refs'] = ['acc-2'] if reassigned else []
                    self.users_path.write_text(json.dumps(live), encoding='utf-8')
                    return (True, '', {'username': '火花号', 'display_id': 'abc_123', 'trusted': f.trusted()}, '')
                self.client = f.TestClient(app.app, raise_server_exceptions=False)
                response = self.post(operator=operator, verify=verify)
                self.assertEqual(200, response.status_code, response.text)
                live = json.loads(self.users_path.read_text(encoding='utf-8'))['users']
                self.assertEqual(['acc-1'], live[0]['account_refs'])
                self.assertEqual(['acc-2'] if reassigned else [], live[1]['account_refs'])

    def test_auto_match_disappearing_or_rebound_refuses_creation(self):
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                self.accounts_path.write_text(json.dumps([f.legacy_row(**f.bound())]), encoding='utf-8')
                def verify(*args, **kwargs):
                    rows = [] if deleted else [f.legacy_row(**f.bound(f.OTHER_UID))]
                    self.accounts_path.write_text(json.dumps(rows), encoding='utf-8')
                    return (True, '', {'username': 'another name', 'display_id': 'another_handle', 'trusted': f.trusted()}, '')
                response = self.post(extra={'relogin_unique_id': ''}, verify=verify)
                self.assertEqual(409, response.status_code, response.text)
                rows = json.loads(self.accounts_path.read_text(encoding='utf-8'))
                self.assertEqual(0 if deleted else 1, len(rows))
                if rows:
                    self.assertEqual(f.OTHER_UID, rows[0]['platform_uid'])
