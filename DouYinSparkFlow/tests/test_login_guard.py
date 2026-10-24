import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from webui import login_guard
from webui.login_guard import LoginThrottle, password_problem


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(clock=self.clock)

    def fail_n(self, n, ip="1.1.1.1", user="admin"):
        for _ in range(n):
            self.throttle.record_failure(ip, user)

    def test_five_failures_lock_the_ip_and_username_pair_for_fifteen_minutes(self):
        self.fail_n(4)
        self.assertEqual(0, self.throttle.retry_after("1.1.1.1", "admin"))
        self.fail_n(1)
        self.assertEqual(15 * 60, self.throttle.retry_after("1.1.1.1", "admin"))
        self.clock.now += 15 * 60 - 1
        self.assertEqual(1, self.throttle.retry_after("1.1.1.1", "admin"))
        self.clock.now += 1
        self.assertEqual(0, self.throttle.retry_after("1.1.1.1", "admin"))

    def test_a_stranger_cannot_lock_the_admin_out_from_another_ip(self):
        self.fail_n(5, ip="6.6.6.6")
        self.assertGreater(self.throttle.retry_after("6.6.6.6", "admin"), 0)
        self.assertEqual(0, self.throttle.retry_after("1.1.1.1", "admin"))

    def test_username_case_and_spaces_do_not_bypass_the_pair_limit(self):
        for user in ("admin", "Admin", " ADMIN ", "admin", "aDmin"):
            self.throttle.record_failure("1.1.1.1", user)
        self.assertGreater(self.throttle.retry_after("1.1.1.1", "admin"), 0)

    def test_failures_on_other_usernames_never_lock_a_correct_owner_on_the_same_ip(self):
        # Shared NAT: a neighbour sprays usernames; the owner must still get in.
        for i in range(500):
            self.throttle.record_failure("6.6.6.6", f"user{i}")
        self.assertEqual(0, self.throttle.retry_after("6.6.6.6", "admin"))

    def test_state_stays_bounded_under_many_distinct_usernames_and_ips(self):
        for i in range(login_guard.MAX_KEYS * 3):
            self.throttle.record_failure(f"10.0.{i // 250}.{i % 250}", f"user{i}")
        self.assertLessEqual(self.throttle.size(), login_guard.MAX_KEYS)

    def test_huge_usernames_are_truncated_before_they_are_stored(self):
        for _ in range(5):
            self.throttle.record_failure("1.1.1.1", "x" * 1_000_000)
        self.assertLessEqual(self.throttle.stored_chars(), 64 + 2 * login_guard.MAX_USERNAME_CHARS)
        # Truncation must not let the attacker escape the pair limit.
        self.assertGreater(self.throttle.retry_after("1.1.1.1", "x" * 1_000_000), 0)

    def test_expired_entries_are_swept_without_being_looked_up_again(self):
        for i in range(50):
            self.throttle.record_failure("1.1.1.1", f"user{i}")
        self.clock.now += login_guard.WINDOW_SECONDS + login_guard.LOCK_SECONDS + 1
        self.throttle.record_failure("2.2.2.2", "fresh")
        self.assertEqual(1, self.throttle.size())

    def test_active_locks_survive_eviction_pressure(self):
        self.fail_n(5)  # admin locked from 1.1.1.1
        for i in range(login_guard.MAX_KEYS * 2):
            self.throttle.record_failure("9.9.9.9", f"spam{i}")
        self.assertGreater(self.throttle.retry_after("1.1.1.1", "admin"), 0)

    def test_failures_older_than_the_window_do_not_count(self):
        self.fail_n(4)
        self.clock.now += 15 * 60
        self.fail_n(4)
        self.assertEqual(0, self.throttle.retry_after("1.1.1.1", "admin"))

    def test_success_clears_the_pair_count(self):
        self.fail_n(4)
        self.throttle.record_success("1.1.1.1", "admin")
        self.fail_n(4)
        self.assertEqual(0, self.throttle.retry_after("1.1.1.1", "admin"))


