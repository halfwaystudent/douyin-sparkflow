"""Credential-bearing identity requests must not follow redirects."""

import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from core import identity


class IdentityRedirectSafetyTests(unittest.TestCase):
    def test_redirects_never_create_a_followup_request(self):
        handler = identity._NoIdentityRedirect()
        request = urllib.request.Request(identity.USER_TOKEN_URL, headers={"Cookie": "sessionid=synthetic"})
        for destination in ("https://other.example/", identity.USER_TOKEN_URL, "http://creator.douyin.com/"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(destination=destination, status=status):
                    self.assertIsNone(handler.redirect_request(request, None, status, "redirect", {}, destination))

    def test_transport_installs_redirect_guard_and_returns_only_status(self):
        request = identity._build_request([{"name": "sessionid", "value": "synthetic"}])
        with patch.object(urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.HTTPError(
                identity.USER_TOKEN_URL, 302, "redirect", {}, None
            )
            self.assertEqual((302, ""), identity._open_once(request, None, 1))
        self.assertTrue(any(isinstance(item, identity._NoIdentityRedirect) for item in build.call_args.args))


if __name__ == "__main__":
    unittest.main()
