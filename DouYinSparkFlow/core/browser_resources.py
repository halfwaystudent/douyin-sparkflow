"""Pre-launch memory safety for browser sessions.

The helpers here only *measure*; they never touch the browser itself.  Two
layers are reported separately because the deployment differs:

* ``host``    - /proc/meminfo MemAvailable.  This is the whole machine, not a
  container view.  Swap is deliberately ignored: it is disk, not RAM, and a
  page-in storm is exactly the stall we are trying to avoid.
* ``cgroup``  - the tightest finite limit among cgroup v2 ``memory.max`` and
  v1 ``memory.limit_in_bytes`` discovered under the process's own
  ``/proc/self/cgroup`` membership, paired with ``memory.current`` /
  ``memory.usage_in_bytes``.  ``max``/``max``-like values mean unlimited and
  are skipped.  On cgroup v1 the kernel also reports a near-2**63 value for
  unlimited; values >= 1 EiB are treated as unlimited.

Read failures are never treated as "infinite resources".  A sampling or I/O
problem returns ``memory=None`` entries with a ``read_error`` note instead of
raising, so a non-Linux or fixture-broken host cannot silently disable the
check forever, while a real permission/disk fault is visible in the returned
dict rather than being swallowed as success.

Thresholds (see ``browser_resource_report``):

* ``launch``: refuse to start a *new* browser when the tightest bounded
  layer (cgroup limit when finite, else host MemAvailable) would drop below
  ``max(fixed_min_bytes, ratio * bound)``.  Defaults are 256 MiB fixed and
  10% of the bound.  The fixed floor keeps a 2 GiB host able to launch a
  session after its current browsers have already consumed RAM.
* ``session``: while a browser is already running, only the *fraction* floor
  (default 10% of MemTotal) is enforced by callers that re-check mid-session;
  the fixed floor is intentionally not applied there so a small host is not
  permanently locked out once it is already in use.

Everything is injectable for tests: ``_read_text`` can be monkeypatched, or
``_MEMINFO_PATH`` / ``_CGROUP_ROOT`` / ``_SELF_CGROUP_PATH`` point at fixture
files.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Injectable filesystem roots (tests patch these or _read_text directly)
# ---------------------------------------------------------------------------

_MEMINFO_PATH = Path("/proc/meminfo")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_SELF_CGROUP_PATH = Path("/proc/self/cgroup")

# cgroup v2 unified hierarchy id; also accepted for v1 lookups below.
_CGROUP_V2_HIERARCHY = "0::"

# 1 EiB in bytes; cgroup v1 reports ~2**63-1 for "unlimited", which is >= this.
_UNLIMITED_FLOOR = 1 << 60


class BrowserResourceUnavailable(RuntimeError):
    """Raised when a new browser session must not be started.

    ``category`` is the stable failure-classification token used by the task
    layer (``classify_browser_failure`` / ``account_failure`` records).  The
    exception keeps the measured detail in ``details`` for logs while the
    string form stays a single greppable token.
    """

    category = "browser_resource_unavailable"

    def __init__(self, details=None):
        self.details = dict(details or {})
        self.check = self.details.get("check") or "launch"
        super().__init__(
            "browser_resource_unavailable check=%s %s" % (
                self.check,
                _format_details(self.details),
            )
        )


def _format_details(details):
    parts = []
    for key in ("layer", "available_bytes", "required_bytes", "bound_bytes"):
        if key in details:
            parts.append(f"{key}={details[key]}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Low-level readers (return None on failure; never raise to callers)
# ---------------------------------------------------------------------------


def _read_text(path):
    """Read a proc/sysfs file.  Tests monkeypatch this directly."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_meminfo_kb(text):
    """Return {field: kibibytes} for MemTotal/MemAvailable/MemFree."""
    values = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].rstrip(":") in {
            "MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree",
        }:
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
    return values


def _parse_cgroup_v2_limit(text):
    """cgroup v2 memory.max -> bytes or None for 'max'/unparseable."""
    raw = (text or "").strip()
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value < _UNLIMITED_FLOOR else None


def _parse_cgroup_v1_limit(text):
    """cgroup v1 memory.limit_in_bytes -> bytes or None for unlimited."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # Kernel uses a huge sentinel for "no limit" on v1.
    if value <= 0 or value >= _UNLIMITED_FLOOR:
        return None
    return value


def _parse_cgroup_v2_current(text):
    raw = (text or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _cgroup_memberships():
    """Yield relative cgroup paths for this process, v2 and v1 controllers."""
    text = _read_text(_SELF_CGROUP_PATH)
    if text is None:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split(":")
        if len(fields) != 3:
            continue
        hierarchy, _controllers, path = fields
        path = path.strip()
        if not path:
            path = "/"
        if path == "/":
            rel = ""
        else:
            rel = path.lstrip("/")
        yield hierarchy.strip(), rel


def _cgroup_v2_dir():
    """Absolute dir holding this process's v2 memory files (None if absent)."""
    for hierarchy, rel in _cgroup_memberships() or ():
        if hierarchy == "0":
            base = _CGROUP_ROOT
            return base / rel if rel else base
    return None


