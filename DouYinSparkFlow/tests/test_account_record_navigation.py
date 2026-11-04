"""Account links select durable records while old numeric links still work."""
import re
import unittest
from copy import deepcopy
from html.parser import HTMLParser

from tests import test_shared_security as fixtures
from utils import config
from webui import users
from webui.account_binding import binding_progress


class Ids(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        value = dict(attrs).get('id')
        if value:
            self.ids.append(value)


class AccountRecordNavigationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login()

    def test_binding_result_points_to_actual_record_fold(self):
        account = config.get_userData(force_reload=True)[0]
        url = binding_progress(account)['next_url']
        self.assertEqual('/accounts?_anchor=account-a1', url)
        self.assertIn('account-a1', Ids(self.client.get(url).text).ids)

    def test_legacy_numeric_redirect_retains_one_alias_without_duplicate_ids(self):
        accounts = config.get_userData(force_reload=True)
        accounts.append(dict(deepcopy(accounts[0]), account_ref='a3'))
        config.save_userData(accounts)
        users.update_web_user('alice', append_account_ref='a3')
        response = self.client.get('/accounts%23account-111', follow_redirects=False)
        self.assertEqual(303, response.status_code)
        ids = Ids(self.client.get(response.headers['location']).text).ids
        self.assertEqual(1, ids.count('account-111'))
        self.assertEqual(1, ids.count('account-a1'))
        self.assertEqual(1, ids.count('account-a3'))
        self.assertEqual(len(ids), len(set(ids)))

    def test_legacy_alias_never_shadows_any_visible_record_reference(self):
        accounts = config.get_userData(force_reload=True)
        # Existing refs need not use the newer acc-<uuid> convention.
        accounts[0]['account_ref'] = '111'
        accounts.append(dict(deepcopy(accounts[0]), account_ref='a3', unique_id='333'))
        accounts.append(dict(deepcopy(accounts[0]), account_ref='333', unique_id='444'))
        config.save_userData(accounts)
        users.update_web_user('alice', account_refs=['111', 'a3', '333'])
        self.client = self.fixture.login()
        ids = Ids(self.client.get('/accounts').text).ids
        self.assertEqual(1, ids.count('account-111'))
        self.assertEqual(1, ids.count('account-333'))
        self.assertEqual(len(ids), len(set(ids)))
