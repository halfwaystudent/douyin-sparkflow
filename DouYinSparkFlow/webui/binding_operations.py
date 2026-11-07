"""Bounded, session-owned receipts for long-running workspace saves."""

import asyncio
import json
import re
import threading
import time

from fastapi.responses import JSONResponse


ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
RESULT_FIELDS = (
    "ok", "action", "verified", "verification", "verification_error", "verification_category",
    "friend_refresh", "identityState", "identityLabel", "error", "message", "category",
    "categoryLabel", "retryable", "binding_pending", "hint",
)
ACCOUNT_FIELDS = ("account_ref", "login_generation", "unique_id", "username", "platform_uid", "display_id", "enabled", "healthy")
CANDIDATE_FIELDS = ("account_ref", "username", "unique_id")
CONFIRMATION_FIELDS = ("token", "account_ref", "username", "history_id", "history_id_type", "platform_uid", "display_id", "expires_in")
WORKSPACE_FIELDS = ("state", "position", "ticket", "remaining_seconds", "queue_length", "active", "active_username")
SAVE_STAGES = {
    "accepted": "保存请求已接收，尚未保存账号，请不要重复提交。",
    "exporting": "正在读取当前登录信息；尚未写入账号记录。",
    "checking_identity": "正在核对抖音账号身份；尚未写入账号记录。",
    "saving": "正在写入账号记录，请不要重复提交。",
    "saved": "账号已保存，正在等待后台登录核验。",
}


def _fields(source, allowed):
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in allowed if key in source and (source[key] is None or isinstance(source[key], (str, bool, int, float)))}


def _receipt_result(result):
    projected = _fields(result, RESULT_FIELDS)
    for key, fields in (("account", ACCOUNT_FIELDS), ("duplicate_candidate", CANDIDATE_FIELDS),
                        ("identity_confirmation", CONFIRMATION_FIELDS), ("workspace", WORKSPACE_FIELDS)):
        if isinstance(result.get(key), dict):
            projected[key] = _fields(result[key], fields)
    if isinstance(result.get("duplicate_candidates"), list):
        projected["duplicate_candidates"] = [_fields(item, CANDIDATE_FIELDS) for item in result["duplicate_candidates"] if isinstance(item, dict)]
    return projected


def owner_key(principal):
    return tuple(str(principal.get(key) or "") for key in ("username", "role", "session_id"))


class BindingOperation:
    def __init__(self, operation_id, owner, ticket):
        self.operation_id = operation_id
        self.owner = owner
        self.ticket = ticket
        self.created = time.monotonic()
        self.state = "running"
        self.stage = "accepted"
        self.result = None
        self.http_status = 202
        self.write_started = False
        self.saved_result = None
        self.task = None
        self.lock = threading.Lock()

    def begin_write(self):
        with self.lock:
            self.write_started = True
            self.stage = "saving"

    def set_stage(self, stage):
        if stage not in SAVE_STAGES:
            raise ValueError("Unknown save stage")
        with self.lock:
            if self.state == "running" and self.saved_result is None:
                self.stage = stage

    def record_saved(self, account, action):
        # Keep only the same small projection already returned to the owner.
        with self.lock:
            self.stage = "saved"
            self.saved_result = {
                "ok": True, "action": action, "verification": "pending",
                "account": _fields(account, ("account_ref", "login_generation", "unique_id", "username")),
                "message": "账号已保存，后台核验稍后继续。",
            }

    def finish(self, result, status):
        with self.lock:
            if self.saved_result is not None and (status >= 400 or not result.get("ok")):
                result, status = self.saved_result, 200
            uncertain = bool(result.get("binding_pending")) or (status >= 500 and self.write_started and result.get("binding_pending") is not False)
            self.state = "done" if status < 400 and result.get("ok") else "uncertain" if uncertain else "failed"
            self.result, self.http_status = _receipt_result(result), status

    def payload(self):
        with self.lock:
            return {"ok": True, "operation_id": self.operation_id, "ticket": self.ticket, "state": self.state,
                    "stage": self.stage, "message": SAVE_STAGES[self.stage],
                    "status_url": "/login-desktop/save/status?operation_id=" + self.operation_id,
                    "result": self.result, "http_status": self.http_status}


class BindingOperations:
    def __init__(self):
        self.operations = {}

    def find(self, principal, operation_id):
        if not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id):
            return None
        key = (owner_key(principal), operation_id)
        operation = self.operations.get(key)
        if operation and operation.state != "running" and time.monotonic() - operation.created > 900:
            self.operations.pop(key, None)
            return None
        return operation

    def running(self, principal):
        owner = owner_key(principal)
        return next((operation for operation in self.operations.values() if operation.owner == owner and operation.state == "running"), None)

    def start(self, principal, operation_id, ticket, callback):
        if not isinstance(operation_id, str) or not ID_PATTERN.fullmatch(operation_id):
            raise ValueError("Invalid binding operation ID")
        now = time.monotonic()
        for key, operation in list(self.operations.items()):
            if operation.state != "running" and now - operation.created > 900:
                del self.operations[key]
        key = (owner_key(principal), operation_id)
        if key in self.operations:
            return self.operations[key]
        if len(self.operations) >= 128:
            completed = [(key, op) for key, op in self.operations.items() if op.state != "running"]
            if not completed:
                raise RuntimeError("保存请求较多，请稍后重试。")
            del self.operations[min(completed, key=lambda item: item[1].created)[0]]
        operation = BindingOperation(operation_id, key[0], ticket)
        self.operations[key] = operation

        async def run():
            try:
                response = await callback(operation)
                result = json.loads(response.body)
                if not isinstance(result, dict):
                    raise ValueError("Invalid binding response")
                operation.finish(result, response.status_code)
            except asyncio.CancelledError:
                operation.finish({"ok": False, "error": "服务已中断，请先核对保存结果。", "binding_pending": operation.write_started}, 503)
                raise
            except Exception as exc:
                # Upstream error text may contain exported login data; keep
                # only the error type in this operation's log and receipt.
                import logging
                logging.getLogger(__name__).error("Workspace save operation %s failed (%s)", operation_id, type(exc).__name__)
                operation.finish({"ok": False, "error": "保存未完成，请稍后重试或联系管理员。" if not operation.write_started else "保存结果暂时无法确认，请先核对账号。",
                                  "binding_pending": operation.write_started}, 503)

        operation.task = asyncio.create_task(run())

        def completed(task):
            # Cancellation can arrive before run() executes its first line.
            # Such a task never reaches the handler inside the coroutine.
            if task.cancelled() and operation.state == "running":
                operation.finish({"ok": False, "error": "服务已中断，请先核对保存结果。", "binding_pending": operation.write_started}, 503)

        operation.task.add_done_callback(completed)
        return operation

    @staticmethod
    def response(operation):
        return JSONResponse(operation.payload(), status_code=202, headers={"Cache-Control": "no-store"})