def _cgroup_v1_dirs():
    """Yield absolute dirs for each v1 controller that has memory files."""
    seen = set()
    for hierarchy, rel in _cgroup_memberships() or ():
        if hierarchy == "0":
            continue
        base = _CGROUP_ROOT / f"memory"  # unified layout on modern kernels
        if not base.exists():
            # Older layout: /sys/fs/cgroup/memory is itself the controller root.
            base = _CGROUP_ROOT
        if not base.exists():
            continue
        directory = base / rel if rel else base
        key = str(directory)
        if key in seen:
            continue
        seen.add(key)
        yield directory


def _finite_cgroup_limit(directory, *, version):
    """Return (limit_bytes, current_bytes, source) for one cgroup dir."""
    if version == 2:
        limit_text = _read_text(directory / "memory.max")
        current_text = _read_text(directory / "memory.current")
        limit = _parse_cgroup_v2_limit(limit_text)
        current = _parse_cgroup_v2_current(current_text)
        return limit, current, "cgroup_v2"
    limit_text = _read_text(directory / "memory.limit_in_bytes")
    current_text = _read_text(directory / "memory.usage_in_bytes")
    limit = _parse_cgroup_v1_limit(limit_text)
    try:
        current = int((current_text or "").strip())
    except (TypeError, ValueError):
        current = None
    return limit, current, "cgroup_v1"


def _sample_memory():
    """Collect host + cgroup memory facts.  Never raises."""
    sample = {
        "host": {"mem_total_bytes": None, "mem_available_bytes": None,
                 "mem_free_bytes": None, "swap_total_bytes": None,
                 "swap_free_bytes": None, "read_error": None},
        "cgroups": [],
        "platform": sys.platform,
    }

    meminfo_text = _read_text(_MEMINFO_PATH)
    if meminfo_text is None:
        sample["host"]["read_error"] = "meminfo_unreadable"
    else:
        fields = _parse_meminfo_kb(meminfo_text)
        sample["host"].update({
            "mem_total_bytes": fields.get("MemTotal", 0) * 1024 or None,
            "mem_available_bytes": fields.get("MemAvailable", 0) * 1024 or None,
            "mem_free_bytes": fields.get("MemFree", 0) * 1024 or None,
            "swap_total_bytes": fields.get("SwapTotal", 0) * 1024 or None,
            "swap_free_bytes": fields.get("SwapFree", 0) * 1024 or None,
        })

    # cgroup v2 (unified) first; it is the tightest view when present.
    v2_dir = _cgroup_v2_dir()
    if v2_dir is not None:
        limit, current, source = _finite_cgroup_limit(v2_dir, version=2)
        sample["cgroups"].append({
            "version": 2, "source": source, "path": str(v2_dir),
            "limit_bytes": limit, "current_bytes": current,
            "read_error": None if limit is not None else "no_finite_limit",
        })

    for directory in _cgroup_v1_dirs():
        limit, current, source = _finite_cgroup_limit(directory, version=1)
        sample["cgroups"].append({
            "version": 1, "source": source, "path": str(directory),
            "limit_bytes": limit, "current_bytes": current,
            "read_error": None if limit is not None else "no_finite_limit",
        })

    return sample


# ---------------------------------------------------------------------------
# Thresholds and decision
# ---------------------------------------------------------------------------

# Defaults chosen so a 2 GiB host with one browser already running (~600 MiB)
# can still launch another: floor 256 MiB + ratio 10% of a 2 GiB bound = 460 MiB
# required available, comfortably below the ~1.4 GiB a healthy 2 GiB host shows.
LAUNCH_MIN_AVAILABLE_BYTES = 256 * 1024 * 1024       # 256 MiB fixed floor
LAUNCH_MIN_AVAILABLE_RATIO = 0.10                    # 10% of the bound
SESSION_MIN_AVAILABLE_RATIO = 0.10                   # in-session: fraction only

# Env overrides exist for operators but are NOT the test hook: tests patch the
# constants/sampler directly so production cannot be silently disabled by an
# environment variable left in a container spec.


