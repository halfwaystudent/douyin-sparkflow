"""Private WebUI/login-desktop engine-expectation handshake contract."""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, patch
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from core.browser_engine import BrowserEngineError, BrowserEngineSelection


class _Selection:
    name = "fingerprint"

    def expectation(self):
        return {
            "engine": "fingerprint",
            "seed": 123,
            "executable_sha256": "a" * 64,
        }

    def launch_overrides(self):
        return {"executable_path": "/server/only/chrome", "args": ["--fingerprint=123"]}


def test_desktop_open_login_api_accepts_and_forwards_only_the_optional_expectation():
    import login_desktop_server as desktop

    expectation = {"engine": "fingerprint", "seed": 123, "executable_sha256": "a" * 64}
    with patch.object(desktop.manager, "open_login", new=AsyncMock(return_value={"ok": True})) as open_login:
        response = TestClient(desktop.app).post("/open-login", json={"engine_expectation": expectation})

    assert response.status_code == 200
    open_login.assert_awaited_once_with(engine_expectation=expectation)


def test_desktop_checks_fingerprint_expectation_before_network_or_browser(monkeypatch):
    import login_desktop_server as desktop

    async def scenario():
        manager = desktop.LoginDesktopManager()
        selection = _Selection()
        monkeypatch.setattr(desktop, "resolve_browser_engine", lambda profile_name: selection)
        monkeypatch.setattr(desktop, "check_engine_expectation", lambda _selection, _expectation: (_ for _ in ()).throw(desktop.BrowserEngineError("engine expectation mismatch")))
        manager._select_network_route = AsyncMock()
        manager.start = AsyncMock()

        try:
            await manager.open_login(
                account_profile={"unique_id": "42", "account_ref": "record-1", "login_generation": "g1"},
                engine_expectation={"engine": "fingerprint", "seed": 999, "executable_sha256": "a" * 64},
            )
        except desktop.BrowserEngineError as exc:
            assert "mismatch" in str(exc)
        else:
            raise AssertionError("foreign expectation was accepted")
        manager._select_network_route.assert_not_awaited()
        manager.start.assert_not_awaited()

    asyncio.run(scenario())


def test_webui_gate_on_generates_server_expectation_without_private_engine_fields(monkeypatch):
    from tests import test_shared_security as fixtures
    from utils import config
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        client = fixture.login()
        csrf = fixture.token(client)
        accounts = config.get_userData(force_reload=True)
        accounts[0].update({"unique_id": "111", "login_generation": "generation-a", "enabled": True})
        config.save_userData(accounts)
        monkeypatch.setenv("SPARKFLOW_LOGIN_PROFILE_CONTINUITY", "1")
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: True)
        resolved = []
        monkeypatch.setattr(app_module, "resolve_browser_engine", lambda profile_name: resolved.append(profile_name) or _Selection())

        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "account"}) as call:
            response = client.post("/login-desktop/open", data={
                "csrf_token": csrf,
                "mode": "relogin",
                "relogin_unique_id": "111",
                "relogin_account_ref": "a1",
                "login_generation": "generation-a",
            })

        assert response.status_code == 200, response.text
        payload = call.call_args.kwargs["payload"]
        assert resolved == ["uid-111"]
        assert payload["engine_expectation"] == _Selection().expectation()
        assert "executable_path" not in payload
        assert "seed" not in payload
        assert "engine" not in payload
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()


def test_webui_gate_off_does_not_add_an_expectation_to_the_legacy_payload(monkeypatch):
    from tests import test_shared_security as fixtures
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        client = fixture.login()
        csrf = fixture.token(client)
        monkeypatch.delenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", raising=False)
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: False)
        monkeypatch.setattr(app_module, "resolve_browser_engine", lambda _name: (_ for _ in ()).throw(AssertionError("gate-off must not resolve engine")))

        with patch.object(app_module, "call_login_desktop", return_value={"ok": True, "profile_mode": "staging"}) as call:
            response = client.post("/login-desktop/open", data={"csrf_token": csrf, "mode": "add"})

        assert response.status_code == 200, response.text
        assert call.call_args.kwargs["payload"] == {"interaction_key": response.json()["workspace"]["ticket"]}
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()


