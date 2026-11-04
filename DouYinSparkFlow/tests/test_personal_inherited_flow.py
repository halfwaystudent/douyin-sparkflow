"""An explicitly saved global-message plan works through setup and manual gates."""
import unittest
from unittest.mock import patch

from core.personal_streak import preferences
from tests import test_binding_flow_ui as fixtures
from utils import config
from webui import app as app_module, users
from webui.setup_flow import setup_state


class InheritedMessageFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SetupFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.principal = {'username': 'alice', 'role': 'user'}
        # Simulate the new persisted representation with a genuine revision.
        with users._web_users_lock():
            raw = users._load_raw()
            profile = next(value for value in raw['personal_streak'].values()
                           if value['revision'] == self.fixture.profile['revision'])
            profile['messages'] = []
            users._atomic_write_json(users.USERS_FILE, raw)
        self.profile = preferences(self.principal)

    def test_saved_global_message_choice_can_complete_setup(self):
        state = setup_state(self.fixture.accounts, self.profile, 'a1')
        self.assertTrue(state['has_messages'])
        self.assertTrue(state['using_global_messages'])
        self.assertTrue(state['can_finish'])
        page = self.client.get('/setup?step=3')
        self.assertIn('使用全局默认消息', page.text)
        self.assertNotIn('已保存 0 条', page.text)
        self.assertIn('第 4 步，共 4 步', self.client.get('/setup?step=4').text)
        self.assertEqual('/', self.fixture.finish().headers['location'])

    def test_unconfigured_user_still_cannot_skip_setup(self):
        profile = dict(self.profile, revision='')
        self.assertFalse(setup_state(self.fixture.accounts, profile, 'a1')['can_finish'])

    def test_missing_global_templates_still_block_setup_and_manual_spawns(self):
        empty = {'messageTemplate': '', 'sendStrategy': {'messageVariants': []}, 'happyNewYear': {'enabled': False}}
        with patch('utils.config.get_config', return_value=empty):
            state = setup_state(self.fixture.accounts, self.profile, 'a1')
        self.assertFalse(state['can_finish'])
        self.assertIn('全局', state['setup_blocked_reason'])
        with patch.object(app_module, 'get_config', return_value=empty), patch.object(app_module, 'run_task_now') as run:
            response = self.client.post('/ops/run-now', data={'csrf_token': self.fixture.csrf}, follow_redirects=False)
        self.assertEqual('/preferences/streak', response.headers['location'])
        run.assert_not_called()

    def test_all_manual_entrypoints_accept_saved_inheritance_but_keep_own_scope(self):
        for path, function in (('/ops/run-now', 'run_task_now'), ('/ops/run-failed', 'run_failed_retry_now'),
                               ('/ops/run-unsent', 'run_unsent_retry_now')):
            with self.subTest(path=path), patch.object(app_module, function, return_value=12345) as run:
                response = self.client.post(path, data={'csrf_token': self.fixture.csrf}, follow_redirects=False)
                self.assertEqual('/ops/send-console', response.headers['location'])
                self.assertEqual(['a1'], run.call_args.kwargs['account_refs'])
                if path == '/ops/run-now':
                    self.assertFalse(run.call_args.kwargs['force_all'])

    def test_missing_saved_plan_still_blocks_all_manual_entrypoints(self):
        with users._web_users_lock():
            raw = users._load_raw()
            raw.pop('personal_streak')
            users._atomic_write_json(users.USERS_FILE, raw)
        for path, function in (('/ops/run-now', 'run_task_now'), ('/ops/run-failed', 'run_failed_retry_now'),
                               ('/ops/run-unsent', 'run_unsent_retry_now')):
            with self.subTest(path=path), patch.object(app_module, function) as run:
                response = self.client.post(path, data={'csrf_token': self.fixture.csrf}, follow_redirects=False)
                self.assertEqual('/preferences/streak', response.headers['location'])
                run.assert_not_called()