def _env_int(name, default):
    try:
        return int(str(os.getenv(name) or "").strip())
    except (TypeError, ValueError):
        return default


def _launch_thresholds():
    return {
        "min_available_bytes": _env_int(
            "SPARKFLOW_BROWSER_LAUNCH_MIN_AVAILABLE_MB",
            LAUNCH_MIN_AVAILABLE_BYTES // (1024 * 1024),
        ) * 1024 * 1024,
        "min_available_ratio": float(
            os.getenv("SPARKFLOW_BROWSER_LAUNCH_MIN_AVAILABLE_RATIO")
            or LAUNCH_MIN_AVAILABLE_RATIO
        ),
    }


def _session_thresholds():
    return {
        "min_available_ratio": float(
            os.getenv("SPARKFLOW_BROWSER_SESSION_MIN_AVAILABLE_RATIO")
            or SESSION_MIN_AVAILABLE_RATIO
        ),
    }


def _tightest_bounded_layer(sample):
    """Pick the layer whose limit actually constrains this process.

    Preference order: finite cgroup limit (tightest wins) > host MemAvailable.
    Returns (layer_name, bound_bytes, available_bytes, detail_dict).
    """
    best = None
    for cg in sample.get("cgroups") or []:
        limit = cg.get("limit_bytes")
        current = cg.get("current_bytes")
        if not isinstance(limit, int) or limit <= 0:
            continue
        if not isinstance(current, int) or current < 0:
            current = 0
        available = max(0, limit - current)
        candidate = (available, limit, "cgroup", cg, current)
        if best is None or available < best[0]:
            best = candidate
    if best is not None:
        available, bound, layer, cg, current = best
        return layer, bound, available, {
            "cgroup_version": cg.get("version"),
            "cgroup_path": cg.get("path"),
            "cgroup_limit_bytes": bound,
            "cgroup_current_bytes": current,
        }

    host = sample.get("host") or {}
    available = host.get("mem_available_bytes")
    total = host.get("mem_total_bytes")
    if isinstance(available, int) and isinstance(total, int) and total > 0:
        return "host", total, available, {
            "mem_total_bytes": total,
            "mem_free_bytes": host.get("mem_free_bytes"),
            "swap_total_bytes": host.get("swap_total_bytes"),
            "swap_free_bytes": host.get("swap_free_bytes"),
            "host_read_error": host.get("read_error"),
        }
    return "unknown", None, None, {"sample": sample}


def browser_resource_report(check="launch", *, sampler=None):
    """Measure memory and decide whether a browser may start/continue.

    Returns a dict; raises nothing.  ``ok`` is False only when a *finite*
    bound was measured AND available memory is below the required threshold.
    Unmeasurable hosts/cgroups report ``ok=True`` with ``measured=False`` so
    exotic platforms keep working, while ``read_error`` fields keep the cause
    visible.
    """
    sample = (sampler or _sample_memory)()
    layer, bound, available, extra = _tightest_bounded_layer(sample)

    if check == "session":
        thresholds = _session_thresholds()
        required = (
            int(bound * thresholds["min_available_ratio"])
            if isinstance(bound, int) and bound > 0 else None
        )
    else:
        thresholds = _launch_thresholds()
        fixed = thresholds["min_available_bytes"]
        ratio = thresholds["min_available_ratio"]
        if isinstance(bound, int) and bound > 0:
            required = max(fixed, int(bound * ratio))
        else:
            required = fixed

    report = {
        "ok": True,
        "measured": available is not None and bound is not None,
        "check": check,
        "layer": layer,
        "bound_bytes": bound,
        "available_bytes": available,
        "required_bytes": required,
        "thresholds": thresholds,
        "sample": sample,
    }
    report.update(extra)

    if not report["measured"] or required is None or available is None:
        return report
    if available < required:
        report["ok"] = False
    return report


def ensure_browser_resources(check="launch", *, sampler=None):
    """Raise :class:`BrowserResourceUnavailable` when memory is too tight.

    Contract for callers (``get_browser`` / ``get_persistent_browser_context``
    use ``check="launch"``; a mid-session re-check may pass ``check="session"``):

    * returns the measurement dict on success (``ok=True``);
    * raises ``BrowserResourceUnavailable(category='browser_resource_unavailable')``
      only when a finite bound was measured and available < required;
    * never raises for missing /proc or non-Linux platforms — those report
      ``measured=False`` and pass, because refusing forever on a platform we
      cannot measure would be worse than the risk it guards.
    """
    report = browser_resource_report(check=check, sampler=sampler)
    if report["ok"]:
        return report
    raise BrowserResourceUnavailable(report)
