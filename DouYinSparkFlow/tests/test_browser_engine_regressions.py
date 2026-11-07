"""Parent reproductions: default-off compatibility and concurrent desktop opens."""
import asyncio,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core import browser_engine as engine


def test_stock_without_binding_keeps_legacy_relative_profile_paths(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    engine.prepare_profile_engine(Path('relative-profiles')/'uid-42',engine.BrowserEngineSelection('stock'))
    assert not (tmp_path/'relative-profiles').exists()


def test_stock_without_binding_keeps_legacy_symlink_profile_roots(tmp_path):
    target=tmp_path/'real';target.mkdir();link=tmp_path/'alias';link.symlink_to(target,target_is_directory=True)
    engine.prepare_profile_engine(link/'uid-42',engine.BrowserEngineSelection('stock'))
    assert list(target.iterdir())==[]


def test_boolean_marker_schema_is_not_a_valid_engine_binding(tmp_path):
    profile=tmp_path/'profile';profile.mkdir()
    (profile/'.sparkflow-browser-engine.json').write_text(json.dumps({'schema':True,'engine':'fingerprint','seed':123,'executable_sha256':'a'*64}))
    selection=engine.BrowserEngineSelection('fingerprint','/verified/chrome',123,'a'*64)
    with pytest.raises(engine.BrowserEngineError):engine.prepare_profile_engine(profile,selection)


def test_queued_open_cannot_overwrite_the_engine_of_an_inflight_launch(monkeypatch,tmp_path):
    import login_desktop_server as desktop
    async def scenario():
        monkeypatch.setenv('LOGIN_ACCOUNT_PROFILE_ROOT',str(tmp_path/'profiles'))
        monkeypatch.setenv('LOGIN_ACCOUNT_LOCK_ROOT',str(tmp_path/'locks'))
        fingerprint=engine.BrowserEngineSelection('fingerprint','/verified/fingerprint/chrome',123,'a'*64)
        stock=engine.BrowserEngineSelection('stock')
        route_entered=asyncio.Event();release_route=asyncio.Event();b_started=asyncio.Event();launches=[]
        def resolve(name):
            if name=='uid-43':return stock
            return fingerprint
        monkeypatch.setattr(desktop,'resolve_browser_engine',resolve)
        class Context:
            def __init__(self):
                self._impl_obj=SimpleNamespace(_closed_future=asyncio.get_running_loop().create_future())
                self.pages=[SimpleNamespace(url='https://fixture.test/',is_closed=lambda:False,emulate_media=AsyncMock())]
            def on(self,*args):pass
            async def close(self):
                if not self._impl_obj._closed_future.done():self._impl_obj._closed_future.set_result(None)
        async def launch(path,**kwargs):launches.append((Path(path).name,kwargs));return Context()
        runtime=SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(side_effect=launch)),stop=AsyncMock())
        monkeypatch.setattr(desktop,'async_playwright',lambda:SimpleNamespace(start=AsyncMock(return_value=runtime)))
        manager=desktop.LoginDesktopManager()
        async def route():
            if not route_entered.is_set():route_entered.set();await release_route.wait()
            return {'mode':'direct'}
        manager._select_network_route=route
        async def ensure():
            await manager.start()
            return {'url':'https://fixture.test/','state':'logged_in','logged_in':True,'reset':False}
        manager._ensure_login_form_locked=ensure
        a=b=None
        try:
            a=asyncio.create_task(manager.open_login(account_profile={'unique_id':'42','account_ref':'a','login_generation':'g'},engine_expectation=fingerprint.expectation()))
            await asyncio.wait_for(route_entered.wait(),2)
            async def open_b():
                b_started.set()
                return await manager.open_login(account_profile={'unique_id':'43','account_ref':'b','login_generation':'g'},engine_expectation=stock.expectation())
            b=asyncio.create_task(open_b())
            await asyncio.wait_for(b_started.wait(),2)
            release_route.set()
            await asyncio.gather(a,b,return_exceptions=True)
            first=next(options for name,options in launches if name=='uid-42')
            assert first.get('executable_path')=='/verified/fingerprint/chrome'
            assert '--fingerprint=123' in first['args']
        finally:
            release_route.set()
            for task in [a,b]:
                if task and not task.done():task.cancel()
            await asyncio.gather(*(t for t in [a,b] if t),return_exceptions=True)
            await manager.stop(clear_profile=True)
    asyncio.run(scenario())
