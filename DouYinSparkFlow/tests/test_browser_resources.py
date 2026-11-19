"""Tests for core.browser_resources pre-launch memory gating.

All filesystem reads are faked through monkeypatched module constants and
``_read_text``; no real /proc or /sys/fs/cgroup is touched.  The launch-entry
tests patch ``core.browser.ensure_browser_resources`` and the Playwright
entrypoints so a refusal can be proven to happen *before* any browser starts.
"""

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import browser as browser_module
from core import browser_resources as br


MEMINFO_OK = textwrap.dedent(
    """\
    MemTotal:        2048000 kB
    MemFree:          512000 kB
    MemAvailable:    1572864 kB
    SwapTotal:       2097152 kB
    SwapFree:        1048576 kB
    """
)

MEMINFO_LOW = textwrap.dedent(
    """\
    MemTotal:        2048000 kB
    MemFree:           81920 kB
    MemAvailable:     122880 kB
    SwapTotal:       2097152 kB
    SwapFree:        2097152 kB
    """
)


def _write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _patch_fs(monkeypatch, tmp_path, *, meminfo=None, self_cgroup=None,
              cgroup_files=None, cgroup_root=None):
    """Point the module at fixture files under tmp_path."""
    root = cgroup_root or tmp_path / "cgroup"
    meminfo_path = _write(tmp_path / "meminfo",
                          meminfo if meminfo is not None else MEMINFO_OK)
    self_cgroup_path = _write(
        tmp_path / "self_cgroup",
        self_cgroup if self_cgroup is not None else "0::/docker/abc\n",
    )
    for rel, content in (cgroup_files or {}).items():
        _write(root / rel, content)

    def fake_read_text(path):
        path = Path(path)
        # Redirect the three well-known roots into the fixture tree.
        try:
            rel = path.relative_to(Path("/proc"))
        except ValueError:
            rel = None
        if rel == Path("meminfo"):
            return meminfo_path.read_text(encoding="utf-8")
        if rel == Path("self/cgroup"):
            return self_cgroup_path.read_text(encoding="utf-8")
        try:
            rel = path.relative_to(Path("/sys/fs/cgroup"))
        except ValueError:
            return None
        target = root / rel
        if target.is_file():
            return target.read_text(encoding="utf-8")
        return None

    monkeypatch.setattr(br, "_read_text", fake_read_text)
    monkeypatch.setattr(br, "_MEMINFO_PATH", Path("/proc/meminfo"))
    monkeypatch.setattr(br, "_SELF_CGROUP_PATH", Path("/proc/self/cgroup"))
    monkeypatch.setattr(br, "_CGROUP_ROOT", Path("/sys/fs/cgroup"))
    # Path.exists() is used by the v1-dir discovery; emulate it for fixtures.
    real_exists = Path.exists

    def fake_exists(self, *args, **kwargs):
        try:
            rel = self.relative_to(Path("/sys/fs/cgroup"))
        except ValueError:
            return real_exists(self, *args, **kwargs)
        return (root / rel).exists()

    monkeypatch.setattr(Path, "exists", fake_exists)


