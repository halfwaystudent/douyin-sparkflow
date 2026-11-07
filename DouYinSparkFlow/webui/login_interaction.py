"""Authenticated page interaction shared by administrators and ordinary users."""

import asyncio
import base64
import binascii
import hmac
import json

from fastapi import Request
from fastapi.responses import JSONResponse, Response


def register_routes(app, *, principal, owner, expire, csrf_valid, call_desktop):
    gate = asyncio.Semaphore(2)

    def permitted(request, ticket):
        current, active, owns = owner(request)
        return bool(current and owns and active and ticket and ticket.isascii()
                    and hmac.compare_digest(str(active.get("ticket", "")), ticket))

    @app.post("/login-desktop/interaction/{operation}")
    async def interaction(request: Request, operation: str):
        if operation not in {"screen", "action"}:
            return Response(status_code=404)
        if not principal(request):
            return JSONResponse({"error": "请重新登录平台"}, status_code=401)
        form = await request.form()
        if not csrf_valid(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "页面已更新，请刷新后重试"}, status_code=403)
        await expire()
        ticket = str(form.get("ticket", ""))
        if not permitted(request, ticket):
            return JSONResponse({"error": "登录工作区未分配给本会话"}, status_code=423)
        payload = {"ticket": ticket}
        if operation == "action":
            raw = str(form.get("action", ""))
            if len(raw) > 8192:
                return JSONResponse({"error": "输入内容过长"}, status_code=413)
            try:
                payload["action"] = json.loads(raw)
                if not isinstance(payload["action"], dict):
                    raise ValueError
            except ValueError:
                return JSONResponse({"error": "登录操作格式无效"}, status_code=400)
        try:
            await asyncio.wait_for(gate.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "正在处理上一个操作"}, status_code=429, headers={"Retry-After": "1"})
        try:
            if not permitted(request, ticket):
                return JSONResponse({"error": "登录工作区已释放"}, status_code=423)
            try:
                status, result = await asyncio.to_thread(
                    call_desktop, "/interaction", method="POST", payload=payload,
                    timeout=9, max_bytes=4 * 1024 * 1024,
                )
            except RuntimeError:
                return JSONResponse({"error": "登录窗口暂时不可用，请稍后重试"}, status_code=503)
            # A request may have started while this user owned the workspace,
            # then completed after release or handoff. Never deliver that frame.
            if not permitted(request, ticket):
                return JSONResponse({"error": "登录工作区已释放"}, status_code=423)
            if status >= 400 or not result.get("ok"):
                code = status if status in {400, 409, 423, 429, 503} else 503
                return JSONResponse({"error": str(result.get("error") or "登录窗口暂时不可用")[:240]}, status_code=code)
            if operation == "action":
                return JSONResponse({"ok": True})
            try:
                frame = base64.b64decode(result.get("image", ""), validate=True)
                if not frame.startswith(b"\xff\xd8") or len(frame) > 3 * 1024 * 1024:
                    raise ValueError
            except (ValueError, TypeError, binascii.Error):
                return JSONResponse({"error": "登录窗口画面暂时不可用"}, status_code=503)
            return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        finally:
            gate.release()