class PasswordPolicyTests(unittest.TestCase):
    def test_weak_passwords_are_refused_with_a_reason(self):
        for value in ("", "short1", "aaaaaaaaaa", "1234567890", "abcdefghij", "a1a1a1a1a1a1", "admin12345!"):
            with self.subTest(value=value):
                self.assertTrue(password_problem(value, "admin"))

    def test_reasonable_passwords_are_accepted(self):
        for value in ("spark-2026-flow", "Hz7#qLm2wx", "火花续命abc123"):
            with self.subTest(value=value):
                self.assertEqual("", password_problem(value, "admin"))


class LoginRouteThrottleTests(unittest.TestCase):
    def setUp(self):
        from webui import app as app_module

        self.app_module = app_module
        patches = [
            patch.object(app_module, "is_bootstrapped", return_value=True),
            patch("webui.users.authenticate", side_effect=self._auth),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(app_module.create_app())
        self.calls = 0

    def _auth(self, username, password):
        self.calls += 1
        if password == "right-password-1":
            return {"username": username, "role": "admin", "account_refs": [], "auth_version": 0}
        return None

    def _login(self, password):
        return self.client.post(
            "/login", data={"username": "admin", "password": password}, follow_redirects=False
        )

    def test_sixth_attempt_is_refused_without_checking_the_password(self):
        for _ in range(5):
            self.assertEqual(303, self._login("wrong").status_code)
        checked = self.calls

        response = self._login("right-password-1")

        self.assertEqual(303, response.status_code)
        self.assertEqual("/login", response.headers["location"])
        self.assertEqual(checked, self.calls)
        self.assertGreater(self.client.app.state.login_throttle.retry_after("testclient", "admin"), 0)

    def test_correct_password_before_the_limit_signs_in(self):
        for _ in range(4):
            self._login("wrong")

        response = self._login("right-password-1")

        self.assertEqual("/", response.headers["location"])


class PasswordPolicyRouteTests(unittest.TestCase):
    def test_bootstrap_refuses_a_weak_password(self):
        from webui import app as app_module

        with (
            patch.object(app_module, "is_bootstrapped", return_value=False),
            patch.object(app_module, "bootstrap_admin_password") as boot,
        ):
            client = TestClient(app_module.create_app())
            client.post(
                "/bootstrap",
                data={"username": "admin", "password": "123456", "confirm_password": "123456"},
                follow_redirects=False,
            )
        boot.assert_not_called()

    def test_bootstrap_accepts_a_reasonable_password(self):
        from webui import app as app_module

        with (
            patch.object(app_module, "is_bootstrapped", return_value=False),
            patch.object(app_module, "bootstrap_admin_password") as boot,
        ):
            client = TestClient(app_module.create_app())
            client.post(
                "/bootstrap",
                data={"username": "admin", "password": "spark-2026-flow", "confirm_password": "spark-2026-flow"},
                follow_redirects=False,
            )
        boot.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class ThirdReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(clock=self.clock)

    def test_filling_the_table_with_locks_never_evicts_an_active_lock(self):
        for _ in range(5):
            self.throttle.record_failure("6.6.6.6", "admin")
        for i in range(login_guard.MAX_KEYS - 1):
            for _ in range(5):
                self.throttle.record_failure("6.6.6.6", f"pad{i}")
        self.throttle.record_failure("6.6.6.6", "overflow")
        self.assertGreater(self.throttle.retry_after("6.6.6.6", "admin"), 0)
        self.assertLessEqual(self.throttle.size(), login_guard.MAX_KEYS)

    def test_rotating_ips_against_one_username_is_slowed_down_not_locked(self):
        # 10 failures from 10 different IPs: the username enters slow mode.
        for i in range(login_guard.USER_SLOW_AFTER):
            self.throttle.record_failure(f"10.0.0.{i}", "admin")
        self.throttle.record_failure("10.0.1.1", "admin")
        wait = self.throttle.retry_after("10.0.1.2", "admin")
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, login_guard.USER_SLOW_INTERVAL_SECONDS)
        # After the short interval anyone (including the owner) may try again.
        self.clock.now += login_guard.USER_SLOW_INTERVAL_SECONDS
        self.assertEqual(0, self.throttle.retry_after("1.2.3.4", "admin"))

    def test_slow_mode_caps_guess_rate_across_ips(self):
        tried = 0
        for second in range(600):
            self.clock.now += 1
            ip = f"10.{second // 250}.{second % 250}.1"
            if self.throttle.retry_after(ip, "admin") == 0:
                tried += 1
                self.throttle.record_failure(ip, "admin")
        # Without slow mode this would be 600 guesses in 10 minutes.
        self.assertLess(tried, login_guard.USER_SLOW_AFTER + 600 // login_guard.USER_SLOW_INTERVAL_SECONDS + 2)

    def test_success_ends_slow_mode_for_that_username(self):
        for i in range(login_guard.USER_SLOW_AFTER + 1):
            self.throttle.record_failure(f"10.0.0.{i}", "admin")
        self.throttle.record_success("1.2.3.4", "admin")
        self.assertEqual(0, self.throttle.retry_after("10.0.9.9", "admin"))

    def test_user_table_is_bounded_too(self):
        for i in range(login_guard.MAX_KEYS * 3):
            self.throttle.record_failure(f"10.0.{i // 250}.{i % 250}", f"user{i}")
        self.assertLessEqual(self.throttle.user_size(), login_guard.MAX_KEYS)


class FourthReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(clock=self.clock)

    def _slow_admin(self):
        for i in range(login_guard.USER_SLOW_AFTER):
            self.throttle.record_failure(f"10.0.0.{i}", "admin")

    def test_spraying_usernames_never_evicts_an_active_slow_mode(self):
        self._slow_admin()
        self.assertGreater(self.throttle.retry_after("10.9.9.9", "admin"), 0)
        for i in range(login_guard.MAX_KEYS * 2):
            self.throttle.record_failure(f"10.1.{i // 250}.{i % 250}", f"ghost{i}")
        self.assertGreater(self.throttle.retry_after("10.9.9.9", "admin"), 0)
        self.assertLessEqual(self.throttle.user_size(), login_guard.MAX_KEYS)

    def test_table_full_of_slow_mode_users_declines_new_users_but_keeps_counting_pairs(self):
        for u in range(login_guard.MAX_KEYS):
            for i in range(login_guard.USER_SLOW_AFTER):
                self.throttle.record_failure(f"10.{u // 250}.{u % 250}.{i}", f"slow{u}")
        self.throttle.record_failure("1.1.1.1", "newcomer")
        self.assertEqual(login_guard.MAX_KEYS, self.throttle.user_size())
        self.assertGreater(self.throttle.retry_after("10.9.9.9", "slow0"), 0)
        # The per-pair limit still applies to the newcomer.
        for _ in range(login_guard.PAIR_LIMIT - 1):
            self.throttle.record_failure("1.1.1.1", "newcomer")
        self.assertGreater(self.throttle.retry_after("1.1.1.1", "newcomer"), 0)


class SaturatedLoginRouteTests(unittest.TestCase):
    """Integration: /login keeps the cross-IP cap while the pair table is saturated."""

    def test_slow_mode_still_applies_through_the_route_when_pairs_are_full(self):
        from webui import app as app_module

        with (
            patch.object(app_module, "is_bootstrapped", return_value=True),
            patch("webui.users.authenticate", return_value=None),
        ):
            client = TestClient(app_module.create_app())
            throttle = client.app.state.login_throttle
            # Saturate the pair table with active locks from other IPs.
            for i in range(login_guard.MAX_KEYS):
                for _ in range(login_guard.PAIR_LIMIT):
                    throttle.record_failure(f"10.{i // 250}.{i % 250}.1", f"pad{i}")
            for i in range(login_guard.USER_SLOW_AFTER):
                throttle.record_failure(f"10.200.0.{i}", "admin")
            self.assertGreater(throttle.retry_after("testclient", "admin"), 0)
            response = client.post("/login", data={"username": "admin", "password": "x"}, follow_redirects=False)
            self.assertEqual("/login", response.headers["location"])
