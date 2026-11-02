"""Self-service sending preferences; the form cannot name another user."""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from core import personal_streak
from webui.users import _web_users_lock


def schedule_for(principal):
    try:
        profile = personal_streak.preferences(principal)
    except ValueError:
        return None
    if principal.get("role") == "admin" and not profile.get("messages"):
        return None
    enabled = bool(profile.get("enabled"))
    timestamp = profile.get("next_run_at") if enabled else ""
    display = datetime.fromisoformat(timestamp).astimezone(ZoneInfo(profile["timezone"])).strftime("%m-%d %H:%M") if timestamp else ""
    return {
        "personal": True, "enabled": enabled, "label": f"每天 {profile['time']} · {profile['timezone']}" if profile.get("messages") else "尚未设置",
        "nextTriggerAt": timestamp or "", "nextTriggerDisplay": display or "自动发送已暂停", "hasWorkNextTrigger": enabled,
    }


def register_routes(app, *, principal, csrf_valid, render, flash):
    def with_current_session(request, expected, operation):
        with _web_users_lock():
            live = principal(request)
            if not live or any(live.get(key) != expected.get(key) for key in ("username", "role", "session_id")):
                raise PermissionError("登录状态已变化，请重新登录后核对设置。")
            return operation(live)

    def context(profile):
        display = ""
        if profile.get("next_run_at"):
            display = datetime.fromisoformat(profile["next_run_at"]).astimezone(ZoneInfo(profile["timezone"])).strftime("%Y-%m-%d %H:%M")
        return {"preferences": profile, "next_personal_run": display}

    @app.get("/preferences/streak")
    async def page(request: Request):
        current = principal(request)
        if not current:
            return RedirectResponse("/login", status_code=303)
        try:
            profile = await asyncio.to_thread(with_current_session, request, current, personal_streak.preferences)
        except PermissionError:
            return RedirectResponse("/login", status_code=303)
        return render(request, "personal_streak.html", {**context(profile), "flash": request.session.pop("flash", None)})

    @app.post("/preferences/streak")
    async def save(request: Request):
        current = principal(request)
        if not current:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not csrf_valid(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "页面已更新，请刷新后重试"}, status_code=403)
        asynchronous = request.headers.get("x-requested-with") == "XMLHttpRequest"
        messages = [line.strip() for line in str(form.get("messages", "")).splitlines() if line.strip()]
        try:
            if os.getenv("SPARKFLOW_SHARED_MODE", "").lower() in {"1", "true", "yes", "on"} and "edit_revision" not in form:
                raise personal_streak.PreferenceConflict("页面版本已更新。输入已保留，请先重新打开设置页核对。")
            saved = await asyncio.to_thread(with_current_session, request, current, lambda live: personal_streak.save_preferences(
                live, enabled=form.get("enabled") == "on",
                at_time=str(form.get("time", "")), timezone_name=str(form.get("timezone", "")), messages=messages,
                expected_revision=str(form.get("edit_revision", "")) if "edit_revision" in form else None,
            ))
        except PermissionError as exc:
            if asynchronous:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        except ValueError as exc:
            if asynchronous:
                return JSONResponse({"ok": False, "error": str(exc), "category": "edit_conflict" if isinstance(exc, personal_streak.PreferenceConflict) else "validation"}, status_code=409 if isinstance(exc, personal_streak.PreferenceConflict) else 400)
            entered = {"enabled": form.get("enabled") == "on", "time": str(form.get("time", ""))[:5], "timezone": str(form.get("timezone", ""))[:64], "messages": messages[:20], "revision": str(form.get("edit_revision", ""))}
            return render(request, "personal_streak.html", {**context(entered), "flash": {"message": str(exc), "level": "error"}, "edit_conflict": isinstance(exc, personal_streak.PreferenceConflict), "unsaved_input": True}, status_code=409 if isinstance(exc, personal_streak.PreferenceConflict) else 400)
        if asynchronous:
            return JSONResponse({"ok": True, "message": "你的续火花设置已保存。", "edit_revision": saved.get("revision", ""), "next_run_display": context(saved)["next_personal_run"], "automatic_enabled": bool(saved.get("enabled"))})
        flash(request, "你的续火花设置已保存。", "success")
        return RedirectResponse("/preferences/streak", status_code=303)
