"""Offline contract tests for persistent-profile cookie synchronization."""
from __future__ import annotations

import asyncio
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
import unittest

from core import profile_cookies


class Store:
    """In-memory stand-in for utils.config's locked account transaction."""

    def __init__(self, accounts):
        self.accounts = deepcopy(accounts)

    def read(self, **_kwargs):
        return deepcopy(self.accounts)

    def update(self, mutator, **_kwargs):
        result = mutator(self.accounts)
        changed = True
        if isinstance(result, tuple) and len(result) == 2:
            result, changed = result
        return deepcopy(result)


def auth(value="saved", *, domain=".douyin.com", expires=-1, name="sessionid"):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


def account(*, generation="gen-a", cookies=None, ref="acct-a", unique_id="101", **extra):
    result = {
        "account_ref": ref,
        "unique_id": unique_id,
        "login_generation": generation,
        "cookies": list(cookies if cookies is not None else [auth()]),
        "targets": ["offline-target"],
    }
    result.update(extra)
    return result


def profile(**values):
    result = {"syncStoredCookiesBeforeRun": True, "seedCookiesWhenEmpty": False}
    result.update(values)
    return result


def context(cookies):
    return Mock(cookies=AsyncMock(return_value=deepcopy(cookies)), add_cookies=AsyncMock(), clear_cookies=AsyncMock())


def prepare(store, ctx, saved, cfg, directory):
    return patch.object(profile_cookies, "get_userData", store.read), patch.object(
        profile_cookies, "update_user_data", store.update
    )


def test_legacy_profile_auth_adopts_stored_generation_without_importing():
    saved = account()
    store = Store([saved])
    ctx = context([auth("profile")])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(
                profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory))
            )
        marker = json.loads((Path(directory) / ".sparkflow-cookie-session.json").read_text())
    ctx.add_cookies.assert_not_awaited()
    assert session.account["login_generation"] == "gen-a"
    assert marker == {
        "version": 1,
        "account_ref": "acct-a",
        "unique_id": "101",
        "login_generation": "gen-a",
    }


def test_empty_profile_seeds_only_when_configured():
    saved = account(cookies=[auth("saved")])
    store = Store([saved])
    ctx = context([])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(
                profile_cookies.prepare_profile_cookies(
                    ctx, saved, profile(seedCookiesWhenEmpty=True), Path(directory)
                )
            )
        assert (Path(directory) / ".sparkflow-cookie-session.json").exists()
    ctx.clear_cookies.assert_not_awaited()
    ctx.add_cookies.assert_awaited_once_with([auth("saved")])
    assert session.imported is True


def test_explicit_new_generation_replaces_douyin_once_and_marker_survives_restart():
    saved = account(generation="gen-new", cookies=[auth("new")], browser_cookie_import_generation="gen-new")
    store = Store([saved])
    ctx = context([auth("old"), {"name": "unrelated", "value": "keep", "domain": ".example.test", "path": "/"}])
    with tempfile.TemporaryDirectory() as directory:
        marker_path = Path(directory) / ".sparkflow-cookie-session.json"
        marker_path.write_text(json.dumps({"version": 1, "account_ref": "acct-a", "unique_id": "101", "login_generation": "gen-old"}))
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
            again = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
            marker = json.loads(marker_path.read_text())
    assert session.imported is True
    assert again.imported is False
    ctx.add_cookies.assert_awaited_once_with([auth("new")])
    ctx.clear_cookies.assert_awaited_once()
    domain_filter = ctx.clear_cookies.await_args.kwargs["domain"]
    assert domain_filter.search(".douyin.com")
    assert domain_filter.search("creator.douyin.com")
    assert not domain_filter.search("example.test")
    assert marker["login_generation"] == "gen-new"


def test_pending_initial_login_replaces_existing_profile_without_legacy_adoption():
    saved = account(generation="gen-new", cookies=[auth("new")], browser_cookie_import_generation="gen-new")
    store = Store([saved])
    ctx = context([auth("old")])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
    assert session.imported is True
    ctx.clear_cookies.assert_awaited_once()
    ctx.add_cookies.assert_awaited_once_with([auth("new")])