def _sample(**overrides):
    base = {
        "host": {"mem_total_bytes": 2 * 1024**3, "mem_available_bytes": 1536 * 1024**2,
                 "mem_free_bytes": 512 * 1024**2, "swap_total_bytes": 2 * 1024**3,
                 "swap_free_bytes": 2 * 1024**3, "read_error": None},
        "cgroups": [],
        "platform": "linux",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# ensure_browser_resources contract
# ---------------------------------------------------------------------------


def test_ensure_returns_report_when_host_has_headroom():
    report = br.ensure_browser_resources("launch", sampler=lambda: _sample())
    assert report["ok"] is True
    assert report["measured"] is True
    assert report["layer"] == "host"
    assert report["check"] == "launch"


def test_ensure_raises_browser_resource_unavailable_on_low_host_memory():
    low = _sample()
    low["host"]["mem_available_bytes"] = 64 * 1024**2  # 64 MiB << 256 MiB floor
    with pytest.raises(br.BrowserResourceUnavailable) as excinfo:
        br.ensure_browser_resources("launch", sampler=lambda: low)
    exc = excinfo.value
    assert exc.category == "browser_resource_unavailable"
    assert "browser_resource_unavailable" in str(exc)
    assert exc.details["layer"] == "host"
    assert exc.details["available_bytes"] == 64 * 1024**2


def test_unmeasurable_host_is_fail_safe_not_infinite_resource():
    """A missing Linux sample refuses; non-Linux support is tested separately."""
    report = br.browser_resource_report(
        "launch",
        sampler=lambda: {"host": {"read_error": "meminfo_unreadable"},
                         "cgroups": [], "platform": "linux"},
    )
    assert report["ok"] is False
    assert report["measured"] is False


def test_io_error_is_reported_not_swallowed_as_unlimited():
    """A real read error surfaces in the sample; it does not fake headroom."""
    sample = _sample()
    sample["host"]["read_error"] = "meminfo_unreadable"
    sample["host"]["mem_available_bytes"] = None
    sample["host"]["mem_total_bytes"] = None
    report = br.browser_resource_report("launch", sampler=lambda: sample)
    # No finite bound could be measured -> still passes, but the error is kept.
    assert report["measured"] is False
    assert report["sample"]["host"]["read_error"] == "meminfo_unreadable"


# ---------------------------------------------------------------------------
# Fixture-driven parsing (tmp_path meminfo/cgroup files)
# ---------------------------------------------------------------------------


def test_fixture_host_normal_passes_launch(monkeypatch, tmp_path):
    _patch_fs(monkeypatch, tmp_path, meminfo=MEMINFO_OK,
              self_cgroup="0::/docker/abc\n",
              cgroup_files={"docker/abc/memory.max": "max\n",
                            "docker/abc/memory.current": "1048576\n"})
    report = br.browser_resource_report("launch")
    assert report["ok"] is True
    assert report["layer"] == "host"
    # 1.5 GiB available on a 2 GiB host >> 460 MiB required.
    assert report["required_bytes"] == max(
        br.LAUNCH_MIN_AVAILABLE_BYTES, int(2 * 1024**3 * br.LAUNCH_MIN_AVAILABLE_RATIO))


def test_fixture_host_low_memory_refuses_launch(monkeypatch, tmp_path):
    _patch_fs(monkeypatch, tmp_path, meminfo=MEMINFO_LOW,
              self_cgroup="0::/\n",
              cgroup_files={"memory.max": "max\n", "memory.current": "0\n"})
    with pytest.raises(br.BrowserResourceUnavailable):
        br.ensure_browser_resources("launch")


def test_fixture_cgroup_tighter_than_host_wins(monkeypatch, tmp_path):
    """A 512 MiB cgroup with 400 MiB used must refuse even though the host
    shows plenty of MemAvailable."""
    _patch_fs(
        monkeypatch, tmp_path, meminfo=MEMINFO_OK,
        self_cgroup="0::/docker/abc\n",
        cgroup_files={
            "docker/abc/memory.max": str(512 * 1024**2) + "\n",
            "docker/abc/memory.current": str(400 * 1024**2) + "\n",
        },
    )
    report = br.browser_resource_report("launch")
    assert report["ok"] is False
    assert report["layer"] == "cgroup"
    assert report["bound_bytes"] == 512 * 1024**2
    assert report["available_bytes"] == 112 * 1024**2
    assert report["required_bytes"] == max(
        br.LAUNCH_MIN_AVAILABLE_BYTES, int(512 * 1024**2 * br.LAUNCH_MIN_AVAILABLE_RATIO))


def test_fixture_cgroup_unlimited_falls_back_to_host(monkeypatch, tmp_path):
    _patch_fs(
        monkeypatch, tmp_path, meminfo=MEMINFO_OK,
        self_cgroup="0::/docker/abc\n",
        cgroup_files={
            "docker/abc/memory.max": "max\n",
            "docker/abc/memory.current": "999999999\n",
        },
    )
    report = br.browser_resource_report("launch")
    assert report["layer"] == "host"
    assert report["ok"] is True


def test_fixture_cgroup_v1_sentinel_is_unlimited(monkeypatch, tmp_path):
    """cgroup v1 reports ~2**63-1 for no limit; must not be treated as a bound."""
    _patch_fs(
        monkeypatch, tmp_path, meminfo=MEMINFO_OK,
        self_cgroup="4:memory:/docker/abc\n",
        cgroup_files={
            "memory/docker/abc/memory.limit_in_bytes": str(2**63 - 4096) + "\n",
            "memory/docker/abc/memory.usage_in_bytes": "1048576\n",
        },
    )
    report = br.browser_resource_report("launch")
    # v1 sentinel is ignored -> host decides.
    assert report["layer"] == "host"
    assert report["ok"] is True


def test_fixture_swap_is_not_ram(monkeypatch, tmp_path):
    """Huge SwapFree must not rescue a low-MemAvailable host."""
    meminfo = textwrap.dedent(
        """\
        MemTotal:        2048000 kB
        MemFree:           32768 kB
        MemAvailable:      65536 kB
        SwapTotal:       8388608 kB
        SwapFree:        8388608 kB
        """
    )
    _patch_fs(monkeypatch, tmp_path, meminfo=meminfo,
              self_cgroup="0::/\n",
              cgroup_files={"memory.max": "max\n", "memory.current": "0\n"})
    with pytest.raises(br.BrowserResourceUnavailable):
        br.ensure_browser_resources("launch")


def test_session_check_uses_ratio_only_no_fixed_floor(monkeypatch, tmp_path):
    """In-session floor is 10% of the bound; a small-but-usable host is not
    permanently locked out once a browser is already running."""
    # Host: 1 GiB total, 150 MiB available.  Launch needs max(256MiB, 10%) =
    # 256 MiB -> refuses; session needs only 10% of 1 GiB = ~105 MiB -> allowed.
    # This is the "already-running browser on a small host" case the user asked
    # to keep usable: the fixed floor is a launch-reserve, not a session floor.
    meminfo = textwrap.dedent(
        """\
        MemTotal:        1048576 kB
        MemFree:          102400 kB
        MemAvailable:     153600 kB
        SwapTotal:       2097152 kB
        SwapFree:        2097152 kB
        """
    )
    _patch_fs(monkeypatch, tmp_path, meminfo=meminfo,
              self_cgroup="0::/\n",
              cgroup_files={"memory.max": "max\n", "memory.current": "0\n"})
    with pytest.raises(br.BrowserResourceUnavailable):
        br.ensure_browser_resources("launch")
    report = br.ensure_browser_resources("session")
    assert report["ok"] is True
    assert report["check"] == "session"


def test_non_linux_platform_still_measures_via_fixtures(monkeypatch, tmp_path):
    """Platform string is informational only; the sampler decides."""
    _patch_fs(monkeypatch, tmp_path, meminfo=MEMINFO_OK,
              self_cgroup="0::/\n",
              cgroup_files={"memory.max": "max\n", "memory.current": "0\n"})
    monkeypatch.setattr(br.sys, "platform", "darwin")
    report = br.browser_resource_report("launch")
    assert report["sample"]["platform"] == "darwin"
    assert report["ok"] is True


# ---------------------------------------------------------------------------
# Launch entry points refuse before Playwright starts
# ---------------------------------------------------------------------------


def _patch_launch_spies(monkeypatch):
    """Record whether async_playwright/chromium.launch were reached."""
    calls = {"playwright_start": 0, "chromium_launch": 0,
             "launch_persistent": 0}

    class _FakePlaywrightModule:
        def __call__(self):
            return self

        async def start(self):
            calls["playwright_start"] += 1
            return self

        async def stop(self):
            return None

        class chromium:
            @staticmethod
            async def launch(**kwargs):
                calls["chromium_launch"] += 1
                return object()

            @staticmethod
            async def launch_persistent_context(user_data_dir, **kwargs):
                calls["launch_persistent"] += 1
                return object()

    def fake_async_playwright():
        return _FakePlaywrightModule()

    monkeypatch.setattr(browser_module, "async_playwright", fake_async_playwright)
    return calls


def test_get_browser_refuses_before_playwright_when_resources_low(monkeypatch):
    calls = _patch_launch_spies(monkeypatch)

    def deny(*args, **kwargs):
        raise br.BrowserResourceUnavailable({"check": "launch", "layer": "host",
                                             "available_bytes": 1,
                                             "required_bytes": 2})

    monkeypatch.setattr(browser_module, "ensure_browser_resources", deny)
    with pytest.raises(br.BrowserResourceUnavailable):
        asyncio.run(browser_module.get_browser())
    assert calls["playwright_start"] == 0
    assert calls["chromium_launch"] == 0


def test_get_persistent_browser_context_refuses_before_playwright(monkeypatch, tmp_path):
    calls = _patch_launch_spies(monkeypatch)

    def deny(*args, **kwargs):
        raise br.BrowserResourceUnavailable({"check": "launch", "layer": "cgroup",
                                             "available_bytes": 1,
                                             "required_bytes": 2})

    monkeypatch.setattr(browser_module, "ensure_browser_resources", deny)
    monkeypatch.setattr(browser_module, "browser_profile_root",
                        lambda root=None: tmp_path / "profiles")
    with pytest.raises(br.BrowserResourceUnavailable):
        asyncio.run(browser_module.get_persistent_browser_context(
            "acct", root=str(tmp_path / "profiles")))
    assert calls["playwright_start"] == 0
    assert calls["launch_persistent"] == 0


def test_get_browser_allows_launch_when_resources_ok(monkeypatch):
    calls = _patch_launch_spies(monkeypatch)
    seen = {}

    def allow(check, **kwargs):
        seen["check"] = check
        return {"ok": True, "check": check}

    monkeypatch.setattr(browser_module, "ensure_browser_resources", allow)
    monkeypatch.setattr(browser_module, "resolve_browser_engine",
                        lambda profile_name=None: type("E", (), {
                            "name": "stock",
                            "launch_overrides": lambda self: {},
                        })())
    result = asyncio.run(browser_module.get_browser())
    playwright, browser = result
    assert seen["check"] == "launch"
    assert calls["playwright_start"] == 1
    assert calls["chromium_launch"] == 1
    asyncio.run(playwright.stop())


def test_exception_category_is_stable_for_task_layer():
    exc = br.BrowserResourceUnavailable({"check": "launch"})
    assert exc.category == "browser_resource_unavailable"
    assert isinstance(exc, RuntimeError)
