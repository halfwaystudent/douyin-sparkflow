"""A cancelled/wrong-account re-login must not authorize sender profile adoption."""
import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core import login_profile, profile_cookies


def account(gen='old'):
    return {'account_ref':'record-a','unique_id':'42','login_generation':gen,
            'browser_cookie_import_generation':gen,
            'cookies':[{'name':'sessionid','value':'verified-A','domain':'.douyin.com','path':'/'}]}


def test_begin_account_login_persists_identity_fence_before_launch(monkeypatch,tmp_path):
    monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT',str(tmp_path/'profiles'))
    monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT',str(tmp_path/'locks'))
    p=login_profile.resolve_account_profile({'unique_id':'42','account_ref':'record-a','login_generation':'old'})
    login_profile.ensure_account_profile_directory(p)
    login_profile.mark_account_profile_pending(p)
    pending=login_profile.read_account_profile_pending(p.profile_dir)
    assert pending['account_ref']=='record-a'
    assert pending['unique_id']=='42'
    assert pending['login_generation']=='old'


def test_unsaved_profile_is_not_adopted_under_old_generation(tmp_path):
    async def check():
        saved=account(); ctx=Mock(cookies=AsyncMock(return_value=[{'name':'sessionid','value':'wrong-B','domain':'.douyin.com','path':'/'}]),add_cookies=AsyncMock(),clear_cookies=AsyncMock())
        (tmp_path/'.sparkflow-cookie-session.json').write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
        (tmp_path/'.sparkflow-login-pending.json').write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
        with patch.object(profile_cookies,'get_userData',return_value=[saved]):
            with pytest.raises(profile_cookies.ProfileCookieSyncError,match='重新登录|未保存|pending|uncommitted'):
                await profile_cookies.prepare_profile_cookies(ctx,saved,{'syncStoredCookiesBeforeRun':True},tmp_path)
        ctx.add_cookies.assert_not_awaited()
    asyncio.run(check())


def test_verified_new_generation_forces_cookie_import_and_clears_fence(tmp_path):
    async def check():
        saved=account('new'); ctx=Mock(cookies=AsyncMock(return_value=[{'name':'sessionid','value':'wrong-B','domain':'.douyin.com','path':'/'}]),add_cookies=AsyncMock(),clear_cookies=AsyncMock())
        (tmp_path/'.sparkflow-cookie-session.json').write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
        fence=tmp_path/'.sparkflow-login-pending.json'
        fence.write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
        with patch.object(profile_cookies,'get_userData',return_value=deepcopy([saved])):
            result=await profile_cookies.prepare_profile_cookies(ctx,saved,{'syncStoredCookiesBeforeRun':True},tmp_path)
        assert result.imported is True
        ctx.add_cookies.assert_awaited_once_with(saved['cookies'])
        assert not fence.exists(), 'Only a successful verified new-generation import clears the fence'
    asyncio.run(check())


def test_pending_login_blocks_cookie_publication_from_an_old_session(tmp_path):
    async def check():
        saved = account()
        ctx = Mock(cookies=AsyncMock(return_value=[{'name':'sessionid','value':'wrong-B','domain':'.douyin.com','path':'/'}]))
        (tmp_path/'.sparkflow-login-pending.json').write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
        session = profile_cookies.Session(saved, tmp_path, saved['cookies'])
        with patch.object(profile_cookies, 'update_user_data') as update:
            result = await profile_cookies.publish_profile_cookies(ctx, session)
        assert result is False
        update.assert_not_called()
    asyncio.run(check())


@pytest.mark.parametrize('kind', ['malformed', 'foreign', 'symlink'])
def test_untrusted_pending_marker_never_imports_or_publishes(tmp_path, kind):
    async def check():
        saved = account('new')
        fence = tmp_path/'.sparkflow-login-pending.json'
        if kind == 'malformed':
            fence.write_text('{broken')
        elif kind == 'foreign':
            fence.write_text(json.dumps({'version':1,'account_ref':'other-record','unique_id':'99','login_generation':'old'}))
        else:
            outside = tmp_path/'outside.json'
            outside.write_text(json.dumps({'version':1,'account_ref':'record-a','unique_id':'42','login_generation':'old'}))
            fence.symlink_to(outside)
        ctx = Mock(cookies=AsyncMock(return_value=saved['cookies']), add_cookies=AsyncMock(), clear_cookies=AsyncMock())
        with patch.object(profile_cookies,'get_userData',return_value=[saved]):
            with pytest.raises(profile_cookies.ProfileCookieSyncError):
                await profile_cookies.prepare_profile_cookies(ctx,saved,{'syncStoredCookiesBeforeRun':True},tmp_path)
        ctx.add_cookies.assert_not_awaited()
        with patch.object(profile_cookies,'update_user_data') as update:
            assert await profile_cookies.publish_profile_cookies(ctx,profile_cookies.Session(saved,tmp_path,saved['cookies'])) is False
        update.assert_not_called()
        assert fence.exists()
    asyncio.run(check())