def test_changed_marker_generation_imports_only_when_sync_is_enabled():
    saved = account(generation="gen-new", cookies=[auth("new")])
    store = Store([saved])
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory, ".sparkflow-cookie-session.json")
        marker.write_text(json.dumps({"version": 1, "account_ref": "acct-a", "unique_id": "101", "login_generation": "gen-old"}))
        ctx = context([auth("old")])
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
        assert session.imported is True
        ctx.clear_cookies.assert_awaited_once()
        ctx.add_cookies.assert_awaited_once_with([auth("new")])

        marker.write_text(json.dumps({"version": 1, "account_ref": "acct-a", "unique_id": "101", "login_generation": "gen-old"}))
        blocked = context([auth("old")])
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(blocked, saved, profile(syncStoredCookiesBeforeRun=False), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("changed stored login must fail closed when sync is disabled")
        blocked.add_cookies.assert_not_awaited()
        blocked.clear_cookies.assert_not_awaited()


def test_publish_exports_verified_douyin_snapshot_and_updates_session_baseline():
    saved = account(cookies=[auth("saved")])
    store = Store([saved])
    ctx = context([auth("profile")])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
            assert asyncio.run(profile_cookies.publish_profile_cookies(ctx, session)) is True
            ctx.cookies.return_value = [auth("later")]
            assert asyncio.run(profile_cookies.publish_profile_cookies(ctx, session)) is True
    assert store.accounts[0]["cookies"] == [auth("later")]
    assert session.account["cookies"] == [auth("later")]


def test_publish_cas_rejects_new_login_deletion_and_other_cookie_writer():
    for changed in (
        lambda store: store.accounts.__setitem__(0, account(generation="gen-new", cookies=[auth("new")])),
        lambda store: store.accounts.clear(),
        lambda store: store.accounts[0].update(cookies=[auth("other")]),
    ):
        saved = account(cookies=[auth("saved")])
        store = Store([saved])
        ctx = context([auth("profile")])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(profile_cookies, "get_userData", store.read), patch.object(
                profile_cookies, "update_user_data", store.update
            ):
                session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
                changed(store)
                assert asyncio.run(profile_cookies.publish_profile_cookies(ctx, session)) is False
        assert store.accounts != [{**saved, "cookies": [auth("profile")] }]


def test_publish_rejects_empty_or_invalid_profile_snapshot():
    saved = account(cookies=[auth("saved")])
    store = Store([saved])
    ctx = context([auth(""), auth("foreign", domain=".example.test")])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(context([auth("profile")]), saved, profile(), Path(directory)))
            assert asyncio.run(profile_cookies.publish_profile_cookies(ctx, session)) is False
    assert store.accounts[0]["cookies"] == [auth("saved")]


