"""Read-only memory gate for browser startup and pre-dispatch checks.

Check host MemAvailable and every finite process-memory cgroup independently;
swap is never counted as RAM. Launch requires max(256 MiB, 10% of each bound),
session checks require 10%. Linux sampling errors fail closed. Platforms without
Linux memory telemetry remain supported; no browser or sysctl is modified here.
"""

from __future__ import annotations

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
    for key in ("layer", "available_bytes", "required_bytes", "bound_bytes", "errors"):
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
        hierarchy, controllers, path = fields
        if hierarchy != "0" and "memory" not in controllers.split(","):
            continue
        path = path.strip()
        if not path:
            path = "/"
        if path == "/":
            rel = ""
        else:
            rel = path.lstrip("/")
        yield hierarchy.strip(), rel


def _resolve_cgroup_dir(base, rel):
    # Container mounts may expose /docker/<id> as the controller root.
    # Subtract the kernel mount root, not the host membership twice.
    for line in (_read_text('/proc/self/mountinfo') or '').splitlines():
        fields = line.split()
        if len(fields) < 7 or fields[4] != str(base):
            continue
        try:
            suffix = Path('/'+rel).relative_to(Path(fields[3]))
        except ValueError:
            continue
        return base / suffix
    return base / rel if rel else base


def _cgroup_v2_dir():
    """Absolute dir holding this process's v2 memory files (None if absent)."""
    for hierarchy, rel in _cgroup_memberships() or ():
        if hierarchy == "0":
            base = _CGROUP_ROOT
            return _resolve_cgroup_dir(base, rel)
    return None


def _cgroup_v1_dirs():
    """Yield absolute dirs for each v1 controller that has memory files."""
    seen = set()
    for hierarchy, rel in _cgroup_memberships() or ():
        if hierarchy == "0":
            continue
        base = _CGROUP_ROOT / "memory"
        if _read_text(_CGROUP_ROOT / "memory.limit_in_bytes") is not None:
            base = _CGROUP_ROOT
        directory = _resolve_cgroup_dir(base, rel)
        key = str(directory)
        if key in seen:
            continue
        seen.add(key)
        yield directory


def _finite_cgroup_limit(directory, *, version):
    """Read limit and usage; distinguish unlimited from missing/malformed."""
    limit_name, usage_name = (("memory.max", "memory.current") if version == 2
                              else ("memory.limit_in_bytes", "memory.usage_in_bytes"))
    raw_limit = _read_text(directory / limit_name)
    raw_current = _read_text(directory / usage_name)
    source = "cgroup_v%d" % version
    raw = (raw_limit or "").strip()
    unlimited = raw == "max" if version == 2 else raw.isdigit() and int(raw) >= _UNLIMITED_FLOOR
    if unlimited:
        return None, _parse_cgroup_v2_current(raw_current), source, "no_finite_limit"
    if not raw.isdigit() or int(raw) <= 0:
        return None, None, source, "limit_unreadable"
    limit = int(raw)
    current = _parse_cgroup_v2_current(raw_current)
    error = None if isinstance(current, int) and current >= 0 else "usage_unreadable"
    return limit, current, source, error


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
            "mem_available_bytes": fields["MemAvailable"] * 1024 if "MemAvailable" in fields else None,
            "mem_free_bytes": fields.get("MemFree", 0) * 1024 or None,
            "swap_total_bytes": fields.get("SwapTotal", 0) * 1024 or None,
            "swap_free_bytes": fields.get("SwapFree", 0) * 1024 or None,
        })

    # cgroup v2 (unified) first; it is the tightest view when present.
    v2_dir = _cgroup_v2_dir()
    if v2_dir is not None:
        limit, current, source, error = _finite_cgroup_limit(v2_dir, version=2)
        sample["cgroups"].append({
            "version": 2, "source": source, "path": str(v2_dir),
            "limit_bytes": limit, "current_bytes": current,
            "read_error": error,
        })

    for directory in _cgroup_v1_dirs():
        limit, current, source, error = _finite_cgroup_limit(directory, version=1)
        sample["cgroups"].append({
            "version": 1, "source": source, "path": str(directory),
            "limit_bytes": limit, "current_bytes": current,
            "read_error": error,
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

def _launch_thresholds():
    return {"min_available_bytes": LAUNCH_MIN_AVAILABLE_BYTES,
            "min_available_ratio": LAUNCH_MIN_AVAILABLE_RATIO}


def _session_thresholds():
    return {"min_available_ratio": SESSION_MIN_AVAILABLE_RATIO}


def browser_resource_report(check="launch", *, sampler=None):
    """Check every measured layer. Linux telemetry errors refuse a new visit."""
    if check not in {"launch", "session"}:
        raise ValueError("unknown browser resource check")
    sample = (sampler or _sample_memory)()
    thresholds = _session_thresholds() if check == "session" else _launch_thresholds()
    ratio = thresholds["min_available_ratio"]
    fixed = 0 if check == "session" else thresholds["min_available_bytes"]
    layers = []
    errors = []
    host = sample.get("host") or {}
    total, available = host.get("mem_total_bytes"), host.get("mem_available_bytes")
    linux = sample.get("platform", sys.platform).startswith("linux")
    if isinstance(total, int) and total > 0 and isinstance(available, int) and available >= 0:
        layers.append(("host", total, available))
    elif linux:
        errors.append("host_memory_unreadable")
    for cg in sample.get("cgroups") or []:
        limit, current = cg.get("limit_bytes"), cg.get("current_bytes")
        if limit is None:
            if cg.get("read_error") not in {None, "no_finite_limit"}:
                errors.append("cgroup_limit_unreadable")
            continue
        if not isinstance(limit, int) or limit <= 0 or not isinstance(current, int) or current < 0:
            errors.append("cgroup_usage_unreadable")
            continue
        layers.append(("cgroup", limit, max(0, limit-current)))
    measurements = [{"layer": name, "bound_bytes": bound, "available_bytes": free,
                     "required_bytes": max(fixed, int(bound*ratio))}
                    for name, bound, free in layers]
    tightest = min(measurements, key=lambda x:x["available_bytes"]-x["required_bytes"]) if measurements else {
        "layer":"unknown", "bound_bytes":None, "available_bytes":None, "required_bytes":None}
    return {**tightest, "check": check, "thresholds":thresholds, "sample":sample,
            "layers":measurements, "errors":errors, "measured":bool(measurements),
            "ok":not errors and all(x["available_bytes"]>=x["required_bytes"] for x in measurements)}


def ensure_browser_resources(check="launch", *, sampler=None):
    """Return a report or refuse low/unreadable Linux resources before launch."""
    report = browser_resource_report(check=check, sampler=sampler)
    if report["ok"]:
        return report
    raise BrowserResourceUnavailable(report)
