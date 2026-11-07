"""Launcher integration contract for the optional browser engine."""

import asyncio
from unittest.mock import AsyncMock


class _Selection:
    name = "fingerprint"

    def launch_overrides(self):
        return {
            "executable_path": "/verified/fingerprint/chrome",
            "args": ["--fingerprint=123"],
        }


class _Chromium:
    def __init__(self, events):
        self.events = events
        self.launch = AsyncMock(side_effect=self._launch)
        self.launch_persistent_context = AsyncMock(side_effect=self._persistent)

    async def _launch(self, **options):
        self.events.append(("launch", options))
        return object()

    async def _persistent(self, profile_dir, **options):
        self.events.append(("persistent", profile_dir, options))
        return object()


class _Playwright:
    def __init__(self, events):
        self.chromium = _Chromium(events)
        self.stop = AsyncMock()


class _Starter:
    def __init__(self, playwright, events):
        self.playwright = playwright
        self.events = events

    async def start(self):
        self.events.append(("playwright-start",))
        return self.playwright


def test_get_browser_resolves_the_account_engine_once_and_merges_only_engine_options(monkeypatch):
    from core import browser

    events = []
    playwright = _Playwright(events)
    selection = _Selection()
    resolve = lambda profile_name: events.append(("resolve", profile_name)) or selection
    monkeypatch.setattr(browser, "resolve_browser_engine", resolve)
    monkeypatch.setattr(browser, "async_playwright", lambda: _Starter(playwright, events))

    _runtime, launched = asyncio.run(browser.get_browser(profile_name="uid-42"))

    assert launched is not None
    assert events[0] == ("resolve", "uid-42")
    assert [event[0] for event in events].count("resolve") == 1
    options = playwright.chromium.launch.await_args.kwargs
    assert options["executable_path"] == "/verified/fingerprint/chrome"
    assert options["args"][-1] == "--fingerprint=123"
    assert "--no-sandbox" in options["args"]


def test_persistent_launcher_prepares_the_selected_engine_before_playwright_start(monkeypatch, tmp_path):
    from core import browser

    events = []
    playwright = _Playwright(events)
    selection = _Selection()
    monkeypatch.setattr(browser, "resolve_browser_engine", lambda profile_name: events.append(("resolve", profile_name)) or selection)

    def prepare(profile_dir, actual_selection):
        events.append(("prepare", profile_dir, actual_selection))
        assert not profile_dir.exists()
        profile_dir.mkdir(parents=True)

    monkeypatch.setattr(browser, "prepare_profile_engine", prepare)
    monkeypatch.setattr(browser, "async_playwright", lambda: _Starter(playwright, events))

    _runtime, context, profile_dir = asyncio.run(
        browser.get_persistent_browser_context("uid-42", root=str(tmp_path))
    )

    assert context is not None
    assert profile_dir == tmp_path / "uid-42"
    assert [event[0] for event in events[:3]] == ["resolve", "prepare", "playwright-start"]
    options = playwright.chromium.launch_persistent_context.await_args.kwargs
    assert options["executable_path"] == "/verified/fingerprint/chrome"
    assert options["args"][-1] == "--fingerprint=123"
    assert options["viewport"] == {"width": 1600, "height": 1000}


def test_network_preflight_propagates_the_known_account_profile_to_its_launcher(monkeypatch):
    from core import browser

    calls = []

    class Page:
        async def goto(self, *_args, **_kwargs):
            return type("Response", (), {"status": 200})()

        async def close(self):
            return None

    class Browser:
        async def new_page(self):
            return Page()

        async def close(self):
            return None

    class Runtime:
        async def stop(self):
            return None

    async def get_browser(**kwargs):
        calls.append(kwargs)
        return Runtime(), Browser()

    monkeypatch.setattr(browser, "get_browser", get_browser)
    monkeypatch.setattr(browser, "douyin_network_modes", lambda: ("direct",))

    assert asyncio.run(browser.select_douyin_network_mode("https://example.test", profile_name="uid-42")) == "direct"
    assert calls == [{"network_mode": "direct", "profile_name": "uid-42"}]