def test_webui_gate_on_rejects_existing_account_relogin_without_profile_continuity(monkeypatch):
    from tests import test_shared_security as fixtures
    from utils import config
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        client = fixture.login()
        csrf = fixture.token(client)
        accounts = config.get_userData(force_reload=True)
        accounts[0].update({"unique_id": "111", "login_generation": "generation-a", "enabled": True})
        config.save_userData(accounts)
        monkeypatch.delenv("SPARKFLOW_LOGIN_PROFILE_CONTINUITY", raising=False)
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: True)

        with patch.object(app_module, "call_login_desktop") as call:
            response = client.post("/login-desktop/open", data={
                "csrf_token": csrf,
                "mode": "relogin",
                "relogin_unique_id": "111",
                "relogin_account_ref": "a1",
                "login_generation": "generation-a",
            })

        assert response.status_code == 503
        assert "浏览器环境" in response.json()["error"]
        call.assert_not_called()
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()


def test_desktop_rejects_web_on_expectation_when_desktop_is_off(monkeypatch):
    """A fingerprint expectation cannot be accepted by a stock desktop."""
    import login_desktop_server as desktop

    async def scenario():
        manager = desktop.LoginDesktopManager()
        stock = BrowserEngineSelection("stock")
        monkeypatch.setattr(desktop, "resolve_browser_engine", lambda _name: stock)
        manager._select_network_route = AsyncMock()

        with pytest.raises(BrowserEngineError, match="expectation"):
            await manager.open_login(
                account_profile={"unique_id": "42", "account_ref": "a1", "login_generation": "g"},
                engine_expectation=_Selection().expectation(),
            )
        manager._select_network_route.assert_not_awaited()

    asyncio.run(scenario())


def test_desktop_rejects_missing_web_expectation_when_desktop_is_on(monkeypatch):
    """A fingerprint desktop refuses an old web payload with no handshake."""
    import login_desktop_server as desktop

    async def scenario():
        manager = desktop.LoginDesktopManager()
        fingerprint = BrowserEngineSelection("fingerprint", "/verified/chrome", 123, "a" * 64)
        monkeypatch.setattr(desktop, "resolve_browser_engine", lambda _name: fingerprint)
        manager._select_network_route = AsyncMock()

        with pytest.raises(BrowserEngineError, match="expectation"):
            await manager.open_login(
                account_profile={"unique_id": "42", "account_ref": "a1", "login_generation": "g"},
                engine_expectation=None,
            )
        manager._select_network_route.assert_not_awaited()

    asyncio.run(scenario())


def test_webui_initial_handshake_mismatch_releases_lease(monkeypatch):
    """An initial web/desktop gate mismatch fails closed and releases its lease."""
    from tests import test_shared_security as fixtures
    from utils import config
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        client = fixture.login()
        csrf = fixture.token(client)
        accounts = config.get_userData(force_reload=True)
        accounts[0].update({"unique_id": "111", "login_generation": "generation-a", "enabled": True})
        config.save_userData(accounts)
        monkeypatch.setenv("SPARKFLOW_LOGIN_PROFILE_CONTINUITY", "1")
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: True)
        monkeypatch.setattr(app_module, "resolve_browser_engine", lambda _name: _Selection())

        def old_desktop(path, **kwargs):
            if path == "/close":
                return {"ok": True}
            assert kwargs["payload"]["engine_expectation"] == _Selection().expectation()
            raise RuntimeError("engine expectation mismatch")

        with patch.object(app_module, "call_login_desktop", side_effect=old_desktop):
            response = client.post("/login-desktop/open", data={
                "csrf_token": csrf,
                "mode": "relogin",
                "relogin_unique_id": "111",
                "relogin_account_ref": "a1",
                "login_generation": "generation-a",
            })

        assert response.status_code == 503
        assert login_lock.get_lock() is None
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()


