"""Self-service sending preferences; the form cannot name another user."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from core import personal_streak


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
        profile = await asyncio.to_thread(personal_streak.preferences, current)
        return render(request, "personal_streak.html", context(profile))

    @app.post("/preferences/streak")
    async def save(request: Request):
        current = principal(request)
        if not current:
            return RedirectResponse("/login", status_code=303)
        form = await request.form()
        if not csrf_valid(request, str(form.get("csrf_token", ""))):
            return JSONResponse({"error": "页面已更新，请刷新后重试"}, status_code=403)
        messages = [line.strip() for line in str(form.get("messages", "")).splitlines() if line.strip()]
        try:
            await asyncio.to_thread(
                personal_streak.save_preferences, current, enabled=form.get("enabled") == "on",
                at_time=str(form.get("time", "")), timezone_name=str(form.get("timezone", "")), messages=messages,
            )
        except ValueError as exc:
            flash(request, str(exc), "error")
            entered = {"enabled": form.get("enabled") == "on", "time": str(form.get("time", ""))[:5], "timezone": str(form.get("timezone", ""))[:64], "messages": messages[:20]}
            return render(request, "personal_streak.html", context(entered))
        flash(request, "你的续火花设置已保存。", "success")
        return RedirectResponse("/preferences/streak", status_code=303)
