"""Desktop engine-selection lifecycle regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_checked_selection_survives_profile_transition_without_reresolving(monkeypatch, tmp_path):
    """A checked request keeps its immutable engine across the stop transition."""
    async def scenario():
        import login_desktop_server as desktop
        from core.browser_engine import BrowserEngineSelection

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        fingerprint = BrowserEngineSelection("fingerprint", "/verified/chrome", 123, "a" * 64)
        resolves = []

        def resolve(profile_name):
            resolves.append(profile_name)
            return fingerprint

        manager = desktop.LoginDesktopManager()
        manager.context = SimpleNamespace(_impl_obj=SimpleNamespace(_closing_or_closed=False))
        manager._select_network_route = AsyncMock()
        manager._ensure_login_form_locked = AsyncMock(
            side_effect=lambda: {
                "url": "https://fixture.test/",
                "state": "login_form",
                "logged_in": False,
                "reset": False,
            }
        )

        async def stop(*_args, **_kwargs):
            manager.context = None
            manager._return_to_staging()

        manager.stop = stop
        monkeypatch.setattr(desktop, "resolve_browser_engine", resolve)

        await manager.open_login(
            account_profile={"unique_id": "42", "account_ref": "record-a", "login_generation": "g"},
            engine_expectation=fingerprint.expectation(),
        )

        assert resolves == ["uid-42"]
        assert manager._engine_selection == fingerprint

    asyncio.run(scenario())


def test_prelaunch_prepare_failure_releases_guard_but_keeps_pending_fence(monkeypatch, tmp_path):
    """Preparation is inside launch cleanup, without deleting the sender fence."""
    async def scenario():
        import login_desktop_server as desktop
        from core import login_profile
        from core.browser_engine import BrowserEngineSelection

        monkeypatch.setenv("LOGIN_ACCOUNT_PROFILE_ROOT", str(tmp_path / "profiles"))
        monkeypatch.setenv("LOGIN_ACCOUNT_LOCK_ROOT", str(tmp_path / "locks"))
        profile = login_profile.resolve_account_profile(
            {"unique_id": "42", "account_ref": "record-a", "login_generation": "g"}
        )
        manager = desktop.LoginDesktopManager()
        manager._activate_account_profile(profile)
        manager._engine_selection = BrowserEngineSelection("stock")
        monkeypatch.setattr(
            desktop,
            "prepare_profile_engine",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("prepare failed")),
        )

        with pytest.raises(RuntimeError, match="prepare failed"):
            await manager.start()

        assert manager._account_guard is None
        assert login_profile.read_account_profile_pending(profile.profile_dir)["account_ref"] == "record-a"
        guard = login_profile.acquire_account_profile_guard(profile)
        guard.release()

    asyncio.run(scenario())