def test_webui_initial_old_payload_is_rejected_by_fingerprint_desktop(monkeypatch):
    """The reverse mismatch (web off, desktop on) also releases the lease."""
    from tests import test_shared_security as fixtures
    from utils import config
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        client = fixture.login()
        csrf = fixture.token(client)
        accounts = config.get_userData(force_reload=True)
        accounts[0].update({"unique_id": "111", "login_generation": "generation-a", "enabled": True})
        config.save_userData(accounts)
        monkeypatch.setenv("SPARKFLOW_LOGIN_PROFILE_CONTINUITY", "1")
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: False)
        monkeypatch.setattr(app_module, "resolve_browser_engine", lambda _name: (_ for _ in ()).throw(AssertionError("web gate is off")))

        def new_desktop(path, **kwargs):
            if path == "/close":
                return {"ok": True}
            assert "engine_expectation" not in kwargs["payload"]
            raise RuntimeError("engine expectation mismatch")

        with patch.object(app_module, "call_login_desktop", side_effect=new_desktop):
            response = client.post("/login-desktop/open", data={
                "csrf_token": csrf,
                "mode": "relogin",
                "relogin_unique_id": "111",
                "relogin_account_ref": "a1",
                "login_generation": "generation-a",
            })

        assert response.status_code == 503
        assert login_lock.get_lock() is None
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()


def test_webui_queued_promotion_injects_expectation_and_cleans_up_mismatch(monkeypatch):
    """Queued promotion uses the same handshake and closes a rejected desktop."""
    from tests import test_shared_security as fixtures
    from utils import config
    from webui import app as app_module, login_lock

    fixture = fixtures.SharedConsoleTests()
    fixture.setUp()
    directory = tempfile.TemporaryDirectory()
    lock_patch = patch.object(login_lock, "LOCK_PATH", Path(directory.name) / "workspace.json")
    lock_patch.start()
    try:
        owner = fixture.login()
        owner_csrf = fixture.token(owner)
        bob = fixture.login("bob", "Second-secret-2026!")
        bob_csrf = fixture.token(bob)
        accounts = config.get_userData(force_reload=True)
        accounts[0].update({"unique_id": "111", "login_generation": "generation-a", "enabled": True})
        accounts[1].update({"unique_id": "222", "login_generation": "generation-b", "enabled": True})
        config.save_userData(accounts)
        monkeypatch.setenv("SPARKFLOW_LOGIN_PROFILE_CONTINUITY", "1")
        monkeypatch.setattr(app_module, "fingerprint_browser_enabled", lambda: True)
        monkeypatch.setattr(app_module, "resolve_browser_engine", lambda _name: _Selection())
        calls = []

        def desktop(path, **kwargs):
            calls.append((path, kwargs))
            if path == "/close":
                return {"ok": True}
            payload = kwargs["payload"]
            if "account_profile" not in payload:
                return {"ok": True, "profile_mode": "staging"}
            assert payload["engine_expectation"] == _Selection().expectation()
            raise RuntimeError("engine expectation mismatch")

        with patch.object(app_module, "call_login_desktop", side_effect=desktop):
            active = owner.post("/login-desktop/open", data={"csrf_token": owner_csrf, "mode": "add"})
            assert active.status_code == 200, active.text
            queued = bob.post("/login-desktop/open", data={
                "csrf_token": bob_csrf,
                "mode": "relogin",
                "relogin_unique_id": "222",
                "relogin_account_ref": "a2",
                "login_generation": "generation-b",
            })
            assert queued.status_code == 202, queued.text
            released = owner.post("/login-desktop/release", data={
                "csrf_token": owner_csrf,
                "ticket": active.json()["workspace"]["ticket"],
            })

        assert released.status_code == 200, released.text
        assert [path for path, _kwargs in calls] == ["/open-login", "/close", "/open-login", "/close"]
        promoted_payload = calls[2][1]["payload"]
        assert promoted_payload["engine_expectation"] == _Selection().expectation()
        state = login_lock.get_workspace_state()
        assert state["active"] is None
        assert state["queue"] == []
    finally:
        lock_patch.stop()
        directory.cleanup()
        fixture.doCleanups()

