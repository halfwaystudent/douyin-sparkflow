"""Leaving a queued login page cancels its ticket without touching other users."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_shared_security as fixtures
from webui import app as app_module, login_lock


class LoginQueueReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        lock = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
        lock.start()
        self.addCleanup(lock.stop)
        self.alice = self.fixture.login()
        self.bob = self.fixture.login("bob", "Second-secret-2026!")
        self.alice_csrf = self.fixture.token(self.alice)
        self.bob_csrf = self.fixture.token(self.bob)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            first = self.alice.post("/login-desktop/open", data={"csrf_token": self.alice_csrf, "mode": "add"})
            second = self.bob.post("/login-desktop/open", data={"csrf_token": self.bob_csrf, "mode": "add"})
        self.assertEqual(200, first.status_code)
        self.assertEqual(202, second.status_code)
        self.alice_ticket = first.json()["workspace"]["ticket"]
        self.bob_ticket = second.json()["workspace"]["ticket"]

    def release(self, client=None, *, ticket=None, csrf=None):
        return (client or self.bob).post("/login-desktop/release", data={
            "csrf_token": self.bob_csrf if csrf is None else csrf,
            "ticket": self.bob_ticket if ticket is None else ticket,
        })

    def test_queued_page_leave_removes_only_its_request(self):
        with patch.object(app_module, "call_login_desktop") as browser:
            response = self.release()
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["released"])
        self.assertTrue(response.json()["cancelled"])
        state = login_lock.get_workspace_state()
        self.assertEqual(self.alice_ticket, state["active"]["ticket"])
        self.assertEqual([], state["queue"])
        browser.assert_not_called()

    def test_cancelled_user_is_not_promoted_after_current_owner_finishes(self):
        self.release()
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            response = self.release(self.alice, ticket=self.alice_ticket, csrf=self.alice_csrf)
        self.assertTrue(response.json()["released"])
        self.assertIsNone(login_lock.get_lock())
        self.assertEqual([], login_lock.get_workspace_state()["queue"])

    def test_empty_foreign_and_invalid_tickets_cannot_cancel_queue(self):
        with patch.object(app_module, "call_login_desktop") as browser:
            for ticket in ("", self.alice_ticket, "ticket-unknown", "无效票据"):
                with self.subTest(ticket=ticket):
                    self.assertFalse(self.release(ticket=ticket).json()["released"])
            self.assertFalse(self.release(self.alice, ticket=self.bob_ticket, csrf=self.alice_csrf).json()["released"])
        state = login_lock.get_workspace_state()
        self.assertEqual(self.alice_ticket, state["active"]["ticket"])
        self.assertEqual([self.bob_ticket], [row["ticket"] for row in state["queue"]])
        browser.assert_not_called()

    def test_old_beacon_cannot_cancel_newer_ticket_from_same_session(self):
        self.assertTrue(self.release().json()["released"])
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            opened = self.bob.post("/login-desktop/open", data={"csrf_token": self.bob_csrf, "mode": "add"})
        replacement = opened.json()["workspace"]["ticket"]
        self.assertNotEqual(self.bob_ticket, replacement)
        self.assertFalse(self.release().json()["released"])
        self.assertEqual([replacement], [row["ticket"] for row in login_lock.get_workspace_state()["queue"]])

    def test_another_session_of_same_user_cannot_cancel_this_session_ticket(self):
        other = self.fixture.login("bob", "Second-secret-2026!")
        csrf = self.fixture.token(other)
        self.assertFalse(self.release(other, ticket=self.bob_ticket, csrf=csrf).json()["released"])
        self.assertEqual([self.bob_ticket], [row["ticket"] for row in login_lock.get_workspace_state()["queue"]])

    def test_queued_ticket_promoted_before_beacon_arrives_releases_only_that_owner(self):
        active = login_lock.get_lock()
        login_lock.begin_release(username=active["username"], session_id=active["session_id"], ticket=active["ticket"])
        promoted = login_lock.finish_transition()
        self.assertEqual(self.bob_ticket, promoted["ticket"])
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            response = self.release()
        self.assertTrue(response.json()["released"])
        self.assertFalse(response.json()["cancelled"])
        self.assertIsNone(login_lock.get_lock())

    def test_removing_last_queued_ticket_during_reset_keeps_transition_barrier(self):
        active = login_lock.get_lock()
        login_lock.begin_release(username=active["username"], session_id=active["session_id"], ticket=active["ticket"])
        with patch.object(app_module, "call_login_desktop") as browser:
            response = self.release()
        self.assertTrue(response.json()["cancelled"])
        state = login_lock.get_workspace_state()
        self.assertEqual("resetting", state["phase"])
        self.assertEqual([], state["queue"])
        browser.assert_not_called()
        next_request = login_lock.request_workspace(username="owner", session_id="next-session", mode="add")
        self.assertEqual("queued", next_request["state"])
        self.assertIsNone(login_lock.get_lock())
        self.assertEqual(next_request["request"]["ticket"], login_lock.finish_transition()["ticket"])

    def test_admin_leaving_queue_cannot_force_reset_current_user(self):
        owner = self.fixture.login("owner", "Owner-secret-2026!")
        csrf = self.fixture.token(owner)
        with patch.object(app_module, "call_login_desktop", return_value={"ok": True}):
            opened = owner.post("/login-desktop/open", data={"csrf_token": csrf, "mode": "add"})
        ticket = opened.json()["workspace"]["ticket"]
        with patch.object(app_module, "call_login_desktop") as browser:
            response = self.release(owner, ticket=ticket, csrf=csrf)
        self.assertTrue(response.json()["cancelled"])
        self.assertEqual(self.alice_ticket, login_lock.get_lock()["ticket"])
        self.assertEqual([self.bob_ticket], [row["ticket"] for row in login_lock.get_workspace_state()["queue"]])
        browser.assert_not_called()

    def test_authentication_csrf_and_repeated_beacon_are_safe(self):
        self.assertEqual(401, self.release(self.fixture.client).status_code)
        self.assertEqual(403, self.release(csrf="invalid").status_code)
        self.assertTrue(self.release().json()["released"])
        self.assertFalse(self.release().json()["released"])
        self.assertEqual(self.alice_ticket, login_lock.get_lock()["ticket"])
