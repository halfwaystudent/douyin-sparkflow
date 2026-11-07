"""Additional profile safety seams exercised without opening a consumer site."""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from filelock import FileLock, Timeout
import pytest

from core import browser, login_profile
import login_desktop_server as desktop


def selector(uid='Creator.Name'):
    return {'unique_id': uid, 'account_ref': 'record-a', 'login_generation': 'gen-a'}


def test_guard_path_matches_actual_sender_normalization(monkeypatch, tmp_path):
    monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT', str(tmp_path/'profiles'))
    monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT', str(tmp_path/'locks'))
    p = login_profile.resolve_account_profile(selector())
    expected = tmp_path/'locks'/(browser.browser_account_lock_name(selector())+'.lock.guard')
    assert p.guard_path == expected


def test_status_read_error_must_not_release_a_live_account_guard(monkeypatch, tmp_path):
    async def check():
        monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT', str(tmp_path/'profiles'))
        monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT', str(tmp_path/'locks'))
        p = login_profile.resolve_account_profile(selector('42'))
        manager = desktop.LoginDesktopManager()
        manager._activate_account_profile(p)
        context = Mock()
        context.close = AsyncMock(side_effect=RuntimeError('close unproved'))
        manager.context = context
        manager.page = Mock()
        manager.page.is_closed.side_effect = RuntimeError('transient page inspection error')
        manager.playwright = Mock(stop=AsyncMock(side_effect=RuntimeError('driver still busy')))
        # The real browser implementation says this context is still live.
        with patch.object(manager, '_context_is_closed', return_value=False):
            try:
                try:
                    await manager.status()
                except RuntimeError:
                    pass
                assert manager._account_guard is not None, 'status inspection released a still-live profile'
                assert manager._active_profile_mode == 'account'
                probe = FileLock(str(p.guard_path), timeout=0)
                with pytest.raises(Timeout):
                    probe.acquire()
            finally:
                # Synthetic context only: explicit test cleanup after the assertion.
                manager._release_account_guard()
    asyncio.run(check())


def test_closing_context_is_not_proof_of_closed_account_profile(monkeypatch, tmp_path):
    async def check():
        monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT', str(tmp_path/'profiles'))
        monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT', str(tmp_path/'locks'))
        p = login_profile.resolve_account_profile(selector('42'))
        manager = desktop.LoginDesktopManager()
        manager._activate_account_profile(p)
        closed = asyncio.get_running_loop().create_future()
        context = Mock()
        context._impl_obj = Mock(_closing_or_closed=True, _closed_future=closed)
        manager.context = context
        try:
            assert manager._context_is_closed() is False
            assert manager.context is context
            assert manager._account_guard is not None
        finally:
            manager._release_account_guard()
    asyncio.run(check())


def test_explicit_rescan_clears_only_login_cookies_before_qr(monkeypatch, tmp_path):
    async def check():
        manager = desktop.LoginDesktopManager()
        manager._active_profile_mode = 'account'
        page = Mock(url='https://creator.douyin.com/')
        manager.context = Mock()
        manager.page = page
        manager._get_active_page = AsyncMock(return_value=page)
        manager._clear_login_cookies = AsyncMock(return_value=1)
        manager._qr_payload = AsyncMock(return_value={'qr_ready':True,'logged_in':False,'state':'qr_ready','reset':False,'url':page.url})
        with patch.object(desktop, '_purge_login_data') as purge:
            result = await manager._refresh_login_qr_locked()
        manager._clear_login_cookies.assert_awaited_once_with(page, manager.context)
        manager._qr_payload.assert_awaited_once_with(page)
        purge.assert_not_called()
        assert result['qr_ready'] and result['reset']
    asyncio.run(check())

@pytest.mark.parametrize('root_kind', ['LOGIN_ACCOUNT_PROFILE_ROOT', 'LOGIN_ACCOUNT_LOCK_ROOT'])
def test_symlinked_root_ancestor_is_rejected_before_directory_creation(monkeypatch, tmp_path, root_kind):
    destination = tmp_path/'destination'
    destination.mkdir()
    link = tmp_path/'ancestor'
    link.symlink_to(destination, target_is_directory=True)
    monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT',str(tmp_path/'profiles'))
    monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT',str(tmp_path/'locks'))
    monkeypatch.setenv(root_kind,str(link/'redirected'))
    p = login_profile.resolve_account_profile(selector('42'))
    operation = login_profile.ensure_account_profile_directory if root_kind.endswith('PROFILE_ROOT') else login_profile.acquire_account_profile_guard
    acquired = None
    try:
        with pytest.raises(login_profile.AccountProfileError):
            acquired = operation(p)
    finally:
        if isinstance(acquired, FileLock):
            acquired.release()
    assert not (destination/'redirected').exists()