def test_malformed_marker_or_profile_cookie_read_failure_fails_closed():
    saved = account()
    store = Store([saved])
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, ".sparkflow-cookie-session.json").write_text("not json")
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(context([auth("profile")]), saved, profile(), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("malformed marker must not be treated as an empty profile")
        unreadable = context([])
        unreadable.cookies.side_effect = RuntimeError("offline read failure")
        Path(directory, ".sparkflow-cookie-session.json").unlink()
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(unreadable, saved, profile(seedCookiesWhenEmpty=True), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("profile read failure must not seed cookies")
        unreadable.add_cookies.assert_not_awaited()


def test_foreign_or_expired_auth_cookie_does_not_make_profile_authenticated():
    saved = account()
    store = Store([saved])
    ctx = context([auth("foreign", domain=".example.test"), auth("expired", expires=1)])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
        assert not (Path(directory) / ".sparkflow-cookie-session.json").exists()
    assert session.imported is False
    ctx.add_cookies.assert_not_awaited()


def test_concurrent_login_before_or_after_add_never_claims_import_complete():
    saved = account(generation="gen-a", cookies=[auth("saved")], browser_cookie_import_generation="gen-a")
    newer = account(generation="gen-b", cookies=[auth("new")], browser_cookie_import_generation="gen-b")
    for reads, expected_adds in (([saved, saved, newer], 0), ([saved, saved, saved, newer], 1)):
        ctx = context([auth("old")])
        remaining = [deepcopy(items) for items in reads]

        def read(**_kwargs):
            return [deepcopy(remaining.pop(0))]

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(profile_cookies, "get_userData", side_effect=read), patch.object(
                profile_cookies, "update_user_data", Mock()
            ):
                try:
                    asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
                except profile_cookies.ProfileCookieSyncError:
                    pass
                else:
                    raise AssertionError("concurrent login must abort import")
            assert not (Path(directory) / ".sparkflow-cookie-session.json").exists()
        assert ctx.add_cookies.await_count == expected_adds


def test_marker_write_error_never_returns_successful_session():
    saved = account()
    store = Store([saved])
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ), patch.object(profile_cookies.os, "replace", side_effect=OSError("offline disk")):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(context([auth("profile")]), saved, profile(), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("marker write failure must fail closed")


def test_seed_rechecks_account_after_add_before_writing_marker():
    saved = account(cookies=[auth("saved")])
    newer = account(generation="gen-b", cookies=[auth("new")])
    reads = [[saved], [saved], [saved], [newer]]
    ctx = context([])

    def read(**_kwargs):
        return deepcopy(reads.pop(0))

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", side_effect=read), patch.object(
            profile_cookies, "update_user_data", Mock()
        ):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(seedCookiesWhenEmpty=True), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("post-add login change must abort before marker write")
        assert not (Path(directory) / ".sparkflow-cookie-session.json").exists()
    ctx.add_cookies.assert_awaited_once()


def test_prepare_rechecks_snapshot_after_profile_read_before_marker_or_return():
    saved = account()
    newer = account(generation="gen-b", cookies=[auth("new")])
    store = Store([saved])
    ctx = context([])

    async def read():
        store.accounts[:] = [deepcopy(newer)]
        return [auth("profile")]

    ctx.cookies.side_effect = read
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(profile_cookies, "get_userData", store.read), patch.object(
            profile_cookies, "update_user_data", store.update
        ):
            try:
                asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), Path(directory)))
            except profile_cookies.ProfileCookieSyncError:
                pass
            else:
                raise AssertionError("changed account after cookie read must fail closed")
        assert not (Path(directory) / ".sparkflow-cookie-session.json").exists()

def test_import_filters_foreign_domain_even_when_bundle_contains_valid_auth():
    foreign = {"name": "offline-control", "value": "synthetic-foreign", "domain": ".example.test", "path": "/"}
    saved = account(cookies=[auth(), foreign], browser_cookie_import_generation="gen-a")
    store = Store([saved]); ctx = context([foreign])
    with tempfile.TemporaryDirectory() as directory, patch.object(profile_cookies, "get_userData", store.read):
        asyncio.run(profile_cookies.prepare_profile_cookies(ctx, saved, profile(), directory))
    ctx.add_cookies.assert_awaited_once_with([auth()])


def test_same_generation_pending_request_still_seeds_a_cleared_profile():
    saved = account(browser_cookie_import_generation="gen-a")
    store = Store([saved])
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory, ".sparkflow-cookie-session.json")
        marker.write_text(json.dumps({"version": 1, "account_ref": "acct-a", "unique_id": "101", "login_generation": "gen-a"}))
        ctx = context([])
        with patch.object(profile_cookies, "get_userData", store.read):
            session = asyncio.run(profile_cookies.prepare_profile_cookies(
                ctx, saved, profile(seedCookiesWhenEmpty=True), directory))
        assert session.imported is True
        ctx.add_cookies.assert_awaited_once_with(saved["cookies"])
        ctx.clear_cookies.assert_not_awaited()


class ProfileCookieSyncTests(unittest.TestCase):
    pass


def _case(function):
    def run(self):
        function()
    return run


for _name, _function in list(globals().items()):
    if _name.startswith("test_"):
        setattr(ProfileCookieSyncTests, _name, _case(_function))
