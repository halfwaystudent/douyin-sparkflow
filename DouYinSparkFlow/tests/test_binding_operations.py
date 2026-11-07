"""Detached saves retain an owner-scoped, credential-free result."""

import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import JSONResponse, PlainTextResponse

from webui import binding_operations as receipts


ALICE = {"username": "alice", "role": "user", "session_id": "alice-session"}
OPERATION_ID = "save-request-00000001"
ACCOUNT = {"account_ref": "account-1", "login_generation": "generation-1", "unique_id": "123", "username": "My account"}


class BindingReceiptTests(unittest.TestCase):
    def operation(self):
        return receipts.BindingOperation(OPERATION_ID, receipts.owner_key(ALICE), "owned-ticket")

    def test_running_projection_contains_its_ticket_but_no_session_or_owner(self):
        payload = self.operation().payload()
        self.assertEqual("running", payload["state"])
        self.assertEqual(202, payload["http_status"])
        self.assertIsNone(payload["result"])
        self.assertNotIn("alice-session", json.dumps(payload))
        self.assertEqual("owned-ticket", payload["ticket"])

    def test_http_failures_distinguish_prewrite_attempt_and_known_rollback(self):
        cases = [(False, {}, "failed"), (True, {}, "uncertain"),
                 (True, {"binding_pending": False}, "failed"),
                 (False, {"binding_pending": True}, "uncertain")]
        for write_started, result, state in cases:
            with self.subTest(write_started=write_started, result=result):
                operation = self.operation()
                if write_started:
                    operation.begin_write()
                operation.finish({"ok": False, **result}, 503)
                self.assertEqual(state, operation.payload()["state"])
                self.assertEqual(503, operation.payload()["http_status"])

    def test_success_after_save_remains_done_when_a_later_step_fails(self):
        operation = self.operation()
        operation.begin_write()
        operation.record_saved({**ACCOUNT, "cookies": [{"name": "sessionid", "value": "private-cookie"}]}, "created")
        operation.finish({"ok": False, "error": "postcommit failure", "binding_pending": True}, 503)
        payload = operation.payload()
        self.assertEqual("done", payload["state"])
        self.assertEqual(200, payload["http_status"])
        self.assertEqual(ACCOUNT, payload["result"]["account"])
        self.assertEqual("created", payload["result"]["action"])
        self.assertNotIn("private-cookie", json.dumps(payload))

    def test_result_projection_preserves_confirmation_and_removes_credentials(self):
        result = {
            "ok": False, "category": "identity_confirmation_required", "retryable": True,
            "identity_confirmation": {"token": "one-use-confirmation", "account_ref": "account-1", "username": "My account",
                "history_id": "old-id", "history_id_type": "display ID", "platform_uid": "platform-1", "display_id": "display-1", "expires_in": 300,
                "cookies": "private-confirmation-cookie"},
            "account": {**ACCOUNT, "enabled": True, "healthy": False, "cookies": "private-account-cookie", "password": "private-password"},
            "duplicate_candidate": {"account_ref": "account-2", "username": "Duplicate", "unique_id": "456", "cookies": "private-candidate-cookie"},
            "duplicate_candidates": [{"account_ref": "account-2", "username": "Duplicate", "unique_id": "456", "password": "private-candidate-password"}],
            "workspace": {"state": "active", "position": 0, "ticket": "workspace-ticket", "remaining_seconds": 30, "queue_length": 1,
                "active": True, "active_username": "alice", "session_id": "private-workspace-session"},
            "cookies": "private-cookie", "session_secret": "private-session-secret", "raw_export": {"cookies": "private-export"},
        }
        original = deepcopy(result)
        operation = self.operation()
        operation.finish(result, 409)
        projected = operation.payload()["result"]
        self.assertEqual(original, result)
        self.assertEqual("one-use-confirmation", projected["identity_confirmation"]["token"])
        self.assertEqual("workspace-ticket", projected["workspace"]["ticket"])
        self.assertEqual(300, projected["identity_confirmation"]["expires_in"])
        self.assertEqual(True, projected["retryable"])
        self.assertEqual({"account_ref": "account-2", "username": "Duplicate", "unique_id": "456"}, projected["duplicate_candidate"])
        self.assertEqual([projected["duplicate_candidate"]], projected["duplicate_candidates"])
        self.assertNotIn("private-", json.dumps(projected))

    def test_scalar_fields_cannot_smuggle_nested_credential_objects(self):
        operation = self.operation()
        operation.finish({"ok": False, "error": {"cookies": "private-cookie"}, "account": {"username": {"password": "private-password"}}}, 400)
        self.assertNotIn("private-", json.dumps(operation.payload()))

    def test_response_is_uncacheable_and_retains_operation_id(self):
        response = receipts.BindingOperations.response(self.operation())
        self.assertEqual(202, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(OPERATION_ID, json.loads(response.body)["operation_id"])


class BindingOperationJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_id_runs_once_and_returns_same_completed_receipt(self):
        operations = receipts.BindingOperations()
        release = asyncio.Event()
        calls = []
        async def save(operation):
            calls.append(operation.operation_id)
            await release.wait()
            return JSONResponse({"ok": True, "account": ACCOUNT})
        first = operations.start(ALICE, OPERATION_ID, "ticket", save)
        duplicate = operations.start(ALICE, OPERATION_ID, "ticket", save)
        self.assertIs(first, duplicate)
        await asyncio.sleep(0)
        self.assertEqual([OPERATION_ID], calls)
        release.set()
        await first.task
        self.assertIs(first, operations.start(ALICE, OPERATION_ID, "ticket", save))
        self.assertEqual("done", first.state)
        self.assertEqual([OPERATION_ID], calls)

    async def test_receipts_cannot_be_read_across_sessions_users_or_roles(self):
        operations = receipts.BindingOperations()
        callback = AsyncMock(return_value=JSONResponse({"ok": True}))
        first = operations.start(ALICE, OPERATION_ID, "ticket", callback)
        await first.task
        self.assertIs(first, operations.find(ALICE, OPERATION_ID))
        for other in ({**ALICE, "session_id": "new-alice-session"}, {**ALICE, "username": "bob"}, {**ALICE, "role": "admin"}):
            self.assertIsNone(operations.find(other, OPERATION_ID))
            self.assertIsNone(operations.running(other))
        separate = operations.start({**ALICE, "session_id": "new-alice-session"}, OPERATION_ID, "new-ticket", callback)
        await separate.task
        self.assertIsNot(first, separate)
        self.assertEqual(2, callback.call_count)

    async def test_cancelled_http_wait_does_not_cancel_detached_save(self):
        operations = receipts.BindingOperations()
        started, release = asyncio.Event(), asyncio.Event()
        created = []
        async def save(operation):
            started.set()
            await release.wait()
            operation.begin_write()
            operation.record_saved(ACCOUNT, "created")
            return JSONResponse({"ok": True, "account": ACCOUNT})
        async def request():
            created.append(operations.start(ALICE, OPERATION_ID, "ticket", save))
            await asyncio.Event().wait()
        request_task = asyncio.create_task(request())
        await asyncio.wait_for(started.wait(), 1)
        request_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request_task
        operation = created[0]
        self.assertFalse(operation.task.cancelled())
        self.assertEqual("running", operation.state)
        release.set()
        await operation.task
        self.assertEqual("done", operations.find(ALICE, OPERATION_ID).state)

    async def test_callback_exceptions_are_safe_and_preserve_write_certainty(self):
        for write_started, state in ((False, "failed"), (True, "uncertain")):
            with self.subTest(write_started=write_started):
                operations = receipts.BindingOperations()
                async def save(operation):
                    if write_started:
                        operation.begin_write()
                    raise RuntimeError("private-cookie-from-desktop")
                with self.assertLogs(receipts.__name__, level="ERROR") as logged:
                    operation = operations.start(ALICE, OPERATION_ID, "ticket", save)
                    await operation.task
                self.assertEqual(state, operation.state)
                self.assertNotIn("private-cookie", " ".join(logged.output))
                self.assertNotIn("private-cookie", json.dumps(operation.payload()))

    async def test_postcommit_exception_reports_saved_instead_of_inviting_a_duplicate(self):
        operations = receipts.BindingOperations()
        async def save(operation):
            operation.begin_write()
            operation.record_saved(ACCOUNT, "updated")
            raise RuntimeError("background scheduling failed")
        with self.assertLogs(receipts.__name__, level="ERROR"):
            operation = operations.start(ALICE, OPERATION_ID, "ticket", save)
            await operation.task
        self.assertEqual("done", operation.state)
        self.assertEqual(200, operation.http_status)
        self.assertEqual(ACCOUNT, operation.result["account"])

    async def test_malformed_callback_body_fails_without_exposing_its_contents(self):
        for response in (PlainTextResponse("private-cookie-not-json"), JSONResponse(["private-cookie-in-list"])):
            operations = receipts.BindingOperations()
            with self.assertLogs(receipts.__name__, level="ERROR"):
                operation = operations.start(ALICE, OPERATION_ID, "ticket", AsyncMock(return_value=response))
                await operation.task
            self.assertEqual("failed", operation.state)
            self.assertNotIn("private-cookie", json.dumps(operation.payload()))

    async def test_completed_receipt_expires_at_ttl_without_a_new_save(self):
        operations = receipts.BindingOperations()
        operation = operations.start(ALICE, OPERATION_ID, "ticket", AsyncMock(return_value=JSONResponse({"ok": True})))
        await operation.task
        operation.created -= 901
        self.assertIsNone(operations.find(ALICE, OPERATION_ID))
        self.assertEqual({}, operations.operations)

    async def test_running_work_is_not_expired_and_cannot_be_started_again(self):
        operations = receipts.BindingOperations()
        release = asyncio.Event()
        async def save(operation):
            await release.wait()
            return JSONResponse({"ok": True})
        operation = operations.start(ALICE, OPERATION_ID, "ticket", save)
        operation.created -= 901
        try:
            self.assertIs(operation, operations.find(ALICE, OPERATION_ID))
            self.assertIs(operation, operations.start(ALICE, OPERATION_ID, "ticket", save))
            self.assertIs(operation, operations.running(ALICE))
        finally:
            release.set()
            await operation.task

    async def test_capacity_refuses_before_callback_when_all_128_jobs_are_running(self):
        operations = receipts.BindingOperations()
        for index in range(128):
            operation_id = f"full-operation-{index:03d}"
            operations.operations[(receipts.owner_key(ALICE), operation_id)] = receipts.BindingOperation(operation_id, receipts.owner_key(ALICE), "ticket")
        callback = AsyncMock()
        with self.assertRaises(RuntimeError):
            operations.start(ALICE, OPERATION_ID, "ticket", callback)
        callback.assert_not_called()
        self.assertEqual(128, len(operations.operations))

    async def test_capacity_evicts_oldest_completed_receipt_and_keeps_running_work(self):
        operations = receipts.BindingOperations()
        created_at = receipts.time.monotonic() - 200
        for index in range(128):
            operation_id = f"full-operation-{index:03d}"
            operation = receipts.BindingOperation(operation_id, receipts.owner_key(ALICE), "ticket")
            operation.created = created_at + index
            if index in {3, 7}:
                operation.finish({"ok": True}, 200)
            operations.operations[(receipts.owner_key(ALICE), operation_id)] = operation
        created = operations.start(ALICE, OPERATION_ID, "ticket", AsyncMock(return_value=JSONResponse({"ok": True})))
        await created.task
        self.assertEqual(128, len(operations.operations))
        self.assertNotIn((receipts.owner_key(ALICE), "full-operation-003"), operations.operations)
        self.assertIn((receipts.owner_key(ALICE), "full-operation-000"), operations.operations)
        self.assertIn((receipts.owner_key(ALICE), "full-operation-007"), operations.operations)

    async def test_invalid_ids_are_rejected_before_any_callback_or_storage(self):
        operations = receipts.BindingOperations()
        callback = AsyncMock()
        for invalid in ("short", "x" * 129, "x" * 16 + "\n", "中文账号abcdefghijklmnop", "abcdefghijklmnop?x=1", None):
            with self.subTest(operation_id=invalid):
                self.assertIsNone(operations.find(ALICE, invalid))
                with self.assertRaises(ValueError):
                    operations.start(ALICE, invalid, "ticket", callback)
        callback.assert_not_called()
        self.assertEqual({}, operations.operations)

    async def test_job_cancelled_before_its_first_step_does_not_remain_running(self):
        operations = receipts.BindingOperations()
        callback = AsyncMock()
        operation = operations.start(ALICE, OPERATION_ID, "ticket", callback)
        operation.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation.task
        await asyncio.sleep(0)
        self.assertEqual("failed", operation.state)
        self.assertFalse(operation.result["binding_pending"])
        self.assertIsNone(operations.running(ALICE))
        callback.assert_not_called()

    async def test_job_cancellation_distinguishes_prewrite_attempt_and_known_commit(self):
        for stage, state in (("before", "failed"), ("writing", "uncertain"), ("saved", "done")):
            with self.subTest(stage=stage):
                operations = receipts.BindingOperations()
                entered = asyncio.Event()
                async def save(operation):
                    if stage != "before":
                        operation.begin_write()
                    if stage == "saved":
                        operation.record_saved(ACCOUNT, "created")
                    entered.set()
                    await asyncio.Event().wait()
                operation = operations.start(ALICE, OPERATION_ID, "ticket", save)
                await entered.wait()
                operation.task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation.task
                self.assertEqual(state, operation.state)


class BindingReceiptApiTests(unittest.TestCase):
    def setUp(self):
        import base64
        from tests import test_shared_security as fixtures
        self.fixture = fixtures.SharedConsoleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.login()
        self.csrf = self.fixture.token(self.client)
        session = json.loads(base64.b64decode(self.client.cookies.get("session").split(".")[0]))
        self.principal = {"username": session["user"], "role": session["role"], "session_id": session["session_id"]}
        self.operations = self.fixture.application.state.binding_operations
        self.saved = self.fixture.accounts[0]

    def receipt(self, result=None):
        operation = receipts.BindingOperation(OPERATION_ID, receipts.owner_key(self.principal), "owned-ticket")
        operation.finish(result or {"ok": True, "account": self.saved}, 200)
        self.operations.operations[(operation.owner, operation.operation_id)] = operation
        return operation

    def status(self, client=None):
        return (client or self.client).get("/login-desktop/save/status", params={"operation_id": OPERATION_ID})

    def repeat(self):
        return self.client.post("/login-desktop/save", data={"csrf_token": self.csrf, "async_save": "1", "request_id": OPERATION_ID, "ticket": "owned-ticket"})

    def test_owner_can_read_but_other_session_user_and_admin_cannot(self):
        self.receipt()
        response = self.status()
        self.assertEqual(200, response.status_code)
        self.assertEqual("done", response.json()["state"])
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(401, self.status(self.fixture.client).status_code)
        for client in (self.fixture.login(), self.fixture.login("bob", "Second-secret-2026!"), self.fixture.login("owner", "Owner-secret-2026!")):
            with self.subTest(caller=client):
                denied = self.status(client)
                self.assertEqual(404, denied.status_code)
                self.assertNotIn("Alice account", denied.text)
                self.assertNotIn("owned-ticket", denied.text)

    def test_status_and_repeated_post_reject_a_new_login_generation(self):
        from utils import config
        self.receipt()
        changed = deepcopy(self.fixture.accounts)
        changed[0]["login_generation"] = "newer-login"
        config.save_userData(changed)
        for response in (self.status(), self.repeat()):
            self.assertEqual(409, response.status_code)
            self.assertTrue(response.json()["binding_pending"])
            self.assertNotIn("Alice account", response.text)

    def test_status_and_repeated_post_do_not_return_a_deleted_account(self):
        from utils import config
        self.receipt()
        config.save_userData(self.fixture.accounts[1:])
        for response in (self.status(), self.repeat()):
            self.assertEqual(403, response.status_code)
            self.assertNotIn("Alice account", response.text)

    def test_confirmation_and_duplicate_candidates_are_rechecked_before_output(self):
        foreign = self.fixture.accounts[1]
        for result in ({"ok": False, "duplicate_candidate": foreign},
                       {"ok": False, "duplicate_candidates": [foreign]},
                       {"ok": False, "identity_confirmation": {**foreign, "token": "private-confirmation"}}):
            with self.subTest(result=result):
                self.receipt(result)
                for response in (self.status(), self.repeat()):
                    self.assertEqual(403, response.status_code)
                    self.assertNotIn("Bob account", response.text)
                    self.assertNotIn("private-confirmation", response.text)

    def test_revoked_session_during_account_read_cannot_receive_the_receipt(self):
        from webui import app as app_module, users
        self.receipt()
        def read(**kwargs):
            users.update_web_user("alice", enabled=False)
            return deepcopy(self.fixture.accounts)
        with patch.object(app_module, "get_userData", side_effect=read):
            response = self.status()
        self.assertEqual(401, response.status_code)
        self.assertNotIn("Alice account", response.text)

    def test_duplicate_post_still_requires_csrf_and_matching_workspace_ticket(self):
        self.receipt()
        response = self.client.post("/login-desktop/save", data={"async_save": "1", "request_id": OPERATION_ID, "ticket": "owned-ticket", "csrf_token": "wrong"})
        self.assertEqual(403, response.status_code)
        response = self.client.post("/login-desktop/save", data={"async_save": "1", "request_id": OPERATION_ID, "ticket": "another-ticket", "csrf_token": self.csrf})
        self.assertEqual(409, response.status_code)
        self.assertNotIn("Alice account", response.text)


class BindingSaveAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_save_answers_before_export_and_can_be_rechecked_from_a_new_connection(self):
        import threading
        import httpx
        from webui import app as app_module
        fixture = BindingReceiptApiTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        active = {**fixture.principal, "ticket": "owned-ticket", "mode": "add", "account_ref": ""}
        entered, release = threading.Event(), threading.Event()
        def export(path, **kwargs):
            self.assertEqual("/export", path)
            entered.set()
            release.wait(2)
            return {"ok": True, "result": {"username": "Newly signed in", "cookies": []}}
        async def entered_export():
            while not entered.is_set():
                await asyncio.sleep(0.001)
        form = {"csrf_token": fixture.csrf, "async_save": "1", "request_id": OPERATION_ID, "ticket": "owned-ticket"}
        transport = httpx.ASGITransport(app=fixture.fixture.application)
        with patch.object(app_module, "get_login_lock", return_value=active), \
             patch.object(app_module, "call_login_desktop", side_effect=export) as exporter, \
             patch.object(app_module.account_identity, "fetch_trusted_identity", return_value={"uid": "new-platform-id"}), \
             patch.object(app_module, "commit_login_binding", return_value=(fixture.saved, "updated")), \
             patch.object(app_module, "begin_login_release", return_value=False), \
             patch.object(app_module, "get_workspace_state", return_value={"active": active, "queue": []}), \
             patch.object(app_module, "workspace_status", return_value={"state": "active", "ticket": "owned-ticket"}):
            try:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=fixture.client.cookies) as client:
                    accepted = await asyncio.wait_for(client.post("/login-desktop/save", data=form), 0.5)
                    self.assertEqual(202, accepted.status_code)
                    self.assertEqual(OPERATION_ID, accepted.json()["operation_id"])
                    await asyncio.wait_for(entered_export(), 1)
                    self.assertFalse(release.is_set())
                    running = await client.get("/login-desktop/save/status", params={"operation_id": OPERATION_ID})
                    self.assertEqual("running", running.json()["state"])
                # The original HTTP connection is gone while the job continues.
                release.set()
                operation = fixture.operations.find(fixture.principal, OPERATION_ID)
                await asyncio.wait_for(operation.task, 2)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=fixture.client.cookies) as client:
                    done = await client.get("/login-desktop/save/status", params={"operation_id": OPERATION_ID})
                    self.assertEqual(200, done.status_code)
                    self.assertEqual("done", done.json()["state"])
                    self.assertEqual("a1", done.json()["result"]["account"]["account_ref"])
                    duplicate = await client.post("/login-desktop/save", data=form)
                    self.assertEqual(202, duplicate.status_code)
                    self.assertEqual("done", duplicate.json()["state"])
                self.assertEqual(1, exporter.call_count)
            finally:
                release.set()
