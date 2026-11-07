"""Real Chromium integration, synthetic storage and no external networking."""
import asyncio
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from filelock import FileLock, Timeout
from playwright.async_api import async_playwright

from core import browser, profile_cookies
import login_desktop_server as desktop


def test_login_manager_to_sender_preserves_storage_and_requires_verified_save(monkeypatch, tmp_path):
    async def scenario():
        root = tmp_path/'profiles'; locks = tmp_path/'locks'
        monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT', str(root))
        monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT', str(locks))
        account = {'account_ref':'record-a','unique_id':'42','login_generation':'old',
                   'cookies':[{'name':'sessionid','value':'verified-A','domain':'.douyin.com','path':'/'}]}
        manager = desktop.LoginDesktopManager()
        manager._select_network_route = AsyncMock(return_value={'mode':'direct'})
        manager._login_page_state = AsyncMock(return_value={'logged_in':True,'url':'https://creator.douyin.com/'})
        factory = async_playwright
        async def launch_runtime():
            runtime = await factory().start()
            original = runtime.chromium.launch_persistent_context
            async def launch(*args, **kwargs):
                kwargs['headless'] = True  # only synthetic CI; no display required
                ctx = await original(*args, **kwargs)
                async def route(req):
                    if req.request.url.startswith('https://creator.douyin.com/'):
                        await req.fulfill(status=200, body='<title>synthetic fixture</title>',content_type='text/html')
                    else:
                        await req.abort()
                await ctx.route('**/*',route)
                return ctx
            runtime.chromium.launch_persistent_context = launch
            return runtime
        class Factory:
            start = staticmethod(launch_runtime)
        try:
            with patch.object(desktop,'async_playwright',return_value=Factory()):
                result = await manager.open_login(interaction_key='ticket-'+'a'*32,
                    account_profile={k:account[k] for k in ['unique_id','account_ref','login_generation']})
            assert result['profile_mode']=='account'
            await manager.context.add_cookies(account['cookies'])
            page = manager.page
            await page.evaluate("""async () => {
                localStorage.setItem('continuity','account-A');
                await new Promise((resolve,reject) => {
                    const r=indexedDB.open('profile-continuity',1);
                    r.onupgradeneeded=()=>r.result.createObjectStore('state');
                    r.onerror=()=>reject(r.error);
                    r.onsuccess=()=>{r.result.close();resolve();};
                });
            }""")
            guard_path = locks/(browser.browser_account_lock_name(account)+'.lock.guard')
            competitor = FileLock(str(guard_path),timeout=0)
            with pytest.raises(Timeout):
                competitor.acquire()
        finally:
            await manager.stop(clear_profile=True)
        directory = root/browser.account_profile_name(account)
        assert (directory/'.sparkflow-login-pending.json').is_file()
        pw=ctx=None
        try:
            pw,ctx,_ = await browser.get_persistent_browser_context(browser.account_profile_name(account),root=str(root))
            async def local_route(route):
                if route.request.url.startswith('https://creator.douyin.com/'):
                    await route.fulfill(status=200,body='<title>sender fixture</title>',content_type='text/html')
                else:
                    await route.abort()
            await ctx.route('**/*',local_route)
            with patch.object(profile_cookies,'get_userData',return_value=[deepcopy(account)]):
                with pytest.raises(profile_cookies.ProfileCookieSyncError,match='尚未保存'):
                    await profile_cookies.prepare_profile_cookies(ctx,account,{'syncStoredCookiesBeforeRun':True},directory)
            saved=deepcopy(account);saved['login_generation']='verified-new';saved['browser_cookie_import_generation']='verified-new'
            with patch.object(profile_cookies,'get_userData',return_value=[deepcopy(saved)]):
                session=await profile_cookies.prepare_profile_cookies(ctx,saved,{'syncStoredCookiesBeforeRun':True},directory)
            assert session.imported
            assert not (directory/'.sparkflow-login-pending.json').exists()
            page=await ctx.new_page();await page.goto('https://creator.douyin.com/')
            assert await page.evaluate("localStorage.getItem('continuity')")=='account-A'
            assert await page.evaluate("indexedDB.databases().then(xs=>xs.some(x=>x.name==='profile-continuity'))")
            assert any(c['value']=='verified-A' for c in await ctx.cookies() if c['name']=='sessionid')
        finally:
            if ctx: await ctx.close()
            if pw: await pw.stop()
    asyncio.run(scenario())
