"""Operator logins request exactly one new persistent-profile import."""
import unittest
from unittest.mock import patch
from core import identity, login
from webui import app
from tests import test_identity_trust as fixtures


class CookieLoginGenerationTests(unittest.TestCase):
    def test_each_explicit_web_or_cli_save_marks_its_generation_for_import(self):
        for channel in ['web', 'cli']:
            for existing in [False, True]:
                with self.subTest(channel=channel, existing=existing):
                    store = fixtures.FakeStore([fixtures.legacy_row(**fixtures.bound())] if existing else [])
                    def save():
                        if channel == 'web':
                            with patch.object(app, 'update_user_data', side_effect=store.update):
                                return app.save_exported_login_result(
                                    {'username': 'synthetic', 'cookies': fixtures.cookies('fresh')},
                                    identity=fixtures.trusted(),
                                    **({'relogin_account_ref': 'acc-1'} if existing else {}))
                        with patch.object(login, 'update_user_data', side_effect=store.update):
                            return login.save_cli_login(fixtures.trusted(),
                                {'username': 'synthetic', 'cookies': fixtures.cookies('fresh')}, [])
                    save()
                    row = store.accounts[0]
                    first = row[identity.FIELD_LOGIN_GENERATION]
                    self.assertTrue(first)
                    self.assertEqual(row.get('browser_cookie_import_generation'), first)
                    save()
                    row = store.accounts[0]
                    self.assertNotEqual(row[identity.FIELD_LOGIN_GENERATION], first)
                    self.assertEqual(row['browser_cookie_import_generation'], row[identity.FIELD_LOGIN_GENERATION])


if __name__ == '__main__':
    unittest.main()
