"""Synthetic coverage for account-scoped login browser profiles only."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from filelock import FileLock
from fastapi.testclient import TestClient


@pytest.fixture
def profile_module():
    from core import login_profile

    return login_profile


def test_account_profile_uses_sender_compatible_uid_directory(profile_module, monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))

    profile = profile_module.resolve_account_profile(
        {"unique_id": "  Creator.Name_A  ", "account_ref": "record-1", "login_generation": ""}
    )

    assert profile.unique_id == "Creator.Name_A"
    assert profile.profile_name == "uid-Creator.Name_A"
    assert profile.profile_dir == tmp_path / "profiles" / "uid-Creator.Name_A"
    assert profile.guard_path == tmp_path / "locks" / "Creator_Name_A.lock.guard"


@pytest.mark.parametrize(
    "selector",
    [
        None,
        {},
        {"unique_id": "", "account_ref": "record-1", "login_generation": ""},
        {"unique_id": "has/slash", "account_ref": "record-1", "login_generation": ""},
        {"unique_id": "é", "account_ref": "record-1", "login_generation": ""},
        {"unique_id": "x" * 77, "account_ref": "record-1", "login_generation": ""},
        {"unique_id": "valid", "account_ref": "", "login_generation": ""},
        {"unique_id": "valid", "account_ref": "record-1", "login_generation": 4},
    ],
)
def test_account_profile_rejects_invalid_or_incomplete_selector(profile_module, selector):
    with pytest.raises(profile_module.AccountProfileError):
        profile_module.resolve_account_profile(selector)


def test_account_profile_guard_fails_fast_when_sender_guard_is_held(profile_module, monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
    profile = profile_module.resolve_account_profile(
        {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
    )
    profile.guard_path.parent.mkdir(parents=True)
    sender_guard = FileLock(str(profile.guard_path), timeout=0)
    sender_guard.acquire()
    try:
        with pytest.raises(profile_module.AccountProfileBusy):
            profile_module.acquire_account_profile_guard(profile)
    finally:
        sender_guard.release()



def test_account_profile_rejects_foreign_cookie_session_marker(profile_module, monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
    profile = profile_module.resolve_account_profile(
        {"unique_id": "42", "account_ref": "record-a", "login_generation": "g1"}
    )
    profile.profile_dir.mkdir(parents=True)
    (profile.profile_dir / ".sparkflow-cookie-session.json").write_text(
        json.dumps({"version": 1, "account_ref": "record-b", "unique_id": "42", "login_generation": "g1"}),
        encoding="utf-8",
    )

    with pytest.raises(profile_module.AccountProfileError, match="marker"):
        profile_module.ensure_account_profile_directory(profile)


def test_account_profile_keeps_matching_cookie_session_marker(profile_module, monkeypatch, tmp_path):
    import json

    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
    profile = profile_module.resolve_account_profile(
        {"unique_id": "42", "account_ref": "record-a", "login_generation": "g1"}
    )
    profile.profile_dir.mkdir(parents=True)
    marker = profile.profile_dir / ".sparkflow-cookie-session.json"
    original = {"version": 1, "account_ref": "record-a", "unique_id": "42", "login_generation": "g0"}
    marker.write_text(json.dumps(original), encoding="utf-8")

    profile_module.ensure_account_profile_directory(profile)

    assert json.loads(marker.read_text(encoding="utf-8")) == original


def test_account_profile_rejects_a_symlinked_profile_path(profile_module, monkeypatch, tmp_path):
    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
    profile = profile_module.resolve_account_profile(
        {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
    )
    profile.profile_dir.parent.mkdir(parents=True)
    profile.profile_dir.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(profile_module.AccountProfileError):
        profile_module.ensure_account_profile_directory(profile)


def test_open_login_endpoint_forwards_server_bound_account_selector():
    import login_desktop_server as desktop

    selector = {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
    response_payload = {"ok": True, "profile_mode": "account", "reusable_session": True}
    with patch.object(desktop.manager, "open_login", new=AsyncMock(return_value=response_payload)) as open_login:
        response = TestClient(desktop.app).post("/open-login", json={"account_profile": selector})

    assert response.status_code == 200
    assert response.json() == response_payload
    open_login.assert_awaited_once_with(account_profile=selector)


def test_open_account_profile_returns_reusable_session_without_cookie_reset(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        manager = desktop.LoginDesktopManager()
        context = object()
        page = type("Page", (), {"url": "https://creator.douyin.com/creator-micro/", "is_closed": lambda self: False, "context": context})()
        manager.context = context
        manager.page = page
        manager._active_profile_mode = "account"
        manager._active_profile_dir = tmp_path / "profiles" / "uid-42"
        manager._account_selector = ("42", "record-1", "g1")
        manager._get_active_page = AsyncMock(return_value=page)
        manager.interaction.bind = AsyncMock()
        manager._clear_login_cookies = AsyncMock()
        manager._goto_login_page = AsyncMock()

        result = await manager.open_login(
            interaction_key="ticket-" + "a" * 32,
            account_profile={"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"},
        )

        assert result["profile_mode"] == "account"
        assert result["state"] == "logged_in"
        assert result["reusable_session"] is True
        manager._clear_login_cookies.assert_not_awaited()
        manager._goto_login_page.assert_not_awaited()

    asyncio.run(scenario())


def test_selector_change_under_one_interaction_is_rejected(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        manager = desktop.LoginDesktopManager()
        context = object()
        page = type("Page", (), {"url": "https://creator.douyin.com/", "is_closed": lambda self: False, "context": context})()
        key = "ticket-" + "a" * 32
        manager.context = context
        manager.page = page
        manager._active_profile_mode = "account"
        manager._account_selector = ("42", "record-1", "g1")
        manager.interaction.context = context
        manager.interaction.token = key

        with pytest.raises(RuntimeError, match="profile selector changed"):
            await manager.open_login(
                interaction_key=key,
                account_profile={"unique_id": "43", "account_ref": "record-2", "login_generation": "g1"},
            )

    asyncio.run(scenario())


def test_account_stop_never_purges_profile_and_releases_guard_after_clean_close(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop
        from core import login_profile

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        profile = login_profile.resolve_account_profile(
            {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
        )
        profile.profile_dir.mkdir(parents=True)
        marker = profile.profile_dir / "Default" / "Local Storage" / "keep"
        marker.parent.mkdir(parents=True)
        marker.write_text("persist", encoding="utf-8")
        manager = desktop.LoginDesktopManager()
        manager._active_profile_mode = "account"
        manager._active_profile_dir = profile.profile_dir
        manager._account_guard = login_profile.acquire_account_profile_guard(profile)

        await manager.stop(clear_profile=True)

        assert marker.read_text(encoding="utf-8") == "persist"
        assert manager._account_guard is None

    asyncio.run(scenario())



def test_status_after_account_context_closes_returns_to_staging(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop
        from core import login_profile

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        profile = login_profile.resolve_account_profile(
            {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
        )
        profile.profile_dir.mkdir(parents=True)
        manager = desktop.LoginDesktopManager()
        manager._active_profile_mode = "account"
        manager._active_profile_dir = profile.profile_dir
        manager._account_guard = login_profile.acquire_account_profile_guard(profile)
        manager._status_cache = {"profile_mode": "account"}
        manager._status_checked_at = desktop.time.monotonic()

        payload = await manager.status()

        assert payload["profile_mode"] == "staging"
        assert manager._account_guard is None

    asyncio.run(scenario())


def test_account_close_failure_retains_guard(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop
        from core import login_profile

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        profile = login_profile.resolve_account_profile(
            {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
        )
        profile.profile_dir.mkdir(parents=True)

        class BrokenContext:
            async def close(self):
                raise RuntimeError("cannot close")

        manager = desktop.LoginDesktopManager()
        manager._active_profile_mode = "account"
        manager._active_profile_dir = profile.profile_dir
        manager.context = BrokenContext()
        manager._account_guard = login_profile.acquire_account_profile_guard(profile)

        with pytest.raises(RuntimeError, match="cannot close"):
            await manager.stop()

        assert manager._account_guard is not None
        with pytest.raises(login_profile.AccountProfileBusy):
            login_profile.acquire_account_profile_guard(profile)
        manager._account_guard.release()

    asyncio.run(scenario())


def test_cancelled_account_start_releases_guard_when_browser_never_started(monkeypatch, tmp_path):
    async def scenario():
        import login_desktop_server as desktop
        from core import login_profile

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        profile = login_profile.resolve_account_profile(
            {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
        )
        manager = desktop.LoginDesktopManager()
        manager._activate_account_profile(profile)
        manager._select_network_route = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await manager.start()

        assert manager._account_guard is None
        guard = login_profile.acquire_account_profile_guard(profile)
        guard.release()

    asyncio.run(scenario())


@pytest.mark.skipif(not __import__("importlib").util.find_spec("playwright"), reason="playwright unavailable")
def test_synthetic_creator_storage_survives_persistent_profile_reopen(profile_module, monkeypatch, tmp_path):
    """No external request: creator.douyin.com is fulfilled with local HTML."""
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
    monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
    profile = profile_module.resolve_account_profile(
        {"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"}
    )
    profile_module.ensure_account_profile_directory(profile)
    html = "<script>localStorage.setItem('continuity', 'kept');const request=indexedDB.open('continuity-db', 1);request.onupgradeneeded=()=>request.result.createObjectStore('state');request.onsuccess=()=>{request.result.close();window.indexedDbReady=true}</script>"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile.profile_dir), headless=True)
        context.route("https://creator.douyin.com/**", lambda route: route.fulfill(status=200, body=html, content_type="text/html"))
        page = context.new_page()
        page.goto("https://creator.douyin.com/")
        page.wait_for_function("window.indexedDbReady === true")
        context.close()

        reopened = playwright.chromium.launch_persistent_context(str(profile.profile_dir), headless=True)
        reopened.route("https://creator.douyin.com/**", lambda route: route.fulfill(status=200, body="<title>local</title>", content_type="text/html"))
        page = reopened.new_page()
        page.goto("https://creator.douyin.com/")
        assert page.evaluate("localStorage.getItem('continuity')") == "kept"
        assert page.evaluate("indexedDB.databases().then(items => items.some(item => item.name === 'continuity-db'))")
        reopened.close()
