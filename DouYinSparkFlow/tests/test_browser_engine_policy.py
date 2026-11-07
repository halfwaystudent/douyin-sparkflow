"""Unit coverage for the optional, server-owned browser engine policy."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest


def _fingerprint_policy(path, executable, *, profile="uid-42", seed=1000):
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "fingerprint": {"executable_path": str(executable), "sha256": digest},
                "profiles": {profile: {"engine": "fingerprint", "seed": seed}},
            }
        ),
        encoding="utf-8",
    )
    return digest


def _executable(tmp_path):
    executable = tmp_path / "fingerprint-chrome"
    executable.write_bytes(b"fingerprint browser test executable")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_gate_off_returns_stock_without_reading_policy(monkeypatch, tmp_path):
    from core.browser_engine import resolve_browser_engine

    policy = tmp_path / "missing-policy.json"
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "off")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))

    selection = resolve_browser_engine("uid-42")

    assert selection.name == "stock"
    assert selection.launch_overrides() == {}
    assert selection.expectation() == {"engine": "stock"}


@pytest.mark.parametrize("value", ["", "0", "false", "NO", " off "])
def test_disabled_gate_values_are_false(monkeypatch, value):
    from core.browser_engine import fingerprint_browser_enabled

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", value)

    assert fingerprint_browser_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_enabled_gate_values_are_true(monkeypatch, value):
    from core.browser_engine import fingerprint_browser_enabled

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", value)

    assert fingerprint_browser_enabled() is True


def test_invalid_gate_value_is_rejected(monkeypatch):
    from core.browser_engine import BrowserEngineError, fingerprint_browser_enabled

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "sometimes")

    with pytest.raises(BrowserEngineError):
        fingerprint_browser_enabled()


def test_unbound_profile_is_stock_without_reading_required_policy(monkeypatch):
    from core.browser_engine import resolve_browser_engine

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.delenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", raising=False)

    assert resolve_browser_engine(None).name == "stock"


def test_known_profile_resolves_verified_fingerprint_engine(monkeypatch, tmp_path):
    from core.browser_engine import resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    digest = _fingerprint_policy(policy, executable, seed=7)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))

    selection = resolve_browser_engine("uid-42")

    assert selection.name == "fingerprint"
    assert selection.launch_overrides() == {
        "executable_path": str(executable.resolve()),
        "args": ["--fingerprint=7"],
    }
    assert selection.expectation() == {
        "engine": "fingerprint",
        "seed": 7,
        "executable_sha256": digest,
    }
    with pytest.raises((AttributeError, TypeError)):
        selection.name = "stock"


def test_unlisted_known_profile_stays_stock_when_gate_is_enabled(monkeypatch, tmp_path):
    from core.browser_engine import resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    _fingerprint_policy(policy, executable)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))

    assert resolve_browser_engine("uid-other").expectation() == {"engine": "stock"}


def test_expectations_require_exact_schema_values_and_integer_seed(monkeypatch, tmp_path):
    from core.browser_engine import (
        BrowserEngineError,
        check_engine_expectation,
        resolve_browser_engine,
    )

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    digest = _fingerprint_policy(policy, executable, seed=7)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    fingerprint = resolve_browser_engine("uid-42")
    stock = resolve_browser_engine("uid-other")

    check_engine_expectation(stock, None)
    check_engine_expectation(stock, {"engine": "stock"})
    check_engine_expectation(
        fingerprint,
        {"engine": "fingerprint", "seed": 7, "executable_sha256": digest},
    )
    for expectation in (
        None,
        {"engine": "stock", "unused": None},
        {"engine": "fingerprint", "seed": True, "executable_sha256": digest},
        {"engine": "fingerprint", "seed": 8, "executable_sha256": digest},
        {"engine": "fingerprint", "seed": 7, "executable_sha256": digest.upper()},
    ):
        with pytest.raises(BrowserEngineError):
            check_engine_expectation(fingerprint, expectation)


def test_profile_engine_creates_durable_fingerprint_marker_on_fresh_profile(monkeypatch, tmp_path):
    from core.browser_engine import prepare_profile_engine, resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    digest = _fingerprint_policy(policy, executable, seed=9)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    selection = resolve_browser_engine("uid-42")
    profile_dir = tmp_path / "profiles" / "uid-42"

    prepare_profile_engine(profile_dir, selection)

    marker = profile_dir / ".sparkflow-browser-engine.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema": 1,
        "engine": "fingerprint",
        "seed": 9,
        "executable_sha256": digest,
    }
    assert not list(profile_dir.glob(".sparkflow-browser-engine-*.tmp"))


def test_profile_binding_allows_existing_sparkflow_metadata_only(monkeypatch, tmp_path):
    from core.browser_engine import prepare_profile_engine, resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    _fingerprint_policy(policy, executable)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    profile_dir = tmp_path / "fresh-with-sparkflow-metadata"
    profile_dir.mkdir()
    for name in (".sparkflow-cookie-session.json", ".sparkflow-login-pending.json"):
        (profile_dir / name).write_text("{}", encoding="utf-8")

    prepare_profile_engine(profile_dir, resolve_browser_engine("uid-42"))

    assert (profile_dir / ".sparkflow-browser-engine.json").is_file()


def test_stock_profile_does_not_create_a_marker_or_directory(monkeypatch, tmp_path):
    from core.browser_engine import prepare_profile_engine, resolve_browser_engine

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "off")
    profile_dir = tmp_path / "ordinary-stock-profile"

    prepare_profile_engine(profile_dir, resolve_browser_engine("uid-42"))

    assert not profile_dir.exists()


@pytest.mark.parametrize("alias", ["relative", "symlink"])
def test_stock_cannot_downgrade_fingerprint_marker_through_legacy_path_alias(monkeypatch, tmp_path, alias):
    from core.browser_engine import BrowserEngineError, BrowserEngineSelection, prepare_profile_engine

    profile = tmp_path / "real-profiles" / "uid-42"
    profile.mkdir(parents=True)
    marker = profile / ".sparkflow-browser-engine.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "engine": "fingerprint",
                "seed": 123,
                "executable_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    before = marker.read_bytes()
    if alias == "relative":
        monkeypatch.chdir(tmp_path)
        profile_alias = os.path.join("real-profiles", "uid-42")
    else:
        profile_root_alias = tmp_path / "profile-alias"
        profile_root_alias.symlink_to(profile.parent, target_is_directory=True)
        profile_alias = profile_root_alias / "uid-42"

    with pytest.raises(BrowserEngineError, match="fingerprint-bound"):
        prepare_profile_engine(profile_alias, BrowserEngineSelection("stock"))

    assert marker.read_bytes() == before


def test_profile_binding_refuses_downgrade_mismatch_and_existing_chromium(monkeypatch, tmp_path):
    from core.browser_engine import BrowserEngineError, prepare_profile_engine, resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    _fingerprint_policy(policy, executable, seed=9)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    fingerprint = resolve_browser_engine("uid-42")
    profile_dir = tmp_path / "profiles" / "uid-42"
    prepare_profile_engine(profile_dir, fingerprint)

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "off")
    with pytest.raises(BrowserEngineError, match="fingerprint-bound"):
        prepare_profile_engine(profile_dir, resolve_browser_engine("uid-42"))

    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    _fingerprint_policy(policy, executable, seed=10)
    with pytest.raises(BrowserEngineError, match="different"):
        prepare_profile_engine(profile_dir, resolve_browser_engine("uid-42"))

    unbound = tmp_path / "profiles" / "already-chromium"
    unbound.mkdir()
    (unbound / "Preferences").write_text("{}", encoding="utf-8")
    with pytest.raises(BrowserEngineError, match="migration or backup"):
        prepare_profile_engine(unbound, resolve_browser_engine("uid-42"))


def test_policy_rejects_duplicate_keys_bool_seed_bad_digest_and_symlink_executable(monkeypatch, tmp_path):
    from core.browser_engine import BrowserEngineError, resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    policy.write_text('{"schema":1,"schema":1,"fingerprint":{},"profiles":{}}', encoding="utf-8")
    with pytest.raises(BrowserEngineError):
        resolve_browser_engine("uid-42")

    _fingerprint_policy(policy, executable, seed=True)
    with pytest.raises(BrowserEngineError):
        resolve_browser_engine("uid-42")

    policy_data = json.loads(policy.read_text(encoding="utf-8"))
    policy_data["profiles"]["uid-42"]["seed"] = 1
    policy_data["fingerprint"]["sha256"] = "A" * 64
    policy.write_text(json.dumps(policy_data), encoding="utf-8")
    with pytest.raises(BrowserEngineError):
        resolve_browser_engine("uid-42")

    executable.unlink()
    target = tmp_path / "real-chrome"
    target.write_bytes(b"real")
    target.chmod(target.stat().st_mode | stat.S_IXUSR)
    executable.symlink_to(target)
    _fingerprint_policy(policy, executable, seed=1)
    with pytest.raises(BrowserEngineError, match="symlink"):
        resolve_browser_engine("uid-42")


def test_profile_rejects_symlink_or_invalid_engine_marker(monkeypatch, tmp_path):
    from core.browser_engine import BrowserEngineError, prepare_profile_engine, resolve_browser_engine

    executable = _executable(tmp_path)
    policy = tmp_path / "policy.json"
    _fingerprint_policy(policy, executable)
    monkeypatch.setenv("SPARKFLOW_FINGERPRINT_BROWSER_ENABLED", "on")
    monkeypatch.setenv("SPARKFLOW_BROWSER_ENGINE_POLICY_FILE", str(policy))
    selection = resolve_browser_engine("uid-42")

    target = tmp_path / "target"
    target.mkdir()
    symlinked = tmp_path / "profiles" / "uid-42"
    symlinked.parent.mkdir()
    symlinked.symlink_to(target, target_is_directory=True)
    with pytest.raises(BrowserEngineError, match="symlink"):
        prepare_profile_engine(symlinked, selection)

    profile = tmp_path / "profile"
    profile.mkdir()
    marker = profile / ".sparkflow-browser-engine.json"
    marker.write_text('{"schema":1,"engine":"stock"}', encoding="utf-8")
    with pytest.raises(BrowserEngineError, match="marker"):
        prepare_profile_engine(profile, selection)
